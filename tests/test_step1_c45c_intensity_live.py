"""C4.5c: live Step1 Intensity feedback on the composed layer."""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtGui, QtWidgets  # noqa: E402

from block01.ui.step1_compose_binding import Step1ComposeBinding  # noqa: E402
from block01.ui.step1_compose_coordinator import (  # noqa: E402
    Step1ComposeCoordinator,
)
from block01.ui.step1_composed_layer import Step1ComposedLayer  # noqa: E402
from block01.viewer import step1_compose as compose_core  # noqa: E402

from test_step1_compose_coordinator import (  # noqa: E402
    _Executor, _Host, _settle,
)
from test_step1_composed_layer import _stack as layer_stack  # noqa: E402
from test_step1_draft_binding import _domain, _state  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _rig(app, mode, tiles=((1, 1),)):
    host = _Host(tiles=tiles)
    stack = layer_stack(app)
    host.provider.level_downsample_yx = lambda _level: (1.0, 1.0)
    stack.provider = host.provider
    stack.scheduler = host.scheduler
    stack.controller._visible_tiles = set(tiles)
    host.stack = stack

    executor = _Executor()
    coordinator = Step1ComposeCoordinator(host, executor=executor)
    coordinator.executor = executor
    layer = Step1ComposedLayer(stack)
    layer.attach()
    domain = _domain()
    state = _state(app)
    binding = Step1ComposeBinding(coordinator, domain, state, mode=mode)
    binding.attach_layer(layer)
    binding.connect()
    return SimpleNamespace(host=host, stack=stack, executor=executor,
                           coordinator=coordinator, layer=layer,
                           domain=domain, state=state, binding=binding)


def _pixel(entry):
    entry.item.render()
    image = entry.item.qimage
    assert image is not None
    color = QtGui.QColor.fromRgba(image.pixel(0, 0))
    return (color.red(), color.green(), color.blue(), color.alpha())


def _drain(app, rig, rounds=8):
    for _ in range(rounds):
        app.processEvents()
        ran = rig.executor.run()
        app.processEvents()
        if not ran and not rig.executor.jobs:
            break


def _settle_initial(app, rig):
    rig.binding.refresh("initial")
    rig.host.scheduler.deliver()
    _drain(app, rig)
    entry = rig.layer.pool.entries[(0, 1, 1)]
    return _pixel(entry)


def _latest_pixel_at(rig, tx, ty):
    return _pixel(rig.layer.pool.entries[(0, tx, ty)])


def _expected_pixel_at(rig, tx, ty):
    spec = rig.binding.spec()
    keys = rig.coordinator._keys(0, tx, ty,
                                 rig.coordinator.channels_for(spec))
    tiles = {}
    for key in keys:
        values = rig.coordinator._tile_pixels(key)
        tiles[key.channel] = (np.asarray(values, np.float32),
                              ~np.isnan(values))
    rgba, _valid, _missing = compose_core.compose(
        spec["mode"], tiles, weights=spec.get("weights"),
        colors=spec.get("colors"), mappings=spec.get("mappings"),
        groups=spec.get("groups"), group_weights=spec.get("group_weights"),
        nucleus=spec.get("nucleus") or ("", 0.0))
    return tuple(int(v) for v in rgba[0, 0])

def _latest_pixel(rig):
    return _latest_pixel_at(rig, 1, 1)


def _expected_pixel(rig):
    return _expected_pixel_at(rig, 1, 1)


@pytest.mark.parametrize("mode", [compose_core.MODE_OVERLAY,
                                   compose_core.MODE_FUSION])
def test_mapping_drag_keeps_structural_generation_and_latest_spec_bounded(
        app, mode):
    """Real binding/state path publishes intermediate ImageItem pixels."""
    rig = _rig(app, mode)
    try:
        initial = _settle_initial(app, rig)
        reads = rig.host.scheduler.reads
        generation = rig.coordinator.generation
        published = [initial]

        with rig.state.using_scope("step1"):
            rig.state.set_mapping("CD3", 50.0, 100.0, 1.0, origin="test")
        assert rig.coordinator.generation == generation
        assert len(rig.executor.jobs) == 1
        rig.executor.run()
        app.processEvents()
        published.append(_latest_pixel(rig))

        for value in (40.0, 30.0, 20.0, 10.0):
            with rig.state.using_scope("step1"):
                rig.state.set_mapping("CD3", 0.0, value, 1.0,
                                      origin="test")
        stats = rig.coordinator.intensity_stats()
        assert stats["pending"] is True
        assert len(rig.executor.jobs) == 1

        rig.executor.run()
        app.processEvents()
        published.append(_latest_pixel(rig))
        assert rig.coordinator.intensity_stats()["pending"] is False
        assert rig.coordinator.intensity_stats()["inflight"] == 1
        assert len(rig.executor.jobs) == 1

        rig.executor.run()
        app.processEvents()
        published.append(_latest_pixel(rig))
        stats = rig.coordinator.intensity_stats()
        assert stats["inflight"] == 0
        assert stats["pending"] is False
        assert len({pixel for pixel in published}) >= 3
        assert published[-1] == _expected_pixel(rig)
        assert rig.host.scheduler.reads == reads
    finally:
        rig.binding.disconnect_owners()
        rig.layer.teardown()
        rig.coordinator.shutdown()


