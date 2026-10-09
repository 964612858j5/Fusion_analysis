#version 330 core

out vec2 v_screen_uv;

// Block A9 §32 (Odon: one quad per tile). 0 (the default): the fullscreen
// triangle every pass has always used. 1: a rectangle `u_quad_ndc`
// (x0, x1, y0, y1 in NDC) -- a plane pass rasterises only its own
// (conservatively padded) footprint. The plane programs address pixels by
// gl_FragCoord, so WHICH fragments pass and what they compute does not
// depend on the geometry; the quad only skips fragments that would have
// been discarded anyway.
uniform int u_quad;
uniform vec4 u_quad_ndc;

void main() {
    const vec2 positions[3] = vec2[3](
        vec2(-1.0, -1.0),
        vec2( 3.0, -1.0),
        vec2(-1.0,  3.0)
    );
    const vec2 corners[6] = vec2[6](
        vec2(0.0, 0.0), vec2(1.0, 0.0), vec2(0.0, 1.0),
        vec2(1.0, 0.0), vec2(1.0, 1.0), vec2(0.0, 1.0)
    );
    vec2 position;
    if (u_quad == 1) {
        vec2 c = corners[gl_VertexID];
        position = vec2(mix(u_quad_ndc.x, u_quad_ndc.y, c.x),
                        mix(u_quad_ndc.z, u_quad_ndc.w, c.y));
    } else {
        position = positions[gl_VertexID];
    }
    v_screen_uv = 0.5 * (position + 1.0);
    gl_Position = vec4(position, 0.0, 1.0);
}
