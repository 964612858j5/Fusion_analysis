# Third-party notices

## ARM optimized-routines — single-precision `powf`

`src/fuse_powf.rs` is a Rust translation of code and constant tables from
ARM optimized-routines:

- project: https://github.com/ARM-software/optimized-routines
- release v23.01, commit `56e3bf05c19c4e28e1f5edd9093c712f16c5c32a`
- files: `math/powf.c` (algorithm), `math/powf_log2_data.c` and
  `math/exp2f_data.c` (tables, N = 32), configuration from
  `math/math_config.h` (TOINT_INTRINSICS = 0, as glibc builds it on x86_64)
- file headers: `Copyright (c) 2017-2019, Arm Limited.`
  (`exp2f_data.c`: `Copyright (c) 2017-2018, Arm Limited.`)
- upstream licence: `MIT OR Apache-2.0 WITH LLVM-exception`; this project
  uses it under the **MIT** option, reproduced below.

The translation keeps the algorithm, the constants (as exact binary64 bit
patterns) and the evaluation order of glibc's x86_64 FMA build; it does not
take code from glibc's copy of the routine.

```
MIT License

Copyright (c) 1999-2022, Arm Limited.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
