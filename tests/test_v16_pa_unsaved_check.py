"""Block PA-5b fix 1: "is anything unsaved?" reads no pixels. Own module:
page-heavy PyQt suites crash pyqtgraph offscreen when combined."""

import test_step1_fusion_settings_commit as fs
from test_step1_fusion_settings_commit import app  # noqa: F401


def test_the_unsaved_check_reads_no_pixels(app, tmp_path, monkeypatch):  # noqa: F811
    """Fix 1: "is anything unsaved?" compares parameters. A channel nobody
    tuned takes the automatic window the committed snapshot froze for these
    same pixels -- it is not worked out again from the whole slide (34.7 s on
    the GUI thread at the first Step1 entry, A5 synthetic slide)."""
    w = fs._window(app, tmp_path)
    try:
        cache, reads = {}, []

        def mapping(channels=None, blocking=True, resident_only=False, computed_only=False):
            """Step0's answer: automatic windows, worked out from pixels once
            (what `display_mapping_for_preview` does) unless `computed_only`."""
            out = {}
            for ch in channels or ("DAPI", "CD3", "CD8"):
                if ch not in cache and not computed_only:
                    reads.append(ch)                       # a whole-slide read
                    cache[ch] = {"min": 1.0, "max": 100.0 + len(cache), "gamma": 1.0,
                                 "auto": True}
                if ch in cache:
                    out[ch] = dict(cache[ch])
            return out
        monkeypatch.setattr(w._step0, "display_mapping_for_preview", mapping, raising=False)
        fs._enable(w, "CD3", True)
        assert w._commit_fusion_settings() is True
        assert reads                                       # the commit froze them
        assert w._fusion_settings_dirty() is False
        snap = w._committed_fusion_settings()
        assert all(p.get("auto") for p in snap["display_mapping"].values())

        cache.clear()                                      # e.g. a new session
        reads.clear()
        assert w._fusion_settings_dirty() is False
        assert reads == [], "pixels were read for the unsaved check"
        # another pixel binding: the frozen numbers are not borrowed
        bound = dict(snap["bound_to"], correct_run="elsewhere")
        w._display.fusion.install_committed_snapshot(dict(snap, bound_to=bound))
        assert w._fusion_settings_dirty() is True
        assert reads == []
    finally:
        w.close()