@pytest.mark.parametrize("mode", [compose_core.MODE_OVERLAY,
                                   compose_core.MODE_FUSION])
def test_latest_wins_waits_for_every_visible_tile_in_batch(app, mode):
    tiles = ((1, 1), (2, 1), (1, 2))
    rig = _rig(app, mode, tiles=tiles)
    try:
        _settle_initial(app, rig)
        reads = rig.host.scheduler.reads
        with rig.state.using_scope("step1"):
            rig.state.set_mapping("CD3", 50.0, 100.0, 1.0,
                                  origin="test")
        assert len(rig.executor.jobs) == len(tiles)
        for value in (40.0, 30.0, 20.0, 10.0):
            with rig.state.using_scope("step1"):
                rig.state.set_mapping("CD3", 0.0, value, 1.0,
                                      origin="test")
        assert rig.coordinator.intensity_stats()["pending"] is True
        assert len(rig.executor.jobs) == len(tiles)

        rig.executor.run()
        app.processEvents()
        stats = rig.coordinator.intensity_stats()
        assert stats["pending"] is False
        assert stats["inflight"] == len(tiles)
        assert len(rig.executor.jobs) == len(tiles)

        rig.executor.run()
        app.processEvents()
        stats = rig.coordinator.intensity_stats()
        assert stats["inflight"] == 0
        assert stats["pending"] is False
        assert rig.host.scheduler.reads == reads
        for tx, ty in tiles:
            assert _latest_pixel_at(rig, tx, ty) == _expected_pixel_at(
                rig, tx, ty)
    finally:
        rig.binding.disconnect_owners()
        rig.layer.teardown()
        rig.coordinator.shutdown()


@pytest.mark.parametrize("mode", [compose_core.MODE_OVERLAY,
                                   compose_core.MODE_FUSION])
@pytest.mark.parametrize("field", ["min", "max", "gamma"])
def test_final_intensity_pixel_matches_last_mapping_without_reread(
        app, mode, field):
    rig = _rig(app, mode)
    try:
        _settle_initial(app, rig)
        reads = rig.host.scheduler.reads
        values = {
            "min": (0.0, 20.0, 1.0),
            "max": (0.0, 40.0, 1.0),
            "gamma": (0.0, 100.0, 2.0),
        }
        with rig.state.using_scope("step1"):
            rig.state.set_mapping("CD3", *values[field], origin="test")
        rig.executor.run()
        app.processEvents()
        assert _latest_pixel(rig) == _expected_pixel(rig)
        assert rig.host.scheduler.reads == reads
        assert rig.state.mapping("CD3") == values[field]
        assert rig.domain.group_weight("markers") == 1.0
        assert rig.domain.effective_config()["groups"]["markers"]["channels"]["CD8"] == 0.5
    finally:
        rig.binding.disconnect_owners()
        rig.layer.teardown()
        rig.coordinator.shutdown()


def test_mapping_before_raw_tiles_arrive_keeps_one_latest_spec(app):
    rig = _rig(app, compose_core.MODE_OVERLAY)
    try:
        rig.binding.refresh("initial")
        with rig.state.using_scope("step1"):
            rig.state.set_mapping("CD3", 50.0, 100.0, 1.0,
                                  origin="test")
        for value in (40.0, 30.0, 20.0, 10.0):
            with rig.state.using_scope("step1"):
                rig.state.set_mapping("CD3", 0.0, value, 1.0,
                                      origin="test")
        stats = rig.coordinator.intensity_stats()
        assert stats["waiting"] is True
        assert stats["pending"] is True
        assert len(rig.executor.jobs) == 0
        queued = len(rig.host.scheduler._deferred)
        assert queued == 6
        rig.host.scheduler.deliver()
        assert len(rig.host.scheduler._deferred) == 0
        _drain(app, rig)
        stats = rig.coordinator.intensity_stats()
        assert stats["inflight"] == 0
        assert stats["pending"] is False
        assert _latest_pixel(rig) == _expected_pixel(rig)
    finally:
        rig.binding.disconnect_owners()
        rig.layer.teardown()
        rig.coordinator.shutdown()

    rig = _rig(app, compose_core.MODE_OVERLAY)
    try:
        _settle_initial(app, rig)
        with rig.state.using_scope("step1"):
            rig.state.set_mapping("CD3", 0.0, 50.0, 1.0, origin="test")
        assert rig.executor.jobs
        before = _latest_pixel(rig)
        old_generation = rig.coordinator.generation
        rig.coordinator.invalidate("dataset")
        assert rig.coordinator.generation != old_generation
        old_jobs = list(rig.executor.jobs)
        rig.executor.run()
        app.processEvents()
        assert rig.coordinator.intensity_stats()["pending"] is False
        assert rig.layer.pool.entries
        assert rig.coordinator.generation != old_generation
        assert _latest_pixel(rig) == before
        assert all(job[1][0].get("generation") == old_generation
                   for job in old_jobs)
    finally:
        rig.binding.disconnect_owners()
        rig.layer.teardown()
        rig.coordinator.shutdown()
