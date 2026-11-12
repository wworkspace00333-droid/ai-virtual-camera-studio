// CMIO sink client: pushes processed frames into the "AI Virtual Camera" extension's sink stream.
// Uses the CoreMediaIO C API (the same mechanism OBS uses). Frames are IOSurface CVPixelBuffers
// wrapped in CMSampleBuffers; they reach the extension by mach port without pixel copies.
// Sending never blocks: if the extension's queue is full, the frame is dropped and counted.
import CoreMedia
import CoreMediaIO
import CoreVideo
import Foundation
import VideoToolbox

final class VCSCameraSink {
    let deviceID: CMIOObjectID
    let streamID: CMIOStreamID
    var queue: CMSimpleQueue?
    var formatDesc: CMFormatDescription?
    var sent: Int64 = 0
    var dropped: Int64 = 0
    var started = false
    // Output pixel format sent to the camera. 0 = as rendered (BGRA). '420v' (NV12, video range) is what
    // Chrome asks every camera for: it ignores BGRA camera formats and received no frames from a BGRA feed.
    var outFormat: OSType = 0
    var transfer: VTPixelTransferSession?
    var pool: CVPixelBufferPool?
    var poolKey: (Int, Int, OSType) = (0, 0, 0)

    init(deviceID: CMIOObjectID, streamID: CMIOStreamID) {
        self.deviceID = deviceID
        self.streamID = streamID
    }
}

