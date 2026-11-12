// AI Virtual Camera — CMIOExtension provider, device and streams.
//
// Device "AI Virtual Camera" exposes:
//   source stream  "AI Virtual Camera"        what other apps (Zoom, FaceTime, OBS...) receive
//   sink stream    "AI Virtual Camera Input"  hidden; AI Virtual Camera Studio enqueues processed frames
//
// Frames are IOSurface-backed CVPixelBuffers, so they cross the process boundary without copies.
// The source stream runs on a timer at the client's chosen frame rate and always delivers the
// newest sink frame (repeating it if the app is momentarily late). If no app frame arrives for
// NO_SIGNAL_AFTER seconds, a "waiting for the app" card is sent so client apps keep the camera open.
// Frames whose size differs from the active format are scaled with VTPixelTransferSession (hardware).
import AppKit
import CoreMediaIO
import CoreVideo
import Foundation
import IOKit.audio
import VideoToolbox
import os.log

let log = OSLog(subsystem: "com.aivcstudio.app.camera", category: "extension")
let CAMERA_NAME = "AI Virtual Camera"
let SINK_NAME = "AI Virtual Camera Input"
let NO_SIGNAL_AFTER: Double = 1.0

struct VCFormat {
    let width: Int32
    let height: Int32
}
let FORMATS = [VCFormat(width: 1920, height: 1080), VCFormat(width: 1280, height: 720)]
let FRAME_RATES: [Int32] = [60, 30]

// MARK: - Provider

final class VCProviderSource: NSObject, CMIOExtensionProviderSource {
    private(set) var provider: CMIOExtensionProvider!
    private var deviceSource: VCDeviceSource!

    init(clientQueue: DispatchQueue?) {
        super.init()
        provider = CMIOExtensionProvider(source: self, clientQueue: clientQueue)
        deviceSource = VCDeviceSource(localizedName: CAMERA_NAME)
        do {
            try provider.addDevice(deviceSource.device)
        } catch {
            fatalError("Failed to add device: \(error.localizedDescription)")
        }
    }

    func connect(to client: CMIOExtensionClient) throws {}
    func disconnect(from client: CMIOExtensionClient) {}

    var availableProperties: Set<CMIOExtensionProperty> { [.providerManufacturer] }

    func providerProperties(forProperties properties: Set<CMIOExtensionProperty>) throws -> CMIOExtensionProviderProperties {
        let p = CMIOExtensionProviderProperties(dictionary: [:])
        if properties.contains(.providerManufacturer) { p.manufacturer = "AI Virtual Camera Studio" }
        return p
    }

    func setProviderProperties(_ providerProperties: CMIOExtensionProviderProperties) throws {}
}

// MARK: - Device

final class VCDeviceSource: NSObject, CMIOExtensionDeviceSource {
    private(set) var device: CMIOExtensionDevice!
    private var source: VCStreamSource!
    private var sink: VCSinkSource!
    let queue = DispatchQueue(label: "com.aivcstudio.camera.output", qos: .userInteractive)
    private var timer: DispatchSourceTimer?
    private var sourceStarted = false
    private var sinkStarted = false
    // Each start of the sink stream gets a new generation; a consume loop from an earlier start ends
    // when it sees a newer generation, so a quick stop -> start never leaves two loops reading.
    private var sinkGeneration: UInt64 = 0

    // latest frame from the app (guarded by `queue`)
    private var latest: CVPixelBuffer?
    private var latestAt: UInt64 = 0
    private var placeholder: [String: CVPixelBuffer] = [:]
    private var scaler: VTPixelTransferSession?
    private var scalePools: [String: CVPixelBufferPool] = [:]
    private var sequence: UInt64 = 0

