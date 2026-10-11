#version 330 core

in vec2 v_screen_uv;
out vec4 out_rgba;

#if defined(PASS_SOURCE) || defined(PASS_SOURCE_UINT) || defined(PASS_ARRAY_UINT) || defined(PASS_ARRAY_FLOAT)
// Block A9 §38: "Smooth" (default on). The level and the pixel's validity
// are decided exactly as without it, by the nearest sample; then the raw
// value -- where a texel is wider than a screen pixel; elsewhere nothing
// changes -- is the bilinear mix of the four texels around the pixel, each used
// only when valid (inside this plane, inside its valid rect, not NaN/Inf),
// the weights renormalised. Raw values are mixed, the mapping comes after.
// In these per-plane passes a tap across the tile edge is not reachable and
// is dropped; the single-pass compositor (step1_gpu_vt.frag) reaches it.
uniform int u_smooth;
bool raw_tap(ivec2 texel, ivec2 size, out float raw);

float smoothed_raw(vec2 uv, ivec2 size, float nearest, vec2 plane_size,
                   vec4 view_rect, vec2 target_size) {
    // only where a texel is wider than a screen pixel (as the single pass)
    if (plane_size.x * target_size.x <= float(size.x) * (view_rect.y - view_rect.x)
        && plane_size.y * target_size.y <= float(size.y) * (view_rect.w - view_rect.z)) {
        return nearest;
    }
    vec2 p = uv * vec2(size) - 0.5;
    vec2 base = floor(p);
    vec2 f = p - base;
    ivec2 i0 = ivec2(base);
    float total = 0.0;
    float weights = 0.0;
    for (int j = 0; j < 4; j++) {
        ivec2 d = ivec2(j & 1, j >> 1);
        float w = (d.x == 1 ? f.x : 1.0 - f.x) * (d.y == 1 ? f.y : 1.0 - f.y);
        if (w <= 0.0) {
            continue;
        }
        ivec2 t = i0 + d;
        if (t.x < 0 || t.y < 0 || t.x >= size.x || t.y >= size.y) {
            continue;
        }
        float raw;
        if (raw_tap(t, size, raw)) {
            total += w * raw;
            weights += w;
        }
    }
    return weights > 0.0 ? total / weights : nearest;
}
#endif

#ifdef PASS_SOURCE
uniform sampler2D u_raw;
uniform vec4 u_view_rect;
uniform vec4 u_plane_rect;
uniform vec3 u_mapping;
uniform vec2 u_target_size;     // A9 §32: pixel centres by gl_FragCoord

bool raw_tap(ivec2 texel, ivec2 size, out float raw) {
    raw = texelFetch(u_raw, texel, 0).r;
    return !(isnan(raw) || isinf(raw));
}

void main() {
    vec2 screen_uv = gl_FragCoord.xy / u_target_size;
    vec2 view_size = vec2(u_view_rect.y - u_view_rect.x,
                          u_view_rect.w - u_view_rect.z);
    vec2 world = vec2(u_view_rect.x + screen_uv.x * view_size.x,
                      u_view_rect.w - screen_uv.y * view_size.y);
    if (world.x < u_plane_rect.x || world.x >= u_plane_rect.y ||
        world.y < u_plane_rect.z || world.y >= u_plane_rect.w) {
        discard;
    }
    vec2 plane_size = vec2(u_plane_rect.y - u_plane_rect.x,
                           u_plane_rect.w - u_plane_rect.z);
    vec2 uv = vec2((world.x - u_plane_rect.x) / plane_size.x,
                   (world.y - u_plane_rect.z) / plane_size.y);
    // A9 §35: the texel is chosen explicitly -- the arithmetic the array
    // path uses, so both paths pick the same texel at every boundary
    ivec2 size = textureSize(u_raw, 0);
    ivec2 texel = clamp(ivec2(floor(uv * vec2(size))), ivec2(0), size - 1);
    float raw = texelFetch(u_raw, texel, 0).r;
    if (isnan(raw) || isinf(raw)) {
        discard;
    }
    if (u_smooth != 0) {
        raw = smoothed_raw(uv, size, raw, plane_size, u_view_rect, u_target_size);
    }
    float denominator = u_mapping.y - u_mapping.x;
    float signal = 0.0;
    if (denominator > 0.0) {
        signal = pow(clamp((raw - u_mapping.x) / denominator, 0.0, 1.0),
                     max(u_mapping.z, 0.000001));
    }
    out_rgba = vec4(clamp(signal, 0.0, 1.0), 0.0, 0.0, 1.0);
}
#endif