private func cmioName(_ obj: CMIOObjectID) -> String? {
    var addr = CMIOObjectPropertyAddress(mSelector: CMIOObjectPropertySelector(kCMIOObjectPropertyName),
                                         mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
                                         mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
    var name: Unmanaged<CFString>?
    var used: UInt32 = 0
    let size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard CMIOObjectGetPropertyData(obj, &addr, 0, nil, size, &used, &name) == 0, let n = name else { return nil }
    return n.takeRetainedValue() as String
}

private func cmioObjects(_ obj: CMIOObjectID, _ selector: Int) -> [CMIOObjectID] {
    var addr = CMIOObjectPropertyAddress(mSelector: CMIOObjectPropertySelector(selector),
                                         mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
                                         mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
    var size: UInt32 = 0
    guard CMIOObjectGetPropertyDataSize(obj, &addr, 0, nil, &size) == 0, size > 0 else { return [] }
    var ids = [CMIOObjectID](repeating: 0, count: Int(size) / MemoryLayout<CMIOObjectID>.size)
    var used: UInt32 = 0
    guard CMIOObjectGetPropertyData(obj, &addr, 0, nil, size, &used, &ids) == 0 else { return [] }
    return ids
}

/// Find the device + sink stream. Returns 0 on success, -1 device missing, -2 sink stream missing.
private func findSink(_ deviceName: String, _ sinkName: String) -> (CMIOObjectID, CMIOStreamID, Int32) {
    // Let CMIO see camera-extension (DAL-less) devices from this process.
    var allow: UInt32 = 1
    var allowAddr = CMIOObjectPropertyAddress(mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
                                              mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
                                              mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
    CMIOObjectSetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &allowAddr, 0, nil, UInt32(MemoryLayout<UInt32>.size), &allow)
    for dev in cmioObjects(CMIOObjectID(kCMIOObjectSystemObject), Int(kCMIOHardwarePropertyDevices)) {
        guard cmioName(dev) == deviceName else { continue }
        for s in cmioObjects(dev, Int(kCMIODevicePropertyStreams)) where cmioName(s) == sinkName {
            return (dev, s, 0)
        }
        return (dev, 0, -2)
    }
    return (0, 0, -1)
}

@_cdecl("vcs_cam_list_devices")
public func vcs_cam_list_devices(_ out: UnsafeMutablePointer<CChar>?, _ cap: Int32) -> Int32 {
    guard let out = out else { return -1 }
    let names = cmioObjects(CMIOObjectID(kCMIOObjectSystemObject), Int(kCMIOHardwarePropertyDevices)).compactMap { cmioName($0) }
    let joined = names.joined(separator: "\n")
    _ = joined.withCString { strncpy(out, $0, Int(cap) - 1) }
    out[Int(cap) - 1] = 0
    return Int32(names.count)
}

/// Open the sink. status: 0 ok, -1 camera not installed/visible, -2 no sink stream, -3 queue, -4 start failed.
@_cdecl("vcs_cam_open")
public func vcs_cam_open(_ deviceName: UnsafePointer<CChar>?, _ sinkName: UnsafePointer<CChar>?,
                         _ status: UnsafeMutablePointer<Int32>?) -> UnsafeMutableRawPointer? {
    let dn = deviceName.map { String(cString: $0) } ?? "AI Virtual Camera"
    let sn = sinkName.map { String(cString: $0) } ?? "AI Virtual Camera Input"
    let (dev, stream, rc) = findSink(dn, sn)
    guard rc == 0 else { status?.pointee = rc; return nil }
    let sink = VCSCameraSink(deviceID: dev, streamID: stream)
    var q: Unmanaged<CMSimpleQueue>?
    let r = CMIOStreamCopyBufferQueue(stream, { _, _, _ in }, nil, &q)
    guard r == 0, let queue = q?.takeRetainedValue() else { status?.pointee = -3; return nil }
    sink.queue = queue
    guard CMIODeviceStartStream(dev, stream) == 0 else { status?.pointee = -4; return nil }
    sink.started = true
    status?.pointee = 0
    return Unmanaged.passRetained(sink).toOpaque()
}

/// Enqueue one frame. Returns 0 sent, 1 dropped (queue full), <0 error.
@_cdecl("vcs_cam_send")
public func vcs_cam_send(_ h: UnsafeMutableRawPointer?, _ pbPtr: UnsafeMutableRawPointer?, _ ptsSeconds: Double) -> Int32 {
    return autoreleasepool { vcs_cam_send_impl(h, pbPtr, ptsSeconds) }
}

/// Choose the pixel format sent to the camera: 0 = as rendered, or a CoreVideo format such as '420v'.
@_cdecl("vcs_cam_set_format")
public func vcs_cam_set_format(_ h: UnsafeMutableRawPointer?, _ fourcc: UInt32) {
    guard let h = h else { return }
    let sink = Unmanaged<VCSCameraSink>.fromOpaque(h).takeUnretainedValue()
    sink.outFormat = fourcc
    sink.formatDesc = nil
}

/// GPU conversion (VideoToolbox) of a rendered frame into the camera's output format; nil on failure.
private func converted(_ sink: VCSCameraSink, _ src: CVPixelBuffer) -> CVPixelBuffer? {
    let w = CVPixelBufferGetWidth(src), h = CVPixelBufferGetHeight(src), f = sink.outFormat
    if sink.pool == nil || sink.poolKey != (w, h, f) {
        let attrs: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: f,
                                    kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h,
                                    kCVPixelBufferIOSurfacePropertiesKey as String: [:]]
        var p: CVPixelBufferPool?
        CVPixelBufferPoolCreate(kCFAllocatorDefault, [kCVPixelBufferPoolMinimumBufferCountKey: 6] as CFDictionary,
                                attrs as CFDictionary, &p)
        sink.pool = p
        sink.poolKey = (w, h, f)
    }
    if sink.transfer == nil {
        var t: VTPixelTransferSession?
        VTPixelTransferSessionCreate(allocator: kCFAllocatorDefault, pixelTransferSessionOut: &t)
        if let t = t {
            VTSessionSetProperty(t, key: kVTPixelTransferPropertyKey_DestinationYCbCrMatrix,
                                 value: kCVImageBufferYCbCrMatrix_ITU_R_709_2)
            VTSessionSetProperty(t, key: kVTPixelTransferPropertyKey_DestinationColorPrimaries,
                                 value: kCVImageBufferColorPrimaries_ITU_R_709_2)
            VTSessionSetProperty(t, key: kVTPixelTransferPropertyKey_DestinationTransferFunction,
                                 value: kCVImageBufferTransferFunction_ITU_R_709_2)
        }
        sink.transfer = t
    }
    guard let pool = sink.pool, let t = sink.transfer else { return nil }
    var out: CVPixelBuffer?
    guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &out) == kCVReturnSuccess, let o = out,
          VTPixelTransferSessionTransferImage(t, from: src, to: o) == noErr else { return nil }
    return o
}

