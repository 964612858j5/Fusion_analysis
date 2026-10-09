#version 330 core

// Block A9 §35 (Odon: one quad per tile, here one INSTANCE per tile): the
// array-stored planes of one channel at one level are drawn by a single
// instanced call. Per instance: the plane's world rect, its valid world
// rect, its texel size (w, h) and its array layer. The quad is the plane's
// footprint mapped like the fragment maps pixels (x right, world y DOWN the
// screen), padded one physical pixel and clamped -- the same conservative
// rectangle `_plane_quad_ndc` gives the per-plane path. The fragment still
// decides every pixel exactly.
layout(location = 0) in vec4 a_plane_rect;
layout(location = 1) in vec4 a_valid_rect;
layout(location = 2) in vec2 a_size;
layout(location = 3) in int a_layer;

uniform vec4 u_view_rect;
uniform vec2 u_target_size;

flat out vec4 v_plane_rect;
flat out vec4 v_valid_rect;
flat out vec2 v_size;
flat out int v_layer;
out vec2 v_screen_uv;

void main() {
    const vec2 corners[6] = vec2[6](
        vec2(0.0, 0.0), vec2(1.0, 0.0), vec2(0.0, 1.0),
        vec2(1.0, 0.0), vec2(1.0, 1.0), vec2(0.0, 1.0)
    );
    float sx = 2.0 / max(u_view_rect.y - u_view_rect.x, 1e-12);
    float sy = 2.0 / max(u_view_rect.w - u_view_rect.z, 1e-12);
    vec2 pad = 2.0 / max(u_target_size, vec2(1.0));
    float x0 = clamp((a_plane_rect.x - u_view_rect.x) * sx - 1.0 - pad.x, -1.0, 1.0);
    float x1 = clamp((a_plane_rect.y - u_view_rect.x) * sx - 1.0 + pad.x, -1.0, 1.0);
    float y0 = clamp((u_view_rect.w - a_plane_rect.w) * sy - 1.0 - pad.y, -1.0, 1.0);
    float y1 = clamp((u_view_rect.w - a_plane_rect.z) * sy - 1.0 + pad.y, -1.0, 1.0);
    vec2 c = corners[gl_VertexID];
    vec2 position = vec2(mix(x0, x1, c.x), mix(y0, y1, c.y));
    v_plane_rect = a_plane_rect;
    v_valid_rect = a_valid_rect;
    v_size = a_size;
    v_layer = a_layer;
    v_screen_uv = 0.5 * (position + 1.0);
    gl_Position = vec4(position, 0.0, 1.0);
}