#ifdef PASS_SOURCE_UINT
// Block A9 S2c: a raw channel kept in its own integers (R8UI / R16UI). The
// value is the pyramid's integer, exactly; validity is the plane's valid
// rectangle (outside it the texels are 0 = absent, never black). The
// mapping is the same arithmetic as PASS_SOURCE, on the same raw units.
uniform usampler2D u_raw;
uniform vec4 u_view_rect;
uniform vec4 u_plane_rect;
uniform vec4 u_valid_rect;
uniform vec3 u_mapping;
uniform vec2 u_target_size;     // A9 §32: pixel centres by gl_FragCoord

bool raw_tap(ivec2 texel, ivec2 size, out float raw) {
    // the texel's centre must lie in the valid rect
    vec2 c = vec2(u_plane_rect.x, u_plane_rect.z)
             + (vec2(texel) + 0.5) * vec2(u_plane_rect.y - u_plane_rect.x,
                                          u_plane_rect.w - u_plane_rect.z) / vec2(size);
    raw = 0.0;
    if (c.x < u_valid_rect.x || c.x >= u_valid_rect.y ||
        c.y < u_valid_rect.z || c.y >= u_valid_rect.w) {
        return false;
    }
    raw = float(texelFetch(u_raw, texel, 0).r);
    return true;
}

void main() {
    vec2 screen_uv = gl_FragCoord.xy / u_target_size;
    vec2 view_size = vec2(u_view_rect.y - u_view_rect.x,
                          u_view_rect.w - u_view_rect.z);
    vec2 world = vec2(u_view_rect.x + screen_uv.x * view_size.x,
                      u_view_rect.w - screen_uv.y * view_size.y);
    if (world.x < u_plane_rect.x || world.x >= u_plane_rect.y ||
        world.y < u_plane_rect.z || world.y >= u_plane_rect.w) {
        discard;
    }
    if (world.x < u_valid_rect.x || world.x >= u_valid_rect.y ||
        world.y < u_valid_rect.z || world.y >= u_valid_rect.w) {
        discard;
    }
    vec2 plane_size = vec2(u_plane_rect.y - u_plane_rect.x,
                           u_plane_rect.w - u_plane_rect.z);
    vec2 uv = vec2((world.x - u_plane_rect.x) / plane_size.x,
                   (world.y - u_plane_rect.z) / plane_size.y);
    ivec2 size = textureSize(u_raw, 0);
    ivec2 texel = clamp(ivec2(floor(uv * vec2(size))), ivec2(0), size - 1);
    float raw = float(texelFetch(u_raw, texel, 0).r);
    if (u_smooth != 0) {
        raw = smoothed_raw(uv, size, raw, plane_size, u_view_rect, u_target_size);
    }
    float denominator = u_mapping.y - u_mapping.x;
    float signal = 0.0;
    if (denominator > 0.0) {
        signal = pow(clamp((raw - u_mapping.x) / denominator, 0.0, 1.0),
                     max(u_mapping.z, 0.000001));
    }
    out_rgba = vec4(clamp(signal, 0.0, 1.0), 0.0, 0.0, 1.0);
}
#endif

#if defined(PASS_ARRAY_UINT) || defined(PASS_ARRAY_FLOAT)
// Block A9 §35: one array-stored plane per instance. The same arithmetic as
// PASS_SOURCE / PASS_SOURCE_UINT above, line for line: pixel centre by
// gl_FragCoord, half-open plane and valid rects, the texel by explicit
// index inside the plane's own (w, h) -- never the padding of its layer.
#ifdef PASS_ARRAY_UINT
uniform usampler2DArray u_arr;
#else
uniform sampler2DArray u_arr;
#endif
uniform vec4 u_view_rect;
uniform vec3 u_mapping;
uniform vec2 u_target_size;
flat in vec4 v_plane_rect;
flat in vec4 v_valid_rect;
flat in vec2 v_size;
flat in int v_layer;

