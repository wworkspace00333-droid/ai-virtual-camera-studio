// libVCSNative: GPU compositor exposed through a C ABI (called from Python with ctypes).
//
// Frames are IOSurface-backed CVPixelBuffers mapped to Metal textures through CVMetalTextureCache
// (no pixel copies). Asset and mask textures live in numbered slots that Python uploads only when
// their content changes. One render = blit(src -> dst) + optional MPS Gaussian blurs of src +
// one instanced quad per layer with hardware premultiplied blending (one, 1 - srcAlpha; alpha kept).
import CoreVideo
import Foundation
import Metal
import MetalPerformanceShaders

let LAYER_FLOATS = 28

final class VCSContext {
    let device: MTLDevice
    let queue: MTLCommandQueue
    let pipeline: MTLRenderPipelineState
    var cache: CVMetalTextureCache?
    var slots: [Int32: MTLTexture] = [:]
    var blurCache: [Int: MTLTexture] = [:]
    var lastError = ""

    init?() {
        guard let d = MTLCreateSystemDefaultDevice(), let q = d.makeCommandQueue() else { return nil }
        device = d
        queue = q
        do {
            let lib = try d.makeLibrary(source: vcsShaderSource, options: nil)
            let desc = MTLRenderPipelineDescriptor()
            desc.vertexFunction = lib.makeFunction(name: "vcs_quad")
            desc.fragmentFunction = lib.makeFunction(name: "vcs_layer")
            let att = desc.colorAttachments[0]!
            att.pixelFormat = .bgra8Unorm
            att.isBlendingEnabled = true
            att.rgbBlendOperation = .add
            att.sourceRGBBlendFactor = .one
            att.destinationRGBBlendFactor = .oneMinusSourceAlpha
            att.alphaBlendOperation = .add
            att.sourceAlphaBlendFactor = .zero          // keep the frame's alpha
            att.destinationAlphaBlendFactor = .one
            pipeline = try d.makeRenderPipelineState(descriptor: desc)
        } catch {
            NSLog("VCSNative: pipeline creation failed: \(error)")
            return nil
        }
        var c: CVMetalTextureCache?
        CVMetalTextureCacheCreate(nil, nil, d, nil, &c)
        cache = c
    }

    func texture(for pb: CVPixelBuffer) -> (MTLTexture, CVMetalTexture)? {
        guard let cache = cache else { return nil }
        let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
        var cvt: CVMetalTexture?
        let r = CVMetalTextureCacheCreateTextureFromImage(nil, cache, pb, nil, .bgra8Unorm, w, h, 0, &cvt)
        guard r == kCVReturnSuccess, let t = cvt, let tex = CVMetalTextureGetTexture(t) else { return nil }
        return (tex, t)
    }
}

@_cdecl("vcs_create")
public func vcs_create() -> UnsafeMutableRawPointer? {
    guard let ctx = VCSContext() else { return nil }
    return Unmanaged.passRetained(ctx).toOpaque()
}

@_cdecl("vcs_destroy")
public func vcs_destroy(_ h: UnsafeMutableRawPointer?) {
    guard let h = h else { return }
    Unmanaged<VCSContext>.fromOpaque(h).release()
}

@_cdecl("vcs_device_name")
public func vcs_device_name(_ h: UnsafeMutableRawPointer?, _ out: UnsafeMutablePointer<CChar>?, _ cap: Int32) {
    guard let h = h, let out = out else { return }
    let ctx = Unmanaged<VCSContext>.fromOpaque(h).takeUnretainedValue()
    _ = ctx.device.name.withCString { strncpy(out, $0, Int(cap) - 1) }
    out[Int(cap) - 1] = 0
}

