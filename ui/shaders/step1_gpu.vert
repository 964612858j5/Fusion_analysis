#version 330 core

out vec2 v_screen_uv;

void main() {
    const vec2 positions[3] = vec2[3](
        vec2(-1.0, -1.0),
        vec2( 3.0, -1.0),
        vec2(-1.0,  3.0)
    );
    vec2 position = positions[gl_VertexID];
    v_screen_uv = 0.5 * (position + 1.0);
    gl_Position = vec4(position, 0.0, 1.0);
}