    init(localizedName: String) {
        super.init()
        let deviceID = UUID(uuidString: "9A1C6F3E-5B7D-4E2A-8C11-AE0C0D5A7C01")!
        device = CMIOExtensionDevice(localizedName: localizedName, deviceID: deviceID, legacyDeviceID: nil, source: self)

        var formats: [CMIOExtensionStreamFormat] = []
        for f in FORMATS {
            var desc: CMFormatDescription?
            CMVideoFormatDescriptionCreate(allocator: kCFAllocatorDefault, codecType: kCVPixelFormatType_32BGRA,
                                           width: f.width, height: f.height, extensions: nil, formatDescriptionOut: &desc)
            let durations = FRAME_RATES.map { CMTime(value: 1, timescale: $0) }
            formats.append(CMIOExtensionStreamFormat(formatDescription: desc!, maxFrameDuration: durations.last!,
                                                     minFrameDuration: durations.first!, validFrameDurations: durations))
        }
        source = VCStreamSource(localizedName: localizedName,
                                streamID: UUID(uuidString: "9A1C6F3E-5B7D-4E2A-8C11-AE0C0D5A7C02")!,
                                formats: formats, device: self)
        sink = VCSinkSource(localizedName: SINK_NAME,
                            streamID: UUID(uuidString: "9A1C6F3E-5B7D-4E2A-8C11-AE0C0D5A7C03")!,
                            formats: formats, device: self)
        do {
            try device.addStream(source.stream)
            try device.addStream(sink.stream)
        } catch {
            fatalError("Failed to add streams: \(error.localizedDescription)")
        }
        VTPixelTransferSessionCreate(allocator: kCFAllocatorDefault, pixelTransferSessionOut: &scaler)
    }

    var availableProperties: Set<CMIOExtensionProperty> { [.deviceTransportType, .deviceModel] }

    func deviceProperties(forProperties properties: Set<CMIOExtensionProperty>) throws -> CMIOExtensionDeviceProperties {
        let p = CMIOExtensionDeviceProperties(dictionary: [:])
        if properties.contains(.deviceTransportType) { p.transportType = kIOAudioDeviceTransportTypeVirtual }
        if properties.contains(.deviceModel) { p.model = "AI Virtual Camera Studio" }
        return p
    }

    func setDeviceProperties(_ deviceProperties: CMIOExtensionDeviceProperties) throws {}

    // MARK: source stream lifecycle (timer-driven output)
    func startSource() {
        queue.async { [self] in
            sourceStarted = true
            restartTimer()
        }
    }

    func stopSource() {
        queue.async { [self] in
            sourceStarted = false
            timer?.cancel()
            timer = nil
        }
    }

    func restartTimer() {
        timer?.cancel()
        guard sourceStarted else { return }
        let fps = max(1, source.activeFrameRate)
        let t = DispatchSource.makeTimerSource(flags: .strict, queue: queue)
        t.schedule(deadline: .now(), repeating: 1.0 / Double(fps), leeway: .milliseconds(1))
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume()
        timer = t
    }

    // MARK: sink stream lifecycle (app -> extension)
    func startSink(client: CMIOExtensionClient) {
        queue.async { [self] in
            sinkStarted = true
            sinkGeneration &+= 1
            consume(from: client, generation: sinkGeneration, failures: 0)
        }
    }

    func stopSink() {
        queue.async { [self] in
            sinkStarted = false
            sinkGeneration &+= 1      // ends the running consume loop
            latest = nil
        }
    }

    /// Runs on `queue`. Errors (client gone, transient) back off 20 ms doubling to 1 s, so a sender that
    /// crashed without stopping the stream does not cause a busy retry loop.
    private func consume(from client: CMIOExtensionClient, generation: UInt64, failures: Int) {
        guard sinkStarted, generation == sinkGeneration else { return }
        sink.stream.consumeSampleBuffer(from: client) { [weak self] sbuf, seq, _, _, err in
            guard let self = self else { return }
            self.queue.async {
                guard self.sinkStarted, generation == self.sinkGeneration else { return }
                if let sbuf = sbuf, let pb = CMSampleBufferGetImageBuffer(sbuf) {
                    self.latest = pb
                    self.latestAt = mach_absolute_time()
                    let out = CMIOExtensionScheduledOutput(sequenceNumber: seq,
                                                           hostTimeInNanoseconds: self.hostNanos())
                    self.sink.stream.notifyScheduledOutputChanged(out)
                }
                if err != nil {
                    let delay = min(1000, 20 << min(failures, 6))
                    self.queue.asyncAfter(deadline: .now() + .milliseconds(delay)) {
                        self.consume(from: client, generation: generation, failures: failures + 1)
                    }
                } else {
                    self.consume(from: client, generation: generation, failures: 0)
                }
            }
        }
    }

    // MARK: output
    private func hostNanos() -> UInt64 {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        return mach_absolute_time() * UInt64(info.numer) / UInt64(info.denom)
    }

