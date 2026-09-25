"""Block L1 (user ruling 2026-09-25): Step1's Save shows ONE progress -- the
modal dialog. The old bottom bar is kept, hidden, and never shown; its words
go to the terminal."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_save_progress_is_the_dialog_alone(app, tmp_path, capsys):
    """A real Save start (the fusion job, faked as in the isolation tests):
    the dialog is up, the old bar is not, and its words reach the terminal."""
    import test_step1_fusion_isolation as iso
    w = iso._window(app, tmp_path)
    worker = iso._FakeFusion()
    try:
        w.show()
        w._stack.setCurrentIndex(1)
        w._set_step_active(1)
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        QtWidgets.QApplication.processEvents()
        assert w._fusion_dialog is not None and w._fusion_dialog.isVisible()
        assert not w._fusion_bar_widget.isVisible()
        worker.progress.emit(1, 2, "tile 1 of 2")
        for _ in range(10):
            QtWidgets.QApplication.processEvents()
        assert w._fusion_dialog.labelText() == "tile 1 of 2"
        assert not w._fusion_bar_widget.isVisible()
        out = capsys.readouterr().out
        assert "[Step1-Fusion] Starting fusion" in out and "[Step1-Fusion] tile 1 of 2" in out
    finally:
        iso._finish(worker)
        w.close()
