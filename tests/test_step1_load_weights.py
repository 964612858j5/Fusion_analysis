"""Block F (user ruling 2026-09-25): `Load weights` reads a Step1 session only.

A user loaded step1_session.json through `Load weights`, which then read
`fusion_config.json`'s shape (top-level `groups`): the session nests them,
so an empty draft was installed -- every marker out, DAPI alone -- and a
channel ticked afterwards had no group to join, so the viewer showed DAPI
only and Save Fusion Settings refused.

Now `Load weights` takes a Step1 session and puts its channel part back
exactly as `Load Previous Step1 Session` does (the same two calls); any
other file is refused and nothing changes.
"""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402

import test_fusion_domain_model as fdm  # noqa: E402  (the Step1 window harness)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _state(w):
    model = w._display.fusion
    names = w.loader.channel_names()
    return {
        "enabled": sorted(model.enabled_channels()),
        "weights": {ch: model.channel_weight(ch) for ch in names},
        "groups": {g: dict(v) for g, v in model.groups().items()},
        "visible": {ch: bool(w._channel_dock.row(ch).checkbox.isChecked())
                    for ch in names if w._channel_dock.row(ch) is not None},
    }


def _session_from(w, tmp_path, enabled=("CD8",), weights=(("CD8", 0.6),)):
    """A real session payload of a window whose user ticked and weighted."""
    model = w._display.fusion
    for ch, v in weights:
        model.edit_channel_weight(ch, v)
    for ch in enabled:
        w._channel_dock.row(ch).checkbox.setChecked(True)
    fdm._pump()
    path = tmp_path / "step1_session.json"
    path.write_text(json.dumps(w._step1_session_payload()), encoding="utf-8")
    return str(path)


def test_a_session_loads_like_load_previous_step1_session(app, tmp_path, monkeypatch):
    src = fdm._window(app)
    try:
        path = _session_from(src, tmp_path)
        want_sess = json.loads(open(path, encoding="utf-8").read())
    finally:
        src.close()

    a = fdm._window(app)                                   # Load weights
    b = fdm._window(app)                                   # the session restore path
    try:
        ok, msg = a.load_weights_from_step1_session(path)
        assert ok, msg
        restored = b._restore_step1_scientific_state(want_sess, source_path="")
        b._apply_step1_display_state(want_sess, restored.visibility, display_installed=True,
                                     restore_changed=restored.changed,
                                     mode_moved=restored.mode_moved)
        fdm._pump()
        assert _state(a) == _state(b)
        assert "CD8" in _state(a)["enabled"] and _state(a)["weights"]["CD8"] == pytest.approx(0.6)
        assert _state(a)["visible"]["CD8"] is True
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("name,payload", [
    ("fusion_config.json", {"groups": {"markers": {"channels": {"CD3": 1.0}}},
                            "nucleus": {"channel": "DAPI", "weight": 1.0}}),
    ("step1_fusion_settings.json", {"version": 1, "hash": "h",
                                    "fusion_config": {"groups": {"markers": {"channels": {"CD3": 1.0}}}},
                                    "display_mapping": {}}),
    ("other.json", {"anything": 1}),
])
def test_any_other_file_is_refused_and_nothing_changes(app, tmp_path, name, payload):
    w = fdm._window(app)
    try:
        before = _state(w)
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        ok, msg = w.load_weights_from_step1_session(str(path))
        assert not ok and "not a Step1 session" in msg
        assert _state(w) == before
    finally:
        w.close()


def test_a_session_with_no_marker_of_this_slide_is_refused(app, tmp_path):
    w = fdm._window(app)
    try:
        before = _state(w)
        sess = {"version": 1, "channel_weights": {"DAPI": 1.0, "NOT_HERE": 0.5}}
        path = tmp_path / "step1_session.json"
        path.write_text(json.dumps(sess), encoding="utf-8")
        ok, msg = w.load_weights_from_step1_session(str(path))
        assert not ok and "no marker channel" in msg
        assert _state(w) == before
    finally:
        w.close()


def test_after_loading_a_new_tick_enters_the_fusion(app, tmp_path):
    """The reported chain: after the load, a channel ticked in Step1 is in a
    group, so it is drawn in the fusion and Save has a marker to save."""
    src = fdm._window(app)
    try:
        path = _session_from(src, tmp_path, enabled=("CD8",))
    finally:
        src.close()
    w = fdm._window(app)
    try:
        ok, msg = w.load_weights_from_step1_session(path)
        assert ok, msg
        w._channel_dock.row("CD3").checkbox.setChecked(True)
        fdm._pump()
        model = w._display.fusion
        assert model.fusion_enabled("CD3")
        assert any("CD3" in chans for chans in model.groups().values())
    finally:
        w.close()
