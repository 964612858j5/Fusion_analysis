#version 330 core

// Block 4b: Step3's label layer, two passes over the fullscreen triangle of
// `step1_gpu.vert`. Both address the output by `gl_FragCoord` (pixel centres
// i + 0.5, exact) rather than the interpolated `v_screen_uv`.
//
// PASS_LABEL_IDS  -- one label tile into the SCREEN ID IMAGE (R32UI): the
//   world point under each pixel centre, with the same mapping as the image
//   (PASS_SOURCE in step1_gpu.frag: x grows right, world y grows downward on
//   screen), picks the tile's texel -- nearest, no filtering, an id is never
//   blended. Id 0 is WRITTEN like any other: a new tile's background covers
//   the old labels under it.
// PASS_LABEL_DRAW -- the id image onto the picture: an outline where a pixel
//   of a non-zero id sees a different id within `u_radius` (Chebyshev), or
//   outside the image (= id 0); or a fill whose colour is the id's
//   lowbias32 hash. The CPU references are `core/step3_masks.py`
//   `outline_reference` and `fill_colour`.
//   Dashed (`u_dash` > 0, docs/v17_step3_multi_results_plan.md §4): the
//   outline pixel's outward normal is summed from the neighbours that
//   differ, quantised to 4 directions, and the pixel's position along the
//   edge -- x, y, (x - y)/sqrt2 or (x + y)/sqrt2 of `gl_FragCoord`, plus
//   that direction's slide phase `u_phase` (anchored to the slide, reduced
//   per direction on the CPU so no wrap jumps) -- cut into periods of
//   `u_dash` screen pixels, the first `u_duty` of each drawn.
//   Unset (0), the outline is solid as before.

#ifdef PASS_LABEL_IDS
uniform usampler2D u_labels;
uniform vec4 u_view_rect;      // (x0, x1, y0, y1), level-0 world
uniform vec4 u_plane_rect;     // the tile's world rect, same order
uniform vec2 u_target_size;    // the id image, physical pixels
out uint out_id;

void main() {
    vec2 uv = gl_FragCoord.xy / u_target_size;
    vec2 view_size = vec2(u_view_rect.y - u_view_rect.x,
                          u_view_rect.w - u_view_rect.z);
    vec2 world = vec2(u_view_rect.x + uv.x * view_size.x,
                      u_view_rect.w - uv.y * view_size.y);
    if (world.x < u_plane_rect.x || world.x >= u_plane_rect.y ||
        world.y < u_plane_rect.z || world.y >= u_plane_rect.w) {
        discard;
    }
    vec2 plane_size = vec2(u_plane_rect.y - u_plane_rect.x,
                           u_plane_rect.w - u_plane_rect.z);
    ivec2 size = textureSize(u_labels, 0);
    vec2 plane_uv = vec2((world.x - u_plane_rect.x) / plane_size.x,
                         (world.y - u_plane_rect.z) / plane_size.y);
    ivec2 texel = clamp(ivec2(floor(plane_uv * vec2(size))), ivec2(0), size - 1);
    out_id = texelFetch(u_labels, texel, 0).r;
}
#endif

#ifdef PASS_LABEL_DRAW
uniform usampler2D u_ids;
uniform int u_mode;            // 0 outline, 1 fill
uniform int u_radius;          // screen pixels, 0..8
uniform vec3 u_color;
uniform float u_alpha;
uniform float u_dash;          // period, screen pixels; 0 = solid
uniform float u_duty;          // drawn fraction of a period
uniform vec4 u_phase;          // slide phase per direction: x, y, x-y, x+y (mod the period)
out vec4 out_rgba;

uint lowbias32(uint x) {
    x ^= x >> 16;
    x *= 0x7feb352du;
    x ^= x >> 15;
    x *= 0x846ca68bu;
    x ^= x >> 16;
    return x;
}

float channel(uint h, int shift) {
    return float(55u + (((h >> uint(shift)) & 255u) * 200u) / 255u) / 255.0;
}

void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    ivec2 size = textureSize(u_ids, 0);
    uint id = texelFetch(u_ids, p, 0).r;
    if (id == 0u) {
        discard;
    }
    if (u_mode == 1) {
        uint h = lowbias32(id);
        out_rgba = vec4(channel(h, 0), channel(h, 8), channel(h, 16), u_alpha);
        return;
    }
    bool edge = false;
    vec2 n = vec2(0.0);
    for (int dy = -8; dy <= 8; ++dy) {
        if (abs(dy) > u_radius) {
            continue;
        }
        for (int dx = -8; dx <= 8; ++dx) {
            if (abs(dx) > u_radius || (dx == 0 && dy == 0)) {
                continue;
            }
            ivec2 q = p + ivec2(dx, dy);
            uint other = 0u;
            if (q.x >= 0 && q.y >= 0 && q.x < size.x && q.y < size.y) {
                other = texelFetch(u_ids, q, 0).r;
            }
            if (other != id) {
                edge = true;
                n += vec2(float(dx), float(dy));
            }
        }
    }
    if (!edge) {
        discard;
    }
    if (u_dash > 0.0) {
        vec2 w = gl_FragCoord.xy;
        vec2 a = abs(n);
        float t;
        if (a.x > 2.414 * a.y) {
            t = w.y + u_phase.y;                            // normal ~x: a vertical edge
        } else if (a.y > 2.414 * a.x) {
            t = w.x + u_phase.x;                            // normal ~y: a horizontal edge
        } else if (n.x * n.y > 0.0) {
            t = (w.x - w.y) * 0.70710678 + u_phase.z;       // normal ~(1, 1)
        } else {
            t = (w.x + w.y) * 0.70710678 + u_phase.w;       // normal ~(1, -1)
        }
        if (fract(t / u_dash) >= u_duty) {
            discard;
        }
    }
    out_rgba = vec4(u_color, u_alpha);
}
#endif
