#version 330 core

// Block A9 §35.6/§35.7: ONE fullscreen pass composes every drawn channel
// (a virtual texture: per-channel page tables into the tile arrays) into
// `accum` (Overlay) or `fusion` (Fusion), replacing per channel "clear the
// signal target, draw its planes, add the whole screen". `_finalize` (the
// region clip) and the labels run after it unchanged.
//
// The arithmetic is the multipass path's, expression for expression:
//   signal  PASS_SOURCE / PASS_SOURCE_UINT / PASS_ARRAY_*  (step1_gpu.frag)
//   overlay PASS_CONTRIBUTION component 0, blended ADD (rgb) / MAX (alpha)
//   fusion  component 1 summed per group, PASS_GROUP_RESOLVE MAX, then the
//           nucleus component 2 MAX
// Per channel, the sample is the one the multipass overwrite order leaves:
// the LAST submitted plane (finest level first) that has a valid sample at
// this pixel; an invalid one (outside its valid rect, NaN, Inf) lets the
// next coarser level show, as `discard` did.

in vec2 v_screen_uv;
out vec4 out_rgba;

uniform usampler2DArray u_u0;
uniform usampler2DArray u_u1;
uniform usampler2DArray u_u2;
uniform usampler2DArray u_u3;
uniform usampler2DArray u_u4;
uniform usampler2DArray u_u5;
uniform sampler2DArray u_f0;
uniform sampler2DArray u_f1;
uniform sampler2DArray u_f2;
uniform sampler2DArray u_f3;
uniform isampler2DArray u_pages;   // all levels side by side; layer = channel slot
uniform sampler2D u_meta;          // per (family, block, layer): rect, valid rect, size
uniform sampler2D u_params;        // per drawn channel, in draw order
uniform vec4 u_view_rect;
uniform vec2 u_target_size;
uniform int u_rows;
uniform int u_fusion;
uniform vec4 u_level_a[16];        // (tile world w, tile world h, page x offset, 0)
uniform ivec2 u_level_b[16];       // (grid w, grid h)

const int BLOCK_ROWS = 3;          // meta rows per block: rect, valid, size
const int U_BLOCKS = 6;

float fetch_raw(int family, int block, int layer, ivec2 texel) {
    ivec3 at = ivec3(texel, layer);
    if (family == 0) {
        if (block == 0) return float(texelFetch(u_u0, at, 0).r);
        if (block == 1) return float(texelFetch(u_u1, at, 0).r);
        if (block == 2) return float(texelFetch(u_u2, at, 0).r);
        if (block == 3) return float(texelFetch(u_u3, at, 0).r);
        if (block == 4) return float(texelFetch(u_u4, at, 0).r);
        return float(texelFetch(u_u5, at, 0).r);
    }
    if (block == 0) return texelFetch(u_f0, at, 0).r;
    if (block == 1) return texelFetch(u_f1, at, 0).r;
    if (block == 2) return texelFetch(u_f2, at, 0).r;
    return texelFetch(u_f3, at, 0).r;
}