    private func tick() {
        let (w, h) = source.activeSize
        var frame: CVPixelBuffer?
        let ageSeconds = Double(hostNanos()) / 1e9 - Double(latestAt) * machSeconds()
        if let pb = latest, ageSeconds < NO_SIGNAL_AFTER {
            frame = (CVPixelBufferGetWidth(pb) == w && CVPixelBufferGetHeight(pb) == h) ? pb : scaled(pb, w, h)
        }
        if frame == nil { frame = noSignalCard(w, h) }
        guard let pb = frame else { return }
        var desc: CMFormatDescription?
        CMVideoFormatDescriptionCreateForImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: pb, formatDescriptionOut: &desc)
        let now = CMClockGetTime(CMClockGetHostTimeClock())
        var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: source.activeFrameRate),
                                        presentationTimeStamp: now, decodeTimeStamp: .invalid)
        var sbuf: CMSampleBuffer?
        let err = CMSampleBufferCreateForImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: pb, dataReady: true,
                                                     makeDataReadyCallback: nil, refcon: nil, formatDescription: desc!,
                                                     sampleTiming: &timing, sampleBufferOut: &sbuf)
        guard err == noErr, let s = sbuf else { return }
        sequence += 1
        source.stream.send(s, discontinuity: [], hostTimeInNanoseconds: UInt64(now.seconds * 1e9))
    }

    private var _machSeconds: Double = 0
    private func machSeconds() -> Double {
        if _machSeconds == 0 {
            var info = mach_timebase_info_data_t()
            mach_timebase_info(&info)
            _machSeconds = Double(info.numer) / Double(info.denom) / 1e9
        }
        return _machSeconds
    }

    private func pool(_ w: Int, _ h: Int) -> CVPixelBufferPool? {
        let key = "\(w)x\(h)"
        if let p = scalePools[key] { return p }
        let attrs: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                                    kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h,
                                    kCVPixelBufferIOSurfacePropertiesKey as String: [:]]
        var p: CVPixelBufferPool?
        CVPixelBufferPoolCreate(kCFAllocatorDefault, [kCVPixelBufferPoolMinimumBufferCountKey: 3] as CFDictionary,
                                attrs as CFDictionary, &p)
        scalePools[key] = p
        return p
    }

    private func scaled(_ src: CVPixelBuffer, _ w: Int, _ h: Int) -> CVPixelBuffer? {
        guard let s = scaler, let p = pool(w, h) else { return nil }
        var out: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, p, &out)
        guard let o = out, VTPixelTransferSessionTransferImage(s, from: src, to: o) == noErr else { return nil }
        return o
    }

    private func noSignalCard(_ w: Int, _ h: Int) -> CVPixelBuffer? {
        let key = "\(w)x\(h)"
        if let p = placeholder[key] { return p }
        guard let p = pool(w, h) else { return nil }
        var out: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, p, &out)
        guard let pb = out else { return nil }
        CVPixelBufferLockBaseAddress(pb, [])
        if let base = CVPixelBufferGetBaseAddress(pb),
           let ctx = CGContext(data: base, width: w, height: h, bitsPerComponent: 8,
                               bytesPerRow: CVPixelBufferGetBytesPerRow(pb), space: CGColorSpaceCreateDeviceRGB(),
                               bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue) {
            ctx.setFillColor(CGColor(red: 0.06, green: 0.07, blue: 0.09, alpha: 1))
            ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
            let ns = NSGraphicsContext(cgContext: ctx, flipped: false)
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = ns
            let title = NSAttributedString(string: CAMERA_NAME, attributes: [
                .font: NSFont.systemFont(ofSize: CGFloat(h) / 14, weight: .semibold),
                .foregroundColor: NSColor(calibratedWhite: 0.92, alpha: 1)])
            let sub = NSAttributedString(string: "Waiting for AI Virtual Camera Studio…", attributes: [
                .font: NSFont.systemFont(ofSize: CGFloat(h) / 30), .foregroundColor: NSColor(calibratedWhite: 0.6, alpha: 1)])
            let ts = title.size(), ss = sub.size()
            title.draw(at: NSPoint(x: (CGFloat(w) - ts.width) / 2, y: CGFloat(h) / 2))
            sub.draw(at: NSPoint(x: (CGFloat(w) - ss.width) / 2, y: CGFloat(h) / 2 - ss.height * 1.6))
            NSGraphicsContext.restoreGraphicsState()
        }
        CVPixelBufferUnlockBaseAddress(pb, [])
        placeholder[key] = pb
        return pb
    }
}

// MARK: - Streams

