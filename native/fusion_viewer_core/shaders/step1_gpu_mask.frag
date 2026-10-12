#version 330 core

// Writes nothing: the colour mask is off while the polygon is rasterised
// and only the stencil buffer is touched (GL_INVERT on the low bit, which
// is the even-odd rule -- a triangle fan over a CONCAVE polygon covers the
// area outside it an even number of times and inside it an odd number).

out vec4 out_rgba;

void main() {
    out_rgba = vec4(0.0);
}