// The signal of channel `row` at `world`, or -1.0 when no submitted plane
// has a valid sample there.
float channel_signal(int row, vec2 world) {
    vec4 mapping = texelFetch(u_params, ivec2(0, row), 0);   // lo, hi, gamma, page layer
    vec4 levels0 = texelFetch(u_params, ivec2(3, row), 0);
    vec4 levels1 = texelFetch(u_params, ivec2(4, row), 0);
    vec4 info = texelFetch(u_params, ivec2(2, row), 0);      // group, gweight, nlevels, 0
    int page_layer = int(mapping.w);
    int count = int(info.z);
    for (int k = 0; k < count; k++) {
        int level = int(k < 4 ? levels0[k] : levels1[k - 4]);
        vec4 la = u_level_a[level];
        ivec2 grid = u_level_b[level];
        ivec2 cell = ivec2(floor(world.x / la.x), floor(world.y / la.y));
        // the plane rect decides; a rounding at a cell edge moves one cell
        for (int attempt = 0; attempt < 3; attempt++) {
            if (cell.x < 0 || cell.y < 0 || cell.x >= grid.x || cell.y >= grid.y) {
                break;
            }
            int code = texelFetch(u_pages, ivec3(int(la.z) + cell.x, cell.y, page_layer), 0).r;
            if (code < 0) {
                break;
            }
            int family = code >> 24;
            int block = (code >> 16) & 255;
            int layer = code & 65535;
            int meta_row = (family * U_BLOCKS + block) * BLOCK_ROWS;
            vec4 rect = texelFetch(u_meta, ivec2(layer, meta_row), 0);
            if (world.x < rect.x) { cell.x -= 1; continue; }
            if (world.x >= rect.y) { cell.x += 1; continue; }
            if (world.y < rect.z) { cell.y -= 1; continue; }
            if (world.y >= rect.w) { cell.y += 1; continue; }
            if (family == 0) {
                vec4 valid = texelFetch(u_meta, ivec2(layer, meta_row + 1), 0);
                if (world.x < valid.x || world.x >= valid.y ||
                    world.y < valid.z || world.y >= valid.w) {
                    break;                         // invalid here: next level
                }
            }
            vec2 size_f = texelFetch(u_meta, ivec2(layer, meta_row + 2), 0).xy;
            vec2 plane_size = vec2(rect.y - rect.x, rect.w - rect.z);
            vec2 uv = vec2((world.x - rect.x) / plane_size.x,
                           (world.y - rect.z) / plane_size.y);
            ivec2 size = ivec2(size_f);
            ivec2 texel = clamp(ivec2(floor(uv * vec2(size))), ivec2(0), size - 1);
            float raw = fetch_raw(family, block, layer, texel);
            if (family == 1 && (isnan(raw) || isinf(raw))) {
                break;
            }
            float denominator = mapping.y - mapping.x;
            float signal = 0.0;
            if (denominator > 0.0) {
                signal = pow(clamp((raw - mapping.x) / denominator, 0.0, 1.0),
                             max(mapping.z, 0.000001));
            }
            return clamp(signal, 0.0, 1.0);
        }
    }
    return -1.0;
}

void main() {
    vec2 screen_uv = gl_FragCoord.xy / u_target_size;
    vec2 view_size = vec2(u_view_rect.y - u_view_rect.x,
                          u_view_rect.w - u_view_rect.z);
    vec2 world = vec2(u_view_rect.x + screen_uv.x * view_size.x,
                      u_view_rect.w - screen_uv.y * view_size.y);
    vec4 acc = vec4(0.0);
    if (u_fusion == 0) {
        for (int row = 0; row < u_rows; row++) {
            float s = channel_signal(row, world);
            if (s < 0.0) {
                continue;
            }
            vec4 colour = texelFetch(u_params, ivec2(1, row), 0);   // r, g, b, weight
            float value = s * colour.w;
            acc.rgb += value * colour.rgb;
            acc.a = 1.0;
        }
    } else {
        float group_sum = 0.0;
        bool group_any = false;
        int group = -1;
        float group_weight = 0.0;
        for (int row = 0; row < u_rows; row++) {
            vec4 info = texelFetch(u_params, ivec2(2, row), 0);
            vec4 colour = texelFetch(u_params, ivec2(1, row), 0);
            int row_group = int(info.x);
            if (row_group != group) {
                if (group_any) {
                    acc.r = max(acc.r, clamp(group_sum * group_weight, 0.0, 1.0));
                    acc.a = 1.0;
                }
                group = row_group;
                group_sum = 0.0;
                group_any = false;
                group_weight = info.y;
            }
            float s = channel_signal(row, world);
            if (s < 0.0) {
                continue;
            }
            float value = s * colour.w;
            if (row_group < 0) {
                acc.b = max(acc.b, value);          // the nucleus (MAX)
                acc.a = 1.0;
            } else {
                group_sum += value;
                group_any = true;
            }
        }
        if (group_any) {
            acc.r = max(acc.r, clamp(group_sum * group_weight, 0.0, 1.0));
            acc.a = 1.0;
        }
    }
    out_rgba = acc;
}
