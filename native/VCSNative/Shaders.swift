// Metal shaders, compiled at runtime by the system Metal compiler (no Xcode needed to build).
let vcsShaderSource = #"""
#include <metal_stdlib>
using namespace metal;

// Must match LAYER_FLOATS layout in vcstudio/native/metal.py
struct Layer {
    float4 bbox;      // x0 y0 x1 y1 (frame px)
    float4 inv;       // a b c d   : asset = [a b; c d] * (pos) + t
    float4 tsz;       // tx ty assetW assetH
    float4 bg;        // premultiplied fill (rgba, 0..1)
    float4 params;    // effect params
    float4 maskRect;  // x0 y0 x1 y1 where the mask texture lies (frame px)
    float4 misc;      // opacity, mode (0 composite, 1 effect), effect (1 blur, 2 pixelate, 3 colour), hasMask
};

struct VOut { float4 position [[position]]; };

vertex VOut vcs_quad(uint vid [[vertex_id]], constant Layer& L [[buffer(0)]], constant float2& frame [[buffer(1)]]) {
    float2 corners[4] = { float2(L.bbox.x, L.bbox.y), float2(L.bbox.z, L.bbox.y),
                          float2(L.bbox.x, L.bbox.w), float2(L.bbox.z, L.bbox.w) };
    float2 p = corners[vid];
    VOut o;
    o.position = float4(p.x / frame.x * 2.0 - 1.0, 1.0 - p.y / frame.y * 2.0, 0.0, 1.0);
    return o;
}

fragment float4 vcs_layer(VOut in [[stage_in]], constant Layer& L [[buffer(0)]],
                          texture2d<float> asset [[texture(0)]], texture2d<float> mask [[texture(1)]],
                          texture2d<float> src [[texture(2)]], texture2d<float> blurred [[texture(3)]]) {
    constexpr sampler lin(filter::linear, address::clamp_to_zero, coord::normalized);
    float2 pos = in.position.xy;          // pixel centre (x + 0.5)
    float2 pix = pos - 0.5;               // integer pixel index space (OpenCV convention)
    float k = L.misc.x;
    if (L.misc.w > 0.5) {
        float2 mu = (pos - L.maskRect.xy) / (L.maskRect.zw - L.maskRect.xy);
        k *= mask.sample(lin, mu).r;
    }
    if (k <= 0.0) discard_fragment();
    int mode = int(L.misc.y + 0.5);
    if (mode == 0) {
        float2 uv = float2(L.inv.x * pix.x + L.inv.y * pix.y + L.tsz.x,
                           L.inv.z * pix.x + L.inv.w * pix.y + L.tsz.y);
        float4 a = asset.sample(lin, (uv + 0.5) / L.tsz.zw);
        float4 inner = a + L.bg * (1.0 - a.a);
        return float4(inner.rgb * k, inner.a * k);
    }
    int effect = int(L.misc.z + 0.5);
    float3 c;
    uint2 ip = uint2(pix);
    if (effect == 1) {
        c = blurred.read(ip).rgb;
    } else if (effect == 2) {
        float b = max(L.params.x, 1.0);
        float2 cell = floor(pix / b) * b + floor(b * 0.5);
        cell = clamp(cell, float2(0.0), float2(src.get_width() - 1, src.get_height() - 1));
        c = src.read(uint2(cell)).rgb;
    } else {
        c = src.read(ip).rgb;
        c = (c - 0.5) * L.params.y + 0.5 + L.params.x;            // contrast, brightness
        float lum = dot(c, float3(0.2126, 0.7152, 0.0722));        // texture is presented as RGBA
        c = mix(float3(lum), c, L.params.z);                       // saturation
        c = clamp(c, 0.0, 1.0);
    }
    return float4(c * k, k);
}
"""#