bool raw_tap(ivec2 texel, ivec2 size, out float raw) {
#ifdef PASS_ARRAY_UINT
    vec2 c = vec2(v_plane_rect.x, v_plane_rect.z)
             + (vec2(texel) + 0.5) * vec2(v_plane_rect.y - v_plane_rect.x,
                                          v_plane_rect.w - v_plane_rect.z) / vec2(size);
    raw = 0.0;
    if (c.x < v_valid_rect.x || c.x >= v_valid_rect.y ||
        c.y < v_valid_rect.z || c.y >= v_valid_rect.w) {
        return false;
    }
    raw = float(texelFetch(u_arr, ivec3(texel, v_layer), 0).r);
    return true;
#else
    raw = texelFetch(u_arr, ivec3(texel, v_layer), 0).r;
    return !(isnan(raw) || isinf(raw));
#endif
}

void main() {
    vec2 screen_uv = gl_FragCoord.xy / u_target_size;
    vec2 view_size = vec2(u_view_rect.y - u_view_rect.x,
                          u_view_rect.w - u_view_rect.z);
    vec2 world = vec2(u_view_rect.x + screen_uv.x * view_size.x,
                      u_view_rect.w - screen_uv.y * view_size.y);
    if (world.x < v_plane_rect.x || world.x >= v_plane_rect.y ||
        world.y < v_plane_rect.z || world.y >= v_plane_rect.w) {
        discard;
    }
#ifdef PASS_ARRAY_UINT
    if (world.x < v_valid_rect.x || world.x >= v_valid_rect.y ||
        world.y < v_valid_rect.z || world.y >= v_valid_rect.w) {
        discard;
    }
#endif
    vec2 plane_size = vec2(v_plane_rect.y - v_plane_rect.x,
                           v_plane_rect.w - v_plane_rect.z);
    vec2 uv = vec2((world.x - v_plane_rect.x) / plane_size.x,
                   (world.y - v_plane_rect.z) / plane_size.y);
    ivec2 size = ivec2(v_size);
    ivec2 texel = clamp(ivec2(floor(uv * vec2(size))), ivec2(0), size - 1);
#ifdef PASS_ARRAY_UINT
    float raw = float(texelFetch(u_arr, ivec3(texel, v_layer), 0).r);
#else
    float raw = texelFetch(u_arr, ivec3(texel, v_layer), 0).r;
    if (isnan(raw) || isinf(raw)) {
        discard;
    }
#endif
    if (u_smooth != 0) {
        raw = smoothed_raw(uv, size, raw, plane_size, u_view_rect, u_target_size);
    }
    float denominator = u_mapping.y - u_mapping.x;
    float signal = 0.0;
    if (denominator > 0.0) {
        signal = pow(clamp((raw - u_mapping.x) / denominator, 0.0, 1.0),
                     max(u_mapping.z, 0.000001));
    }
    out_rgba = vec4(clamp(signal, 0.0, 1.0), 0.0, 0.0, 1.0);
}
#endif

#ifdef PASS_CONTRIBUTION
uniform sampler2D u_input;
uniform vec3 u_color;
uniform float u_weight;
uniform int u_component;

void main() {
    vec4 signal = texture(u_input, v_screen_uv);
    if (signal.a <= 0.0) {
        discard;
    }
    float value = signal.r * u_weight;
    if (u_component == 0) {
        out_rgba = vec4(value * u_color, signal.a);
    } else if (u_component == 1) {
        out_rgba = vec4(value, 0.0, 0.0, signal.a);
    } else {
        out_rgba = vec4(0.0, 0.0, value, signal.a);
    }
}
#endif

#ifdef PASS_GROUP_RESOLVE
uniform sampler2D u_input;
uniform float u_weight;

void main() {
    vec4 group_sum = texture(u_input, v_screen_uv);
    if (group_sum.a <= 0.0) {
        discard;
    }
    out_rgba = vec4(clamp(group_sum.r * u_weight, 0.0, 1.0),
                    0.0, 0.0, group_sum.a);
}
#endif

#ifdef PASS_FINAL_OVERLAY
uniform sampler2D u_input;

void main() {
    vec4 accum = texture(u_input, v_screen_uv);
    out_rgba = vec4(clamp(accum.rgb, 0.0, 1.0), accum.a > 0.0 ? 1.0 : 0.0);
}
#endif

#ifdef PASS_FINAL_FUSION
uniform sampler2D u_input;

void main() {
    vec4 accum = texture(u_input, v_screen_uv);
    out_rgba = vec4(clamp(accum.r, 0.0, 1.0), 0.0,
                    clamp(accum.b, 0.0, 1.0), accum.a > 0.0 ? 1.0 : 0.0);
}
#endif
