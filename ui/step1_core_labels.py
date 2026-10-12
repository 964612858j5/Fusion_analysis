"""v16_5 R3: Step3's mask surface on the Rust-drawn layer (plan §2.1, L1).

`Step3LabelBinding` plans and reads the masks as before and hands
`LabelSnapshot`s to `set_labels`; here they are checked as
`Step1GpuLayer.set_labels` checks them, each plane's identity becomes a
number, the planes the core holds no copy of yet are sent as bytes, and
the outline width becomes screen pixels for the current device pixel
ratio. The core keeps its copies, uploads and draws them over the picture
it composed; like the Python layer, a new snapshot alone composes no
channel: the masks are drawn at once over the picture as it stands
(`ViewerCore.draw_labels`, in our own context).
"""

import collections
from typing import Any, Dict

import numpy as np
from PyQt5 import QtGui

from .step1_gpu_layer import (LABEL_FILL, LABEL_OUTLINE, LabelSnapshot, _LabelTextureLru,
                              label_radius)

#: plane identities remembered (their numbers); the oldest are forgotten
IDENTITIES_KEPT = 100_000


class CoreLabelSurface:
    """Mixed into `Step1CoreLayer`; `_init_labels` from its constructor."""

    def _init_labels(self, enabled: bool) -> None:
        self._labels_enabled = bool(enabled)
        self._label_snapshot = LabelSnapshot()
        self._label_ids: "collections.OrderedDict[Any, int]" = collections.OrderedDict()
        self._label_next = 1
        self._label_ratio = None

    @property
    def labels_enabled(self) -> bool:
        return self._labels_enabled

    def _label_id(self, identity) -> int:
        number = self._label_ids.get(identity)
        if number is None:
            number = self._label_next
            self._label_next += 1
            self._label_ids[identity] = number
            while len(self._label_ids) > IDENTITIES_KEPT:
                self._label_ids.popitem(last=False)
        else:
            self._label_ids.move_to_end(identity)
        return number

    def set_labels(self, snapshot: LabelSnapshot) -> Dict[str, Any]:
        """`Step1GpuLayer.set_labels`: refused before it is kept when a
        layer or plane is malformed or the visible planes exceed the label
        budget; then drawn over the picture as it stands."""
        from .step1_core_layer import Step1CoreLayerError
        if not self._labels_enabled:
            raise Step1CoreLayerError("this layer was built without labels")
        if self._disposed:
            raise Step1CoreLayerError("the layer is disposed")
        for layer in snapshot.layers:
            if layer.mode not in (LABEL_OUTLINE, LABEL_FILL):
                raise Step1CoreLayerError(f"unknown label mode {layer.mode!r}")
            for plane in layer.planes:
                try:
                    _LabelTextureLru._validate(plane)
                except Exception as exc:                    # noqa: BLE001 -- one error type here
                    raise Step1CoreLayerError(str(exc)) from exc
        needed = {plane.identity: _LabelTextureLru.plane_bytes(plane)
                  for layer in snapshot.layers if layer.visible for plane in layer.planes}
        budget = 256 * 1024 * 1024
        if sum(needed.values()) > budget:
            raise Step1CoreLayerError(f"label working set {sum(needed.values())} bytes exceeds the "
                                      f"label budget {budget}")
        had = any(l.visible and l.planes for l in self._label_snapshot.layers)
        self._label_snapshot = snapshot
        if not had and not needed:
            return self.label_stats()                       # no masks before or now: nothing to draw
        self._send_labels()
        if self._initialized and not self.hidden and not self._failed:
            # drawn now over the picture as it stands, masks alone (as the
            # Python layer composes them; a QOpenGLWidget repaint would wait
            # for the event loop), then presented by the next paint
            own = self._own_context()
            try:
                ratio = float(self.devicePixelRatioF()) or 1.0
                self.core.draw_labels(self.defaultFramebufferObject(), max(1, int(round(self.width() * ratio))),
                                      max(1, int(round(self.height() * ratio))))
            except Exception as exc:                        # noqa: BLE001 -- a GL failure: fall back
                self._fail(f"label draw failed: {exc}")
                return self.label_stats()
            finally:
                if own:
                    self.doneCurrent()
            self.update()
        return self.label_stats()

    def _send_labels(self) -> None:
        ratio = float(self.devicePixelRatioF()) or 1.0
        self._label_ratio = ratio
        layers = [layer for layer in self._label_snapshot.layers if layer.visible and layer.planes]
        numbered = [[(self._label_id(p.identity), p) for p in layer.planes] for layer in layers]
        missing = set(self.core.labels_missing([n for planes in numbered for n, _p in planes]))
        sent = []
        for layer, planes in zip(layers, numbered):
            rows = []
            for number, plane in planes:
                ids = np.ascontiguousarray(np.asarray(plane.ids), dtype=np.uint32)
                data = ids.tobytes() if number in missing else None
                missing.discard(number)                     # one copy, however often named
                rows.append((number, tuple(float(v) for v in plane.world_rect), data,
                             int(ids.shape[0]), int(ids.shape[1])))
            sent.append((rows, tuple(float(c) for c in layer.color), float(layer.alpha),
                         label_radius(layer.width, ratio), layer.mode == LABEL_FILL))
        self.core.set_labels(sent)

    def _labels_follow_ratio(self) -> None:
        """A new device pixel ratio changes the outline radius: send again
        (the snapshot of L1, or -- R3b -- the core's own masks' styles)."""
        ratio = float(self.devicePixelRatioF()) or 1.0
        seen, self._ratio_seen = getattr(self, "_ratio_seen", None), ratio
        if seen is not None and ratio != seen and getattr(self, "on_label_ratio", None) is not None:
            self.on_label_ratio()
        if self._label_ratio is not None and ratio != self._label_ratio and self._label_snapshot.layers:
            self._send_labels()

    def label_stats(self) -> Dict[str, Any]:
        if self._disposed:
            return {}
        return dict(self.core.label_stats())

    def _own_context(self):
        own = QtGui.QOpenGLContext.currentContext() is not self.context()
        if own:
            self.makeCurrent()
        return own

    def readback_label_ids_for_test(self) -> np.ndarray:
        """The screen id image of the LAST mask layer drawn, top-left rows."""
        own = self._own_context()
        try:
            got = self.core.readback_label_ids()
        finally:
            if own:
                self.doneCurrent()
        if got is None:
            return np.zeros((0, 0), np.uint32)
        data, width, height = got
        return np.frombuffer(data, np.uint32).reshape(height, width).copy()

    def readback_shown_for_test(self) -> np.ndarray:
        """What `paintGL` puts on screen: `shown` when masks are drawn, else
        `final`; RGBA8, top-left rows."""
        own = self._own_context()
        try:
            got = self.core.readback_final(True)
        finally:
            if own:
                self.doneCurrent()
        if got is None:
            return np.zeros((0, 0, 4), np.uint8)
        data, width, height = got
        return np.frombuffer(data, np.uint8).reshape(height, width, 4).copy()
