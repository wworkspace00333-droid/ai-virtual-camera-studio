// GPU scaling between IOSurface pixel buffers (MPS bilinear). Used for the AI letterbox
// (1080p -> 640x640 with gray bars) and for preview thumbnails, replacing CPU resizes.
import CoreVideo
import Foundation
import Metal
import MetalPerformanceShaders

/// Scale `src` into the rectangle (x, y, w, h) of `dst`; the rest of dst is filled with `fill` (0..1 gray).
/// fill < 0 leaves the rest of dst untouched. Returns 0 on success.
@_cdecl("vcs_scale")
public func vcs_scale(_ h: UnsafeMutableRawPointer?, _ srcPtr: UnsafeMutableRawPointer?, _ dstPtr: UnsafeMutableRawPointer?,
                      _ x: Int32, _ y: Int32, _ w: Int32, _ hgt: Int32, _ fill: Float) -> Int32 {
    return autoreleasepool { () -> Int32 in
        guard let h = h, let srcPtr = srcPtr, let dstPtr = dstPtr, w > 0, hgt > 0 else { return -1 }
        let ctx = Unmanaged<VCSContext>.fromOpaque(h).takeUnretainedValue()
        let srcPB = Unmanaged<CVPixelBuffer>.fromOpaque(srcPtr).takeUnretainedValue()
        let dstPB = Unmanaged<CVPixelBuffer>.fromOpaque(dstPtr).takeUnretainedValue()
        guard let (src, sref) = ctx.texture(for: srcPB), let (dst, dref) = ctx.texture(for: dstPB) else { return -2 }
        guard let cb = ctx.queue.makeCommandBuffer() else { return -3 }
        if fill >= 0 {
            let rp = MTLRenderPassDescriptor()
            rp.colorAttachments[0].texture = dst
            rp.colorAttachments[0].loadAction = .clear
            rp.colorAttachments[0].storeAction = .store
            rp.colorAttachments[0].clearColor = MTLClearColor(red: Double(fill), green: Double(fill), blue: Double(fill), alpha: 1)
            cb.makeRenderCommandEncoder(descriptor: rp)?.endEncoding()
        }
        let scaler = MPSImageBilinearScale(device: ctx.device)
        // MPS positions the scaled image at the clipRect origin; a translation in the transform would be
        // applied on top of it (measured: content landed at 2*y). So the transform only scales.
        var t = MPSScaleTransform(scaleX: Double(w) / Double(src.width), scaleY: Double(hgt) / Double(src.height),
                                  translateX: 0, translateY: 0)
        withUnsafePointer(to: &t) { p in
            scaler.scaleTransform = p
            scaler.clipRect = MTLRegionMake2D(Int(x), Int(y), Int(w), Int(hgt))
            scaler.encode(commandBuffer: cb, sourceTexture: src, destinationTexture: dst)
        }
        cb.commit()
        cb.waitUntilCompleted()
        _ = sref; _ = dref
        return cb.error == nil ? 0 : -4
    }
}

/// Release cached GPU resources (texture cache entries, blur targets, texture slots).
@_cdecl("vcs_flush")
public func vcs_flush(_ h: UnsafeMutableRawPointer?, _ dropSlots: Int32) {
    autoreleasepool {
        guard let h = h else { return }
        let ctx = Unmanaged<VCSContext>.fromOpaque(h).takeUnretainedValue()
        if let c = ctx.cache { CVMetalTextureCacheFlush(c, 0) }
        ctx.blurCache.removeAll()
        if dropSlots != 0 { ctx.slots.removeAll() }
    }
}
