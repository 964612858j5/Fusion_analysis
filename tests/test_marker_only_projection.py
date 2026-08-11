"""v14.5d Workstream A — Step2 marker-only projection of a Step0 remap config.

The saved Step0 config carries ALL channels (incl the DAPI reference layer, which
Step1 remap + the DAPI cache depend on). Step2 marker promotion runs on a marker-only
projection: selected HQ2/CSD markers minus reference layers, with two invariants —
(1) a selected marker absent from the saved config is an explicit uncovered-marker
refusal (never a silent intersection drop); (2) the mixture is recomputed from the
selected markers, never inherited from the full-config top-level value."""

import copy

from block01.utils.remap_promotion import project_marker_only_config
from block01.utils.source_identity import (
    SOURCE_MIXTURE_HOMOGENEOUS_RAW, SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED,
    SOURCE_MIXTURE_MIXED)


def _ch(kind="raw_ome"):
    return {"min": 100.0, "max": 5000.0, "gamma": 1.0,
            "calibration_source_identity": {"actual_source_kind": kind,
                                            "channel_name": "x"}}


def _saved(**kinds):
    """A Step0-like config: always includes a DAPI reference channel + a stale
    full-config top-level mixed mixture."""
    channels = {"DAPI": _ch("raw_ome")}
    for name, k in kinds.items():
        channels[name] = _ch(k)
    return {"channels": channels,
            "source_policy": {"source_mixture_mode": "mixed_raw_corrected"},
            "source_mixture_mode": "mixed_raw_corrected",
            "used_for": "segmentation_only"}


def test_projection_selects_only_markers_drops_dapi():
    saved = _saved(PanCK="corrected_zarr", CD45="corrected_zarr", TOX="raw_ome")
    proj, rep = project_marker_only_config(saved, ["PanCK", "CD45"])
    assert rep["projected"]
    assert set(proj["channels"]) == {"PanCK", "CD45"}   # no DAPI, no unselected TOX
    assert rep["marker_channels"] == ["PanCK", "CD45"]


def test_projection_uncovered_marker_refused():
    # invariant 1: a selected marker not in the saved config must REFUSE, not drop
    saved = _saved(PanCK="corrected_zarr")
    proj, rep = project_marker_only_config(saved, ["PanCK", "CD45"])
    assert proj is None
    assert any("uncovered-marker" in f and "CD45" in f for f in rep["failures"])


def test_projection_reference_only_selection_refused():
    saved = _saved(PanCK="corrected_zarr")
    proj, rep = project_marker_only_config(saved, ["DAPI"])
    assert proj is None
    assert any("non-reference" in f for f in rep["failures"])


def test_projection_recomputes_homogeneous_mixture_from_selected():
    # invariant 2: full config is mixed, but both selected markers are corrected
    saved = _saved(PanCK="corrected_zarr", CD45="corrected_zarr", TOX="raw_ome")
    assert saved["source_mixture_mode"] == "mixed_raw_corrected"
    proj, rep = project_marker_only_config(saved, ["PanCK", "CD45"])
    assert rep["intended_source_mixture_mode"] == SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED
    # stale full-config mixture dropped so it can't leak onto the homogeneous subset
    assert "source_mixture_mode" not in proj
    assert "source_mixture_mode" not in proj.get("source_policy", {})


def test_projection_homogeneous_raw():
    saved = _saved(TOX="raw_ome", TIM3="raw_ome")
    _proj, rep = project_marker_only_config(saved, ["TOX", "TIM3"])
    assert rep["intended_source_mixture_mode"] == SOURCE_MIXTURE_HOMOGENEOUS_RAW


def test_projection_mixed_when_selected_span_both():
    saved = _saved(PanCK="corrected_zarr", TOX="raw_ome")
    _proj, rep = project_marker_only_config(saved, ["PanCK", "TOX"])
    assert rep["intended_source_mixture_mode"] == SOURCE_MIXTURE_MIXED


def test_projection_none_mixture_when_identity_missing():
    saved = {"channels": {"PanCK": {"min": 1.0, "max": 2.0}},
             "used_for": "segmentation_only"}
    _proj, rep = project_marker_only_config(saved, ["PanCK"])
    assert rep["projected"]
    assert rep["intended_source_mixture_mode"] is None  # no calibration_source_identity


def test_projection_preserves_calibration_identity():
    saved = _saved(PanCK="corrected_zarr", TOX="raw_ome")
    proj, _ = project_marker_only_config(saved, ["PanCK"])
    csi = proj["channels"]["PanCK"]["calibration_source_identity"]
    assert csi["actual_source_kind"] == "corrected_zarr"


def test_projection_does_not_mutate_input():
    saved = _saved(PanCK="corrected_zarr", CD45="raw_ome")
    before = copy.deepcopy(saved)
    project_marker_only_config(saved, ["PanCK"])
    assert saved == before


def test_projection_dedups_repeated_selection_preserving_order():
    saved = _saved(PanCK="corrected_zarr", CD45="corrected_zarr")
    proj, rep = project_marker_only_config(saved, ["CD45", "PanCK", "CD45"])
    assert rep["marker_channels"] == ["CD45", "PanCK"]   # deduped, order kept
    assert set(proj["channels"]) == {"CD45", "PanCK"}


def test_projection_accepts_semicolon_string():
    saved = _saved(PanCK="corrected_zarr", CD45="corrected_zarr")
    proj, _rep = project_marker_only_config(saved, "PanCK;CD45")
    assert set(proj["channels"]) == {"PanCK", "CD45"}
