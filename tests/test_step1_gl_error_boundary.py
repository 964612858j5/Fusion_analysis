"""The GL error boundary of the tile array store.

`_TileArrayStore.prepare` is one step of a submission, not its boundary: the
layer clears the error queue once, at the start of `submit()`, and every call
it makes afterwards is checked. These tests pin that with a fake GL:

* an error that was already pending when the submission starts is foreign and
  must not fail the frame (it is counted, then dropped);
* an error this submission's own upload produces must raise, and must not be
  forgotten by a later "clear";
* a batch that raised has consumed its error: the next submission starts
  clean, and nothing is silently skipped.
"""

import types

import numpy as np
import pytest

from block01.ui.step1_display_types import (Step1GpuLayerError, _check_gl,
                                            _clear_gl_errors)
from block01.ui.step1_gpu_layer import _TileArrayStore


class FakeGL:
    """The GL surface the array store uses, with an error queue."""

    GL_NO_ERROR = 0
    GL_INVALID_ENUM = 0x0500
    GL_TEXTURE_2D_ARRAY = 0x8C1A
    GL_TEXTURE_MIN_FILTER = 0x2801
    GL_TEXTURE_MAG_FILTER = 0x2800
    GL_TEXTURE_WRAP_S = 0x2802
    GL_TEXTURE_WRAP_T = 0x2803
    GL_TEXTURE_MAX_LEVEL = 0x813D
    GL_NEAREST = 0x2600
    GL_CLAMP_TO_EDGE = 0x812F
    GL_UNPACK_ALIGNMENT = 0x0CF5
    GL_R32F = 0x822E
    GL_RED = 0x1903
    GL_FLOAT = 0x1406
    GL_TEXTURE_2D = 0x0DE1

    def __init__(self):
        self.errors = []
        self.calls = []
        self.fail_on = None          # name that must set an error

    def _queue(self, error):
        self.errors.append(error)

    def glGetError(self):
        return self.errors.pop(0) if self.errors else self.GL_NO_ERROR

    def _record(self, name):
        self.calls.append(name)
        if self.fail_on == name:
            self._queue(self.GL_INVALID_ENUM)

    def glGenTextures(self, n):
        self._record("glGenTextures")
        return [9000 + len(self.calls)]

    def glBindTexture(self, *a):
        self._record("glBindTexture")

    def glTexParameteri(self, *a):
        self._record("glTexParameteri")

    def glTexStorage3D(self, *a):
        self._record("glTexStorage3D")

    def glTexImage3D(self, *a):
        self._record("glTexImage3D")

    def glTexSubImage3D(self, *a):
        self._record("glTexSubImage3D")

    def glPixelStorei(self, *a):
        self._record("glPixelStorei")

    def glDeleteTextures(self, *a):
        self._record("glDeleteTextures")

    def glTexSubImage2D(self, *a):
        self._record("glTexSubImage2D")

    # ── the rest of the surface `_warm` touches: recorded, never failing ──
    GL_FRAMEBUFFER_COMPLETE = 0x8CD5
    GL_FRAMEBUFFER = 0x8D40
    GL_READ_FRAMEBUFFER = 0x8CA8
    GL_DRAW_FRAMEBUFFER = 0x8CA9
    GL_COLOR_ATTACHMENT0 = 0x8CE0

    def glGenFramebuffers(self, n):
        self._record("glGenFramebuffers")
        return [7000 + len(self.calls)]

    def glCheckFramebufferStatus(self, *a):
        self._record("glCheckFramebufferStatus")
        return self.GL_FRAMEBUFFER_COMPLETE

    def __getattr__(self, name):
        if name.startswith("GL_"):
            return 0
        if not name.startswith("gl"):
            raise AttributeError(name)
        def call(*a, **k):
            self._record(name)
            return 0
        return call


def _plane(identity=("t", 0, 0, 0)):
    return types.SimpleNamespace(
        identity=identity,
        values=np.zeros((4, 4), np.float32),
        valid=None,
        valid_rect=None,
        world_rect=(0.0, 4.0, 0.0, 4.0),
    )


def test_an_error_pending_before_the_submission_is_foreign():
    gl = FakeGL()
    gl._queue(gl.GL_INVALID_ENUM)                 # something else's error
    assert _clear_gl_errors(gl) == 1              # counted...
    _check_gl(gl, "own call")                     # ...and not blamed on us
    store = _TileArrayStore(max_bytes=64 << 20, max_layers=8)
    store.prepare(gl, [("CD3", _plane())])        # the batch itself is clean
    assert store.deferred == 0


def test_an_error_the_batch_produces_must_raise():
    gl = FakeGL()
    gl.fail_on = "glTexSubImage3D"                # the upload itself fails
    _clear_gl_errors(gl)                          # the submission boundary
    store = _TileArrayStore(max_bytes=64 << 20, max_layers=8)
    with pytest.raises(Step1GpuLayerError) as exc:
        store.prepare(gl, [("CD3", _plane())])
    assert "array tile upload" in str(exc.value)


def test_prepare_does_not_drain_what_the_submission_produced():
    """An error raised after the boundary and before `prepare` (e.g. a failed
    target allocation) must reach `prepare`'s own check, not disappear."""
    gl = FakeGL()
    _clear_gl_errors(gl)                          # the submission boundary
    gl._queue(gl.GL_INVALID_ENUM)                 # an earlier call in this frame
    store = _TileArrayStore(max_bytes=64 << 20, max_layers=8)
    with pytest.raises(Step1GpuLayerError):
        store.prepare(gl, [("CD3", _plane())])


def test_a_batch_that_raised_consumed_its_error():
    gl = FakeGL()
    gl.fail_on = "glTexStorage3D"
    _clear_gl_errors(gl)
    store = _TileArrayStore(max_bytes=64 << 20, max_layers=8)
    with pytest.raises(Step1GpuLayerError):
        store.prepare(gl, [("CD3", _plane())])
    # the next submission clears an empty queue and uploads cleanly
    assert _clear_gl_errors(gl) == 0
    gl.fail_on = None
    store.prepare(gl, [("CD3", _plane(("t", 1, 0, 0)))])
    assert store.deferred == 0
