"""One import point for Qt (docs/v17_pyside6_plan.md §4): no file outside
`block01/qt/` names a Qt binding, so the binding changes in one place."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
_BINDING = re.compile(r"^\s*(from|import)\s+(PyQt5|PyQt6|PySide2|PySide6|sip)\b", re.M)


def test_only_the_import_point_names_the_binding():
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {"qt", "native", "docs"} or "__pycache__" in rel.parts:
            continue
        if _BINDING.search(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(str(rel))
    assert offenders == []


def test_the_import_point_offers_the_shared_spellings():
    from block01.qt import QtCore, is_deleted, keep_wrappers_at_exit  # noqa: F401
    from block01.qt.QtCore import Signal, Slot  # noqa: F401
    from block01.qt import QtGui, QtTest, QtWidgets  # noqa: F401
    assert callable(is_deleted)
