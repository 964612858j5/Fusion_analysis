#version 330 core

in vec2 v_screen_uv;
out vec4 out_rgba;

#ifdef PASS_SOURCE
uniform sampler2D u_raw;
uniform vec4 u_view_rect;
uniform vec4 u_plane_rect;
uniform vec3 u_mapping;

void main() {
    vec2 view_size = vec2(u_view_rect.y - u_view_rect.x,
                          u_view_rect.w - u_view_rect.z);
    vec2 world = vec2(u_view_rect.x + v_screen_uv.x * view_size.x,
                      u_view_rect.w - v_screen_uv.y * view_size.y);
    if (world.x < u_plane_rect.x || world.x >= u_plane_rect.y ||
        world.y < u_plane_rect.z || world.y >= u_plane_rect.w) {
        discard;
    }
    vec2 plane_size = vec2(u_plane_rect.y - u_plane_rect.x,
                           u_plane_rect.w - u_plane_rect.z);
    vec2 uv = vec2((world.x - u_plane_rect.x) / plane_size.x,
                   (world.y - u_plane_rect.z) / plane_size.y);
    float raw = texture(u_raw, uv).r;
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
