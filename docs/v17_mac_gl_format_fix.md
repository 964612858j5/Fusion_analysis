# macOS: Step0 viewer showed a stale upside-down window after Load (fix, 2026-11-09)

User report (TG 2026-11-09, Mac mini, PySide6 6.11.3, locally built
fusion_viewer_core 0.2.1): after Load, Step0's viewer shows the channel list
(a copy of the window) inside the image area; resizing the window clears it.
Highest priority, fix authorized by the user in the same message.

## Cause (reproduced)

Loading the sample (`cropped_region.ome.tif`) in a scripted run and grabbing
the screen: the viewer area showed a shrunken, vertically flipped copy of the
whole window, never the slide; after a resize it went black. The log had
`qt.qpa.openglcontext: Could not create NSOpenGLContext with shared context,
falling back to unshared context` when the GL host was created, and after
the resize `GLD_TEXTURE_INDEX_2D is unloadable ... using zero texture`.

Every GL widget asks for OpenGL 3.3 core (`ui/step0/gl_host.py:601`,
`ui/step1_core_layer.py:82`, `ui/step1_gpu_layer.py:1043`), while the
application's default format -- the window's composition context -- stayed
macOS's legacy 2.1. macOS cannot share between the two profiles, so the
window composited the widget's texture from an unshared context: undefined
memory (the old window image, bottom-up). Linux drivers share across
profiles, which is why it never showed there.

## Fix (closed list)

* `ui/gpu_warmup.py` `configure_surface_format()` (called before the
  QApplication exists): on `sys.platform == "darwin"` only, the default
  format is also OpenGL / 3.3 / CoreProfile. Other systems unchanged.
* `tests/test_a9_s40_swap.py`: the existing "nothing else touched" test pins
  `sys.platform = "linux"`; a new test checks the macOS default.

## Verification

* Scripted real run on the Mac with the change: no NSOpenGLContext / texture
  warnings; the DAPI image shows right after Load and after a resize
  (screenshots in the session scratchpad).
* `tests/test_a9_s40_swap.py`: 5 passed. Mutation (darwin branch disabled):
  the new test fails.
* Real-machine acceptance by the user on the Mac; Linux unchanged by
  construction (branch not taken), to be confirmed on the server.

Code review (codex astra low, 2026-11-09): PASS, no P1/P2. Taken: the
docstring no longer claims other systems share across profiles; the
"nothing else touched" test runs for linux and win32 and also checks the
renderable type. Advisory kept: the unit tests check the requested format
only; the display itself is the user's real-machine check (Step0, Step1,
Step3 viewers on the Mac).