@_cdecl("vcs_last_error")
public func vcs_last_error(_ h: UnsafeMutableRawPointer?, _ out: UnsafeMutablePointer<CChar>?, _ cap: Int32) {
    guard let h = h, let out = out else { return }
    let ctx = Unmanaged<VCSContext>.fromOpaque(h).takeUnretainedValue()
    _ = ctx.lastError.withCString { strncpy(out, $0, Int(cap) - 1) }
    out[Int(cap) - 1] = 0
}

/// Upload pixels into a texture slot. format 0 = BGRA8 (premultiplied), 1 = R8 (mask).
@_cdecl("vcs_upload")
public func vcs_upload(_ h: UnsafeMutableRawPointer?, _ slot: Int32, _ data: UnsafeRawPointer?,
                       _ width: Int32, _ height: Int32, _ stride: Int32, _ format: Int32) -> Int32 {
    return autoreleasepool { vcs_upload_impl(h, slot, data, width, height, stride, format) }
}

func vcs_upload_impl(_ h: UnsafeMutableRawPointer?, _ slot: Int32, _ data: UnsafeRawPointer?,
                       _ width: Int32, _ height: Int32, _ stride: Int32, _ format: Int32) -> Int32 {
    guard let h = h, let data = data, width > 0, height > 0 else { return -1 }
    let ctx = Unmanaged<VCSContext>.fromOpaque(h).takeUnretainedValue()
    let pf: MTLPixelFormat = format == 1 ? .r8Unorm : .bgra8Unorm
    var tex = ctx.slots[slot]
    if tex == nil || tex!.width != Int(width) || tex!.height != Int(height) || tex!.pixelFormat != pf {
        let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: pf, width: Int(width), height: Int(height), mipmapped: false)
        d.usage = [.shaderRead]
        d.storageMode = .shared
        tex = ctx.device.makeTexture(descriptor: d)
        ctx.slots[slot] = tex
    }
    guard let t = tex else { return -2 }
    t.replace(region: MTLRegionMake2D(0, 0, Int(width), Int(height)), mipmapLevel: 0, withBytes: data, bytesPerRow: Int(stride))
    return 0
}

@_cdecl("vcs_release_slot")
public func vcs_release_slot(_ h: UnsafeMutableRawPointer?, _ slot: Int32) {
    guard let h = h else { return }
    Unmanaged<VCSContext>.fromOpaque(h).takeUnretainedValue().slots.removeValue(forKey: slot)
}

/// Composite `n` layers over `src` into `dst` (both BGRA CVPixelBuffers of equal size).
/// layers: n * LAYER_FLOATS floats; slots: n * 2 ints (asset slot, mask slot; -1 = none).
/// timing (optional, 2 doubles): CPU encode+wait ms, GPU execution ms.
@_cdecl("vcs_render")
public func vcs_render(_ h: UnsafeMutableRawPointer?, _ srcPtr: UnsafeMutableRawPointer?, _ dstPtr: UnsafeMutableRawPointer?,
                       _ layers: UnsafePointer<Float>?, _ slots: UnsafePointer<Int32>?, _ n: Int32,
                       _ timing: UnsafeMutablePointer<Double>?) -> Int32 {
    // Called from Python threads that have no autorelease pool: drain one per call.
    return autoreleasepool { vcs_render_impl(h, srcPtr, dstPtr, layers, slots, n, timing) }
}

