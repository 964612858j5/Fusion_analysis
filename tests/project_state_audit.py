"""Block A8: the reusable single-writer audit of the project state's current
segmentation run (application §6). `state_writes` wraps the two named
writers with monkeypatch and records (writer, run, region, origin). On code
from before A8 there is no project state; the fixture is then empty-handed
and the static C-a check (`mask_key_reads`) is what turns red there."""

import ast
import os

import pytest


class StateWrites:
    def __init__(self):
        self.events = []

    def clear(self):
        self.events.clear()

    def count(self, kind=None):
        return len([e for e in self.events if kind is None or e[0] == kind])

    def __repr__(self):                                  # pragma: no cover
        return f"StateWrites({self.events!r})"


@pytest.fixture
def state_writes(monkeypatch):
    rec = StateWrites()
    from block01.core.project_state import ProjectState
    real_choose = ProjectState.choose_segmentation_run
    real_clear = ProjectState.clear_segmentation_run

    def choose(self, run_dir, region="", origin="step3"):
        rec.events.append(("choose", os.path.realpath(str(run_dir)) if run_dir else "",
                           region, origin))
        return real_choose(self, run_dir, region, origin)

    def clear(self):
        rec.events.append(("clear", "", "", ""))
        return real_clear(self)
    monkeypatch.setattr(ProjectState, "choose_segmentation_run", choose)
    monkeypatch.setattr(ProjectState, "clear_segmentation_run", clear)
    return rec


def mask_key_reads(path):
    """Every READ of `self._step3_mask_key` / `__dict__.get("_step3_mask_key")`
    in `path` (constraint C-a: it is Step3's display cache, never a source of
    the current run). Assignments are not reads."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    reads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_step3_mask_key" \
                and isinstance(node.ctx, ast.Load):
            reads.append(node.lineno)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "get" \
                and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value == "_step3_mask_key":
            reads.append(node.lineno)
    return reads