func vcs_cam_send_impl(_ h: UnsafeMutableRawPointer?, _ pbPtr: UnsafeMutableRawPointer?, _ ptsSeconds: Double) -> Int32 {
    guard let h = h, let pbPtr = pbPtr else { return -1 }
    let sink = Unmanaged<VCSCameraSink>.fromOpaque(h).takeUnretainedValue()
    guard let queue = sink.queue else { return -2 }
    let rendered = Unmanaged<CVPixelBuffer>.fromOpaque(pbPtr).takeUnretainedValue()
    if CMSimpleQueueGetCount(queue) >= CMSimpleQueueGetCapacity(queue) {
        sink.dropped += 1
        return 1
    }
    var pb = rendered
    if sink.outFormat != 0 && CVPixelBufferGetPixelFormatType(rendered) != sink.outFormat {
        guard let c = converted(sink, rendered) else { return -5 }
        pb = c
    }
    if sink.formatDesc == nil || !CMVideoFormatDescriptionMatchesImageBuffer(sink.formatDesc!, imageBuffer: pb) {
        var fd: CMFormatDescription?
        CMVideoFormatDescriptionCreateForImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: pb, formatDescriptionOut: &fd)
        sink.formatDesc = fd
    }
    guard let fd = sink.formatDesc else { return -3 }
    var timing = CMSampleTimingInfo(duration: .invalid,
                                    presentationTimeStamp: CMTime(seconds: ptsSeconds, preferredTimescale: 1_000_000_000),
                                    decodeTimeStamp: .invalid)
    var sbuf: CMSampleBuffer?
    guard CMSampleBufferCreateForImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: pb, dataReady: true,
                                             makeDataReadyCallback: nil, refcon: nil, formatDescription: fd,
                                             sampleTiming: &timing, sampleBufferOut: &sbuf) == noErr,
          let s = sbuf else { return -4 }
    let raw = Unmanaged.passRetained(s).toOpaque()        // the queue consumer releases it
    if CMSimpleQueueEnqueue(queue, element: raw) != noErr {
        Unmanaged<CMSampleBuffer>.fromOpaque(raw).release()
        sink.dropped += 1
        return 1
    }
    sink.sent += 1
    return 0
}

@_cdecl("vcs_cam_stats")
public func vcs_cam_stats(_ h: UnsafeMutableRawPointer?, _ out: UnsafeMutablePointer<Int64>?) {
    guard let h = h, let out = out else { return }
    let sink = Unmanaged<VCSCameraSink>.fromOpaque(h).takeUnretainedValue()
    out[0] = sink.sent
    out[1] = sink.dropped
}

@_cdecl("vcs_cam_close")
public func vcs_cam_close(_ h: UnsafeMutableRawPointer?) {
    guard let h = h else { return }
    let sink = Unmanaged<VCSCameraSink>.fromOpaque(h).takeRetainedValue()
    if sink.started { CMIODeviceStopStream(sink.deviceID, sink.streamID) }
    sink.queue = nil
}

@_cdecl("vcs_cam_list_streams")
public func vcs_cam_list_streams(_ deviceName: UnsafePointer<CChar>?, _ out: UnsafeMutablePointer<CChar>?, _ cap: Int32) -> Int32 {
    guard let deviceName = deviceName, let out = out else { return -1 }
    let dn = String(cString: deviceName)
    for dev in cmioObjects(CMIOObjectID(kCMIOObjectSystemObject), Int(kCMIOHardwarePropertyDevices)) where cmioName(dev) == dn {
        let names = cmioObjects(dev, Int(kCMIODevicePropertyStreams)).map { cmioName($0) ?? "?" }
        _ = names.joined(separator: "\n").withCString { strncpy(out, $0, Int(cap) - 1) }
        out[Int(cap) - 1] = 0
        return Int32(names.count)
    }
    return -1
}