class VCStreamBase: NSObject, CMIOExtensionStreamSource {
    private(set) var stream: CMIOExtensionStream!
    let formatsList: [CMIOExtensionStreamFormat]
    unowned let device: VCDeviceSource
    // Set on the CMIO client queue, read by the output timer on the device queue: guarded by a lock.
    private let propLock = NSLock()
    private var _activeFormatIndex: Int = 0
    private var _activeFrameRate: Int32 = 60
    var activeFormatIndex: Int {
        get { propLock.lock(); defer { propLock.unlock() }; return _activeFormatIndex }
        set { propLock.lock(); _activeFormatIndex = newValue; propLock.unlock() }
    }
    var activeFrameRate: Int32 {
        get { propLock.lock(); defer { propLock.unlock() }; return _activeFrameRate }
        set { propLock.lock(); _activeFrameRate = newValue; propLock.unlock() }
    }

    init(localizedName: String, streamID: UUID, direction: CMIOExtensionStream.Direction,
         formats: [CMIOExtensionStreamFormat], device: VCDeviceSource) {
        self.formatsList = formats
        self.device = device
        super.init()
        stream = CMIOExtensionStream(localizedName: localizedName, streamID: streamID, direction: direction,
                                     clockType: .hostTime, source: self)
    }

    var activeSize: (Int, Int) {
        let d = CMVideoFormatDescriptionGetDimensions(formatsList[activeFormatIndex].formatDescription)
        return (Int(d.width), Int(d.height))
    }

    var formats: [CMIOExtensionStreamFormat] { formatsList }

    var availableProperties: Set<CMIOExtensionProperty> { [.streamActiveFormatIndex, .streamFrameDuration] }

    func streamProperties(forProperties properties: Set<CMIOExtensionProperty>) throws -> CMIOExtensionStreamProperties {
        let p = CMIOExtensionStreamProperties(dictionary: [:])
        if properties.contains(.streamActiveFormatIndex) { p.activeFormatIndex = activeFormatIndex }
        if properties.contains(.streamFrameDuration) {
            p.frameDuration = CMTime(value: 1, timescale: activeFrameRate)
        }
        return p
    }

    func setStreamProperties(_ streamProperties: CMIOExtensionStreamProperties) throws {
        if let i = streamProperties.activeFormatIndex, i >= 0, i < formatsList.count { activeFormatIndex = i }
        if let d = streamProperties.frameDuration, d.value > 0 {
            let fps = Int32((Double(d.timescale) / Double(d.value)).rounded())
            activeFrameRate = FRAME_RATES.contains(fps) ? fps : 30
        }
    }

    func authorizedToStartStream(for client: CMIOExtensionClient) -> Bool { true }
    func startStream() throws {}
    func stopStream() throws {}
}

final class VCStreamSource: VCStreamBase {
    init(localizedName: String, streamID: UUID, formats: [CMIOExtensionStreamFormat], device: VCDeviceSource) {
        super.init(localizedName: localizedName, streamID: streamID, direction: .source, formats: formats, device: device)
    }

    override func setStreamProperties(_ streamProperties: CMIOExtensionStreamProperties) throws {
        try super.setStreamProperties(streamProperties)
        device.queue.async { self.device.restartTimer() }
    }

    override func startStream() throws { device.startSource() }
    override func stopStream() throws { device.stopSource() }
}

final class VCSinkSource: VCStreamBase {
    private var client: CMIOExtensionClient?

    init(localizedName: String, streamID: UUID, formats: [CMIOExtensionStreamFormat], device: VCDeviceSource) {
        super.init(localizedName: localizedName, streamID: streamID, direction: .sink, formats: formats, device: device)
    }

    override var availableProperties: Set<CMIOExtensionProperty> {
        super.availableProperties.union([.streamSinkBufferQueueSize, .streamSinkBuffersRequiredForStartup,
                                         .streamSinkBufferUnderrunCount, .streamSinkEndOfData])
    }

    override func streamProperties(forProperties properties: Set<CMIOExtensionProperty>) throws -> CMIOExtensionStreamProperties {
        let p = try super.streamProperties(forProperties: properties)
        if properties.contains(.streamSinkBufferQueueSize) { p.sinkBufferQueueSize = 2 }
        if properties.contains(.streamSinkBuffersRequiredForStartup) { p.sinkBuffersRequiredForStartup = 1 }
        return p
    }

    override func authorizedToStartStream(for client: CMIOExtensionClient) -> Bool {
        self.client = client
        return true
    }

    override func startStream() throws {
        guard let c = client else { return }
        device.startSink(client: c)
    }

    override func stopStream() throws { device.stopSink() }
}
