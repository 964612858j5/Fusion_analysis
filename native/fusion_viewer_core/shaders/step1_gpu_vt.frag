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

//@SAMPLERS@
uniform isampler2DArray u_pages;   // all levels side by side; layer = channel slot
uniform sampler2D u_meta;          // per (family, block, layer): rect, valid rect, size
uniform sampler2D u_params;        // per drawn channel, in draw order
uniform vec4 u_view_rect;
uniform vec2 u_target_size;
uniform int u_rows;
uniform int u_fusion;
uniform int u_smooth;              // A9 §38: bilinear raw values (default on)
uniform vec4 u_level_a[16];        // (tile world w, tile world h, page x offset, 0)
uniform ivec2 u_level_b[16];       // (grid w, grid h)

//@BLOCK_CONSTANTS@

float fetch_raw(int family, int block, int layer, ivec2 texel) {
    ivec3 at = ivec3(texel, layer);
//@FETCH@
}

// A9 §38: texels t0..t3 of one layer (x then y), one sampler choice
vec4 fetch_raw4(int family, int block, int layer, ivec2 t0, ivec2 t1, ivec2 t2, ivec2 t3) {
//@FETCH4@
}

// A9 §38: the plane of `level` holding `world` for page layer
// `page_layer` -- the same cell lookup and rect retry as `channel_signal`.
bool find_plane(int page_layer, int level, vec2 world, out int family, out int block,
                out int layer, out int meta_row, out vec4 rect) {
    family = 0; block = 0; layer = 0; meta_row = 0; rect = vec4(0.0);
    vec4 la = u_level_a[level];
    ivec2 grid = u_level_b[level];
    ivec2 cell = ivec2(floor(world.x / la.x), floor(world.y / la.y));
    for (int attempt = 0; attempt < 3; attempt++) {
        if (cell.x < 0 || cell.y < 0 || cell.x >= grid.x || cell.y >= grid.y) {
            return false;
        }
        int code = texelFetch(u_pages, ivec3(int(la.z) + cell.x, cell.y, page_layer), 0).r;
        if (code < 0) {
            return false;
        }
        family = code >> 24;
        block = (code >> 16) & 255;
        layer = code & 65535;
        meta_row = (family * U_BLOCKS + block) * BLOCK_ROWS;
        rect = texelFetch(u_meta, ivec2(layer, meta_row), 0);
        if (world.x < rect.x) { cell.x -= 1; continue; }
        if (world.x >= rect.y) { cell.x += 1; continue; }
        if (world.y < rect.z) { cell.y -= 1; continue; }
        if (world.y >= rect.w) { cell.y += 1; continue; }
        return true;
    }
    return false;
}

// A9 §38: the raw value at `world` (a texel centre) in channel `page_layer`
// at `level`, from whichever plane of that level holds it -- the tile
// across an edge included. False when absent or invalid there; a coarser
// level is never used for a tap.
bool level_raw(int page_layer, int level, vec2 world, out float raw) {
    raw = 0.0;
    int family; int block; int layer; int meta_row; vec4 rect;
    if (!find_plane(page_layer, level, world, family, block, layer, meta_row, rect)) {
        return false;
    }
    if (family == 0) {
        vec4 valid = texelFetch(u_meta, ivec2(layer, meta_row + 1), 0);
        if (world.x < valid.x || world.x >= valid.y ||
            world.y < valid.z || world.y >= valid.w) {
            return false;
        }
    }
    ivec2 size = ivec2(texelFetch(u_meta, ivec2(layer, meta_row + 2), 0).xy);
    vec2 uv = vec2((world.x - rect.x) / (rect.y - rect.x),
                   (world.y - rect.z) / (rect.w - rect.z));
    ivec2 texel = clamp(ivec2(floor(uv * vec2(size))), ivec2(0), size - 1);
    raw = fetch_raw(family, block, layer, texel);
    return !(family == 1 && (isnan(raw) || isinf(raw)));
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
            // smoothing shows only where a texel is wider than a screen
            // pixel (zoomed past this level's detail); below that it would
            // cost GPU time for a difference nobody sees
            if (u_smooth != 0 && (plane_size.x * u_target_size.x
                    > float(size.x) * (u_view_rect.y - u_view_rect.x)
                    || plane_size.y * u_target_size.y
                    > float(size.y) * (u_view_rect.w - u_view_rect.z))) {
                // four texels around the pixel, each by its own world
                // centre (in this plane: here; across an edge: the
                // neighbour tile of this level), invalid ones dropped
                vec2 p = uv * vec2(size) - 0.5;
                vec2 base = floor(p);
                vec2 f = p - base;
                ivec2 i0 = ivec2(base);
                vec2 texel_world = plane_size / vec2(size);
                // the in-plane taps in one fetch (an outside one reads a
                // clamped texel and is replaced below)
                ivec2 top = size - 1;
                vec4 quad = fetch_raw4(family, block, layer,
                                       clamp(i0, ivec2(0), top),
                                       clamp(i0 + ivec2(1, 0), ivec2(0), top),
                                       clamp(i0 + ivec2(0, 1), ivec2(0), top),
                                       clamp(i0 + ivec2(1, 1), ivec2(0), top));
                vec4 valid = family == 0 ? texelFetch(u_meta, ivec2(layer, meta_row + 1), 0)
                                         : vec4(0.0);
                float total = 0.0;
                float weights = 0.0;
                for (int j = 0; j < 4; j++) {
                    ivec2 d = ivec2(j & 1, j >> 1);
                    float w = (d.x == 1 ? f.x : 1.0 - f.x) * (d.y == 1 ? f.y : 1.0 - f.y);
                    if (w <= 0.0) {
                        continue;
                    }
                    ivec2 t = i0 + d;
                    float tap;
                    bool ok;
                    if (t.x >= 0 && t.y >= 0 && t.x < size.x && t.y < size.y) {
                        tap = quad[j];
                        ok = true;
                        if (family == 0) {
                            vec2 c = vec2(rect.x, rect.z) + (vec2(t) + 0.5) * texel_world;
                            ok = !(c.x < valid.x || c.x >= valid.y ||
                                   c.y < valid.z || c.y >= valid.w);
                        } else {
                            ok = !(isnan(tap) || isinf(tap));
                        }
                    } else {
                        vec2 c = vec2(rect.x, rect.z) + (vec2(t) + 0.5) * texel_world;
                        ok = level_raw(page_layer, level, c, tap);
                    }
                    if (ok) {
                        total += w * tap;
                        weights += w;
                    }
                }
                if (weights > 0.0) {
                    raw = total / weights;
                }
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
