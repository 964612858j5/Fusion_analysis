"""utils/mesmer_utils: what the application keeps of Mesmer -- the run's
metadata and the membrane channel list (Mesmer runs in the seg_runner engine)."""
import json

from block01.utils.mesmer_utils import (
    MESMER_NUCLEI,
    MESMER_WHOLE_CELL,
    mesmer_metadata,
    parse_channel_list,
)


def test_metadata_round_trips_through_json(tmp_path):
    meta = mesmer_metadata(
        MESMER_WHOLE_CELL,
        {"nuclear_channel": "DAPI", "membrane_channels": "CD45; CD68"},
        None,
        output_mask_path="/tmp/mask.ome.tiff",
        runtime_seconds=1.2,
    )
    path = tmp_path / "segmentation_meta.json"
    path.write_text(json.dumps(meta), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["method"] == MESMER_WHOLE_CELL
    assert loaded["output_mask_path"] == "/tmp/mask.ome.tiff"
    assert loaded["whole_cell_mask_path"] == "/tmp/mask.ome.tiff"
    assert loaded["membrane_channels"] == ["CD45", "CD68"]


def test_nuclei_has_no_whole_cell_mask():
    meta = mesmer_metadata(MESMER_NUCLEI, {}, None, output_mask_path="/x.tif")
    assert meta["whole_cell_mask_path"] == ""


def test_parse_channel_list():
    assert parse_channel_list(None) == []
    assert parse_channel_list("A, B;C") == ["A", "B", "C"]
    assert parse_channel_list(["A", " ", "B"]) == ["A", "B"]
