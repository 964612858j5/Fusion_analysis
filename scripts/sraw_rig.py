"""v16_5 S-Raw: a REAL Step1 mount on an OME-TIFF, for the probes.

`open_rig(backend, slide, ...)` sets the backend switch, builds the real
host, stack, controller, view and mount on `slide` with a whole-slide (or
given) region and the chosen channels ticked, opens and activates it.
Shared by `sraw_pixel_probe.py` and `sraw_lifecycle_probe.py`.
"""

import os
import sys
import time
from types import SimpleNamespace

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PALETTE = ["#0000ff", "#00ff00", "#ff0000", "#ffff00", "#ff00ff", "#00ffff"]


def open_rig(backend, slide, *, channels=(), roi=None, size=530, smooth=1):
    os.environ["BLOCK01_STEP1_BACKEND"] = backend
    os.environ["BLOCK01_SMOOTH"] = str(int(smooth))
    sys.path[:0] = [os.path.dirname(_REPO), _REPO]
    import conftest  # noqa: F401 -- maps `block01` to this checkout
    from PyQt5 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["sraw"])
    from block01.core.display_identity import DatasetIdentity
    from block01.core.fusion_domain import FusionDomainModel
    from block01.ui.block01_display import ChannelDisplayState
    from block01.ui.step1_draft_spec import STEP1_SCOPE
    from block01.ui.step1_viewer_host import Step1ViewerHost
    from block01.ui.step1_viewer_mount import Step1WholeSlideMount
    from block01.viewer.raw_tile_provider import RawTileProvider

    raw = RawTileProvider(slide)
    names = list(raw.channel_names)
    h0, w0 = raw.level_shape(0)
    raw.close()
    chosen = list(channels) or names
    identity = DatasetIdentity(path=slide, fingerprint="sraw")
    domain = FusionDomainModel()
    domain.bind_dataset(identity)
    domain.prepare_restore(identity, {
        "groups": {"markers": {"group_weight": 1.0, "channels": {c: 1.0 for c in chosen[1:]}}},
        "nucleus": {"channel": chosen[0], "weight": 1.0}, "enabled": list(chosen)})
    domain.commit_restore("probe")
    domain.install_committed_snapshot({"hash": "one", "fusion_config": domain.effective_config()})
    state = ChannelDisplayState()
    state.bind(identity, install={"order": names})
    with state.using_scope(STEP1_SCOPE):
        for i, c in enumerate(names):
            state.set_display_visible(c, c in chosen)
            state.set_color(c, PALETTE[i % len(PALETTE)])
            state.set_mapping(c, 10.0 + 3 * i, 240.0 - 5 * i, 1.0 if i % 2 == 0 else 0.7)
        state.set_selected_channel(chosen[0])
    state.set_scope(STEP1_SCOPE)
    window = SimpleNamespace(
        _display=SimpleNamespace(state=state, fusion=domain,
                                 request_mapping_seed=lambda *a, **k: True),
        loader=SimpleNamespace(filepath=slide), _corrected_decisions={},
        _corrected_zarr_path="/tmp/sraw-none.zarr",
        _active_roi={"name": "ROI_1", "bbox_fullres": list(roi or [0, h0, 0, w0])},
        step0_output={"channel_remap_config_hash": "r1"})
    host = Step1ViewerHost()
    host.resize(size, size)
    host.show()
    app.processEvents()
    mount = Step1WholeSlideMount(window, host=host, gpu=True)
    mount.open(chosen[0])
    mount.activate()

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QtCore.QEventLoop.AllEvents, 10)

    return SimpleNamespace(app=app, mount=mount, window=window, state=state, domain=domain,
                           names=names, chosen=chosen, shape=(h0, w0), pump=pump)


def busy(rig):
    """Is anything still on its way to the picture?"""
    mount = rig.mount
    binding = mount.gpu_binding
    if binding is None:
        return True
    if type(binding).__name__ == "Step1CoreBinding":
        report = mount.gpu_layer.last_report or {}
        return (not binding.idle() or report.get("deferred", 1)
                or report.get("shown", 0) < report.get("wanted", 0))
    stats = binding.stats()
    return (not mount.host.stack.scheduler.idle() or stats.get("deferred_uploads")
            or binding._fine or binding.coarse_pending_channels())


def stable(rig, timeout=40.0):
    """The layer's `final` once nothing is in flight and two readbacks
    300 ms apart are identical: (picture, reached)."""
    import numpy as np
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        rig.pump(0.3)
        rig.mount.gpu_layer.update()
        rig.pump(0.05)
        if busy(rig):
            last = None
            continue
        shot = rig.mount.gpu_layer.readback_rgba_for_test()
        if last is not None and shot.shape == last.shape and np.array_equal(shot, last):
            return shot, True
        last = shot
    return last, False
