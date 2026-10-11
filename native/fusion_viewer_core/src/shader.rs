//! GLSL sources and program linking. The shader files are byte copies of
//! `ui/shaders` (a test keeps them identical); this module only fills the
//! single-pass template's sampler slots (`step1_gpu_vt.shader_source`) and
//! adds a pass define (`Step1GpuLayer._compile_programs`).

use glow::HasContext;

pub const VERT: &str = include_str!("../shaders/step1_gpu.vert");
pub const FRAG: &str = include_str!("../shaders/step1_gpu.frag");
pub const VT_TEMPLATE: &str = include_str!("../shaders/step1_gpu_vt.frag");
pub const VT_BLOCK_ROWS: usize = 3;

/// `(integer blocks, float blocks)` the single pass can bind: every unit
/// but the page, meta and parameter textures; mostly integer.
pub fn sampler_split(max_units: i32) -> (usize, usize) {
    let available = (max_units.min(32) - 3).max(2) as usize;
    let floats = (available / 6).max(1);
    (available - floats, floats)
}

/// The single-pass fragment shader for `u` integer and `f` float blocks.
pub fn vt_source(u: usize, f: usize) -> String {
    let mut samplers: Vec<String> = (0..u).map(|i| format!("uniform usampler2DArray u_u{i};")).collect();
    samplers.extend((0..f).map(|i| format!("uniform sampler2DArray u_f{i};")));
    let mut fetch = vec!["    if (family == 0) {".to_string()];
    fetch.extend((0..u).map(|i| format!("        if (block == {i}) return float(texelFetch(u_u{i}, at, 0).r);")));
    fetch.push("        return 0.0;".into());
    fetch.push("    }".into());
    fetch.extend((0..f).map(|i| format!("    if (block == {i}) return texelFetch(u_f{i}, at, 0).r;")));
    fetch.push("    return 0.0;".into());
    let quad = |sampler: &str, cast: &str| {
        let taps: Vec<String> = (0..4)
            .map(|k| format!("{cast}(texelFetch({sampler}, ivec3(t{k}, layer), 0).r)")).collect();
        format!("return vec4({});", taps.join(", "))
    };
    let mut fetch4 = vec!["    if (family == 0) {".to_string()];
    fetch4.extend((0..u).map(|i| format!("        if (block == {i}) {}", quad(&format!("u_u{i}"), "float"))));
    fetch4.push("        return vec4(0.0);".into());
    fetch4.push("    }".into());
    fetch4.extend((0..f).map(|i| format!("    if (block == {i}) {}", quad(&format!("u_f{i}"), ""))));
    fetch4.push("    return vec4(0.0);".into());
    let constants = format!("const int BLOCK_ROWS = {VT_BLOCK_ROWS};\nconst int U_BLOCKS = {u};");
    VT_TEMPLATE.replace("//@SAMPLERS@", &samplers.join("\n"))
        .replace("//@FETCH@", &fetch.join("\n"))
        .replace("//@FETCH4@", &fetch4.join("\n"))
        .replace("//@BLOCK_CONSTANTS@", &constants)
}

/// `FRAG` with `#define <pass>` after its first line.
pub fn with_define(define: &str) -> String {
    FRAG.replacen('\n', &format!("\n#define {define}\n"), 1)
}

/// Compile and link; on failure every name made is deleted again.
pub unsafe fn link(gl: &glow::Context, vert: &str, frag: &str) -> Result<glow::Program, String> {
    unsafe {
        let program = gl.create_program()?;
        let mut made = Vec::new();
        let mut failure = None;
        for (kind, src) in [(glow::VERTEX_SHADER, vert), (glow::FRAGMENT_SHADER, frag)] {
            let s = match gl.create_shader(kind) {
                Ok(s) => s,
                Err(e) => { failure = Some(e); break; }
            };
            made.push(s);
            gl.shader_source(s, src);
            gl.compile_shader(s);
            if !gl.get_shader_compile_status(s) {
                failure = Some(gl.get_shader_info_log(s));
                break;
            }
            gl.attach_shader(program, s);
        }
        if failure.is_none() {
            gl.link_program(program);
            if !gl.get_program_link_status(program) {
                failure = Some(gl.get_program_info_log(program));
            }
        }
        for s in made {
            gl.detach_shader(program, s);
            gl.delete_shader(s);
        }
        match failure {
            None => Ok(program),
            Some(e) => {
                gl.delete_program(program);
                Err(e)
            }
        }
    }
}