func vcs_render_impl(_ h: UnsafeMutableRawPointer?, _ srcPtr: UnsafeMutableRawPointer?, _ dstPtr: UnsafeMutableRawPointer?,
                       _ layers: UnsafePointer<Float>?, _ slots: UnsafePointer<Int32>?, _ n: Int32,
                       _ timing: UnsafeMutablePointer<Double>?) -> Int32 {
    guard let h = h, let srcPtr = srcPtr, let dstPtr = dstPtr else { return -1 }
    let ctx = Unmanaged<VCSContext>.fromOpaque(h).takeUnretainedValue()
    let t0 = CFAbsoluteTimeGetCurrent()
    let srcPB = Unmanaged<CVPixelBuffer>.fromOpaque(srcPtr).takeUnretainedValue()
    let dstPB = Unmanaged<CVPixelBuffer>.fromOpaque(dstPtr).takeUnretainedValue()
    guard let (src, srcRef) = ctx.texture(for: srcPB), let (dst, dstRef) = ctx.texture(for: dstPB) else {
        ctx.lastError = "cannot map pixel buffers to Metal textures (IOSurface-backed BGRA required)"
        return -2
    }
    guard src.width == dst.width, src.height == dst.height else {
        ctx.lastError = "source and destination sizes differ"
        return -3
    }
    guard let cb = ctx.queue.makeCommandBuffer() else { return -4 }
    if let blit = cb.makeBlitCommandEncoder() {
        blit.copy(from: src, to: dst)
        blit.endEncoding()
    }
    // Gaussian blurs needed by blur layers (one per distinct sigma), cached textures per sigma.
    var blurred: [Int: MTLTexture] = [:]
    let count = Int(n)
    if count > 0, let L = layers {
        for i in 0..<count {
            let base = i * LAYER_FLOATS
            if Int(L[base + 24 + 1] + 0.5) == 1 && Int(L[base + 24 + 2] + 0.5) == 1 {
                let sigma = max(0.5, L[base + 16])
                let key = Int(sigma * 100)
                if blurred[key] != nil { continue }
                var tex = ctx.blurCache[key]
                if tex == nil || tex!.width != src.width || tex!.height != src.height {
                    let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: src.width,
                                                                     height: src.height, mipmapped: false)
                    d.usage = [.shaderRead, .shaderWrite]
                    d.storageMode = .private
                    tex = ctx.device.makeTexture(descriptor: d)
                    ctx.blurCache[key] = tex
                }
                MPSImageGaussianBlur(device: ctx.device, sigma: sigma).encode(commandBuffer: cb, sourceTexture: src,
                                                                             destinationTexture: tex!)
                blurred[key] = tex
            }
        }
        let rp = MTLRenderPassDescriptor()
        rp.colorAttachments[0].texture = dst
        rp.colorAttachments[0].loadAction = .load
        rp.colorAttachments[0].storeAction = .store
        guard let enc = cb.makeRenderCommandEncoder(descriptor: rp) else { return -5 }
        enc.setRenderPipelineState(ctx.pipeline)
        var frame = SIMD2<Float>(Float(dst.width), Float(dst.height))
        enc.setVertexBytes(&frame, length: MemoryLayout<SIMD2<Float>>.stride, index: 1)
        for i in 0..<count {
            let base = i * LAYER_FLOATS
            let ptr = L.advanced(by: base)
            enc.setVertexBytes(ptr, length: LAYER_FLOATS * 4, index: 0)
            enc.setFragmentBytes(ptr, length: LAYER_FLOATS * 4, index: 0)
            let assetSlot = slots?[i * 2] ?? -1
            let maskSlot = slots?[i * 2 + 1] ?? -1
            enc.setFragmentTexture(assetSlot >= 0 ? ctx.slots[assetSlot] : nil, index: 0)
            enc.setFragmentTexture(maskSlot >= 0 ? ctx.slots[maskSlot] : nil, index: 1)
            enc.setFragmentTexture(src, index: 2)
            let sigmaKey = Int(max(0.5, L[base + 16]) * 100)
            enc.setFragmentTexture(blurred[sigmaKey] ?? src, index: 3)
            enc.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
        }
        enc.endEncoding()
    }
    cb.commit()
    cb.waitUntilCompleted()
    _ = srcRef; _ = dstRef   // keep CVMetalTextures alive until the GPU is done
    if let err = cb.error {
        ctx.lastError = "\(err)"
        return -6
    }
    if let t = timing {
        t[0] = (CFAbsoluteTimeGetCurrent() - t0) * 1000
        t[1] = (cb.gpuEndTime - cb.gpuStartTime) * 1000
    }
    return 0
}
