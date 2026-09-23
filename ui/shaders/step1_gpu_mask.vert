#version 330 core

// G3.2c.1: the ROI polygon, in LEVEL-0 WORLD coordinates, turned into clip
// space with the same mapping the source pass uses (step1_gpu.frag,
// PASS_SOURCE): x grows right, and world y grows DOWNWARD on screen, so the
// bottom of the framebuffer is `u_view_rect.w`.
//
// This program exists because `step1_gpu.vert` can only ever emit the one
// fullscreen triangle: it ignores its inputs and builds the vertex from
// `gl_VertexID`. A mask needs real vertices.

layout(location = 0) in vec2 a_world;

uniform vec4 u_view_rect;   // (x0, x1, y0, y1) in level-0 pixels

void main() {
    float u = (a_world.x - u_view_rect.x) / (u_view_rect.y - u_view_rect.x);
    float v = (u_view_rect.w - a_world.y) / (u_view_rect.w - u_view_rect.z);
    gl_Position = vec4(2.0 * u - 1.0, 2.0 * v - 1.0, 0.0, 1.0);
}
