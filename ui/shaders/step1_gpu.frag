#version 330 core

in vec2 v_screen_uv;
out vec4 out_rgba;

#ifdef PASS_SOURCE
uniform sampler2D u_raw;
uniform vec4 u_view_rect;
uniform vec4 u_plane_rect;
uniform vec3 u_mapping;
uniform vec2 u_target_size;     // A9 §32: pixel centres by gl_FragCoord

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
