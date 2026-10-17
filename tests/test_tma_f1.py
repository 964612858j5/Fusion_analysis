"""TMA F1 (docs/tma_foundation_application.md §4 F1): the map sheet, the
study record and the core table.

Synthetic workbooks (tmp_path) in the TMA designer's layout: a valid one
with an empty position and an optional Spot Slide column, and one broken
workbook per kind of problem, each refused with its location. The four
real map sheets (read only) are parsed when present. The study and the
core table are made on a synthetic project.
"""

import json
import os

import numpy as np
import pytest

openpyxl = pytest.importorskip("openpyxl")
tifffile = pytest.importorskip("tifffile")

from block01.core import provenance as prov  # noqa: E402
from block01.core.tma import cores, map_sheet as ms, study  # noqa: E402

ROWS, COLS = 3, 2          # positions 1..6; 6 left empty


def _workbook(path, *, rows=ROWS, cols=COLS, spot=True, empty=(6,), mutate=None):
    """A map sheet like TMA138.x: Lookup, RB_1, RB_1_Donor, Information."""
    donor = {n: ("Liver" if n == 1 else f"D{(n + 1) // 2}") for n in range(1, rows * cols + 1)
             if n not in empty}
    spec = {"rows": rows, "cols": cols, "donor": donor, "spot": spot,
            "info": {"TMA block name": "TMA_T", "Layout name": f"{rows}x{cols}",
                     "Layout size (row x column)": f"{rows}x{cols}", "Diameter": "1.0",
                     "Number of Donor": str(len(set(donor.values()))), "Number of Core": str(len(donor))}}
    if mutate:
        mutate(spec)
    wb = openpyxl.Workbook()
    look = wb.active
    look.title = "Lookup"
    head = ["Unique TMA sample ID", "TMA Block ID", "TMA Block Name", "Donor Block ID", "Donor Core Height",
            "Annotation Name"] + (["Spot Slide"] if spec["spot"] else [])
    look.append(head)
    for n in spec.get("ids", range(1, rows * cols + 1)):
        x, y = (n - 1) // rows + 1, (n - 1) % rows + 1
        d = spec["donor"].get(n, "")
        row = [n, "RB_1", "TMA_T", d, "3.0 mm" if d else "", "TMA 1" if d else ""]
        if spec["spot"]:
            row.append(spec.get("spot_override", {}).get(n, f"{x}_({y};{x})_{d}" if d and n != 1 else ""))
        look.append(row)
    grid, gdon = wb.create_sheet("RB_1"), wb.create_sheet("RB_1_Donor")
    for ws in (grid, gdon):
        ws.append(["TMA_T"] + [f"X{x}" for x in range(1, spec.get("grid_cols", cols) + 1)])
    for y in range(1, spec.get("grid_rows", rows) + 1):
        grid.append([f"Y{y}"] + [spec.get("grid_ids", {}).get((x, y), (x - 1) * rows + y)
                                 if (x - 1) * rows + y in spec["donor"] else None for x in range(1, cols + 1)])
        gdon.append([f"Y{y}"] + [spec.get("grid_donors", {}).get((x, y), spec["donor"].get((x - 1) * rows + y))
                                 for x in range(1, cols + 1)])
    info = wb.create_sheet("Information")
    info.append([" ", "RB_1"])
    for k, v in spec["info"].items():
        info.append([k, v])
    wb.save(path)
    return str(path)


def test_a_valid_sheet_is_read_with_its_empty_position(tmp_path):
    m = ms.parse(_workbook(tmp_path / "ok.xlsx"))
    assert (m.rows, m.cols, m.block_id) == (ROWS, COLS, "RB_1")
    assert [p.position_id for p in m.occupied()] == [1, 2, 3, 4, 5]
    assert not m.positions[6].occupied and m.positions[6].label == "X2-Y03"
    assert m.positions[4].label == "X2-Y01" and m.positions[1].donor_block_id == "Liver"
    assert m.has_spot_slide and len(m.sha256) == 64


def test_the_spot_slide_column_is_optional(tmp_path):
    m = ms.parse(_workbook(tmp_path / "nospot.xlsx", spot=False))
    assert not m.has_spot_slide and len(m.occupied()) == 5


@pytest.mark.parametrize("name,mutate,needle", [
    ("repeated id", lambda s: s.update(ids=[1, 2, 2, 4, 5, 6]), "repeated"),
    ("missing row", lambda s: s.update(ids=[1, 2, 3, 4, 5]), "have no row"),
    ("grid size", lambda s: s["info"].update({"Layout size (row x column)": "4x2"}), "Information says"),
    ("wrong numbering", lambda s: s.update(grid_ids={(2, 1): 9}), "layout numbering gives"),
    ("donor mismatch", lambda s: s.update(grid_donors={(1, 2): "OTHER"}), "in Lookup"),
    ("occupied in one sheet only", lambda s: s.update(grid_donors={(2, 3): "D9"}), "occupied only in"),
    ("core count", lambda s: s["info"].update({"Number of Core": "7"}), "Number of Core"),
    ("donor count", lambda s: s["info"].update({"Number of Donor": "9"}), "Number of Donor"),
    ("spot slide", lambda s: s.update(spot_override={3: "1_(2;1)_D2"}), "Spot Slide"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_a_broken_sheet_is_refused_with_its_location(tmp_path, name, mutate, needle):
    with pytest.raises(ms.MapSheetError) as err:
        ms.parse(_workbook(tmp_path / "bad.xlsx", mutate=mutate))
    assert any(needle in p for p in err.value.problems), err.value.problems


def test_a_file_that_is_not_a_workbook_is_refused(tmp_path):
    p = tmp_path / "x.xlsx"
    p.write_text("not a workbook")
    with pytest.raises(ms.MapSheetError):
        ms.parse(str(p))


REAL = "/sdb1/Fusion/TMA_cores"
EXPECT = {1: (149, 47, True, "Liver"), 2: (151, 41, True, "Kidney"),
          3: (149, 27, False, "Kidney"), 4: (148, 53, False, "Kidney")}


@pytest.mark.skipif(not os.path.isdir(REAL), reason="real map sheets not on this machine")
@pytest.mark.parametrize("x", [1, 2, 3, 4])
def test_the_four_real_map_sheets_are_valid(x):
    m = ms.parse(os.path.join(REAL, f"TMA138.{x}.xlsx"))
    occ = m.occupied()
    cores_n, donors_n, spot, first = EXPECT[x]
    assert (m.rows, m.cols) == (19, 8)
    assert (len(occ), len({p.donor_block_id for p in occ}), m.has_spot_slide) == (cores_n, donors_n, spot)
    assert occ[0].position_id == 1 and occ[0].donor_block_id == first
    assert occ[-1].donor_block_id == occ[-2].donor_block_id          # the two last punches: one donor


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "project_manifest.json").write_text(json.dumps({"version": 1, "project_schema_version": 1}))
    slide = tmp_path / "s.ome.tif"
    tifffile.imwrite(str(slide), np.zeros((2, 64, 64), np.uint8), ome=True,
                     metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}})
    return str(d), str(slide)


def test_the_study_is_written_registered_and_not_overwritten(project, tmp_path):
    proj, slide = project
    sheet = _workbook(tmp_path / "ok.xlsx")
    rec = study.create_study(proj, slide, sheet)
    assert rec == study.load_study(proj)
    assert rec["slide_id"].startswith("slide_") and rec["map_sheet"]["rows"] == ROWS
    kinds = {e["kind"]: e for e in prov.load_entries(proj)}
    assert kinds["tma_map_sheet"]["token"] == rec["map_sheet"]["sha256"]
    assert kinds["tma_map_sheet"]["slide_id"] == rec["slide_id"]
    assert study.create_study(proj, slide, sheet) == rec               # same inputs: as it is
    other = _workbook(tmp_path / "other.xlsx", spot=False)
    with pytest.raises(study.StudyError):
        study.create_study(proj, slide, other)                         # another map sheet: refused


def test_a_broken_map_sheet_writes_no_study(project, tmp_path):
    proj, slide = project
    bad = _workbook(tmp_path / "bad.xlsx", mutate=lambda s: s.update(ids=[1, 2, 3, 4, 5]))
    with pytest.raises(ms.MapSheetError):
        study.create_study(proj, slide, bad)
    assert study.load_study(proj) is None


def test_the_draft_core_table_has_every_position_and_stable_keys(project, tmp_path):
    proj, slide = project
    rec = study.create_study(proj, slide, _workbook(tmp_path / "ok.xlsx"))
    rows = cores.draft_rows(rec)
    assert [r["position_id"] for r in rows] == list(range(1, ROWS * COLS + 1))
    assert [r["occupied"] for r in rows] == [True] * 5 + [False]
    assert all(r["region_id"] == "" and r["layout_version"] == 0 for r in rows)
    path = cores.write_table(proj, rows, 0)
    back = cores.read_table(path)
    assert back == rows
    assert cores.draft_rows(rec)[2]["position_key"] == rows[2]["position_key"]     # stable
    other = dict(rec, map_sheet=dict(rec["map_sheet"], sha256="0" * 64))
    assert cores.draft_rows(other)[2]["position_key"] != rows[2]["position_key"]  # new sheet, new keys


def test_a_core_table_of_another_schema_is_refused(project, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    proj, _ = project
    path = os.path.join(proj, "tma", "x.parquet")
    os.makedirs(os.path.dirname(path))
    pq.write_table(pa.table({"position_key": ["a"]}), path)
    with pytest.raises(cores.CoreTableError):
        cores.read_table(path)


@pytest.mark.parametrize("name,mutate,needle", [
    ("fractional grid id", lambda s: s.update(grid_ids={(1, 1): 1.9}), "is not a number"),
    ("fractional lookup id", lambda s: s.update(ids=[1, 2, 3.5, 4, 5, 6]), "is not a number"),
    ("spot slide on an empty position", lambda s: s.update(spot_override={6: "2_(3;2)_D9"}), "empty position"),
    ("unreadable core count", lambda s: s["info"].update({"Number of Core": "many"}), "not a whole number"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_more_broken_sheets_are_refused(tmp_path, name, mutate, needle):
    with pytest.raises(ms.MapSheetError) as err:
        ms.parse(_workbook(tmp_path / "bad.xlsx", mutate=mutate))
    assert any(needle in p for p in err.value.problems), err.value.problems


def test_another_slide_is_refused_and_the_study_stays(project, tmp_path):
    proj, slide = project
    sheet = _workbook(tmp_path / "ok.xlsx")
    rec = study.create_study(proj, slide, sheet)
    before = open(study.study_path(proj)).read()
    other = tmp_path / "t.ome.tif"
    tifffile.imwrite(str(other), np.ones((2, 32, 32), np.uint8), ome=True,
                     metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}})
    with pytest.raises(study.StudyError):
        study.create_study(proj, str(other), sheet)
    assert open(study.study_path(proj)).read() == before and study.load_study(proj) == rec


def test_the_core_table_writer_and_reader_refuse_inconsistent_rows(project, tmp_path):
    proj, slide = project
    rows = cores.draft_rows(study.create_study(proj, slide, _workbook(tmp_path / "ok.xlsx")))
    with pytest.raises(cores.CoreTableError):
        cores.write_table(proj, rows[:1] + rows[:1], 0)                       # repeated key
    with pytest.raises(cores.CoreTableError):
        cores.write_table(proj, rows, 1)                                      # version mismatch
    with pytest.raises(cores.CoreTableError):
        cores.write_table(proj, [dict(rows[0], extra=1)], 0)                  # wrong columns
    from block01.core.object_tables import write_typed_table
    bad = os.path.join(proj, "tma", "dup.parquet")
    write_typed_table(bad, cores.TYPES, rows[:1] + rows[:1], cores.SCHEMA_NAME, cores.SCHEMA)
    with pytest.raises(cores.CoreTableError):
        cores.read_table(bad)                                                 # read side checks too


def test_the_core_table_reader_refuses_mixed_versions_and_wrong_types(project, tmp_path):
    proj, slide = project
    rows = cores.draft_rows(study.create_study(proj, slide, _workbook(tmp_path / "ok.xlsx")))
    from block01.core.object_tables import write_typed_table
    mixed = os.path.join(proj, "tma", "mixed.parquet")
    write_typed_table(mixed, cores.TYPES, [rows[0], dict(rows[1], layout_version=1)],
                      cores.SCHEMA_NAME, cores.SCHEMA)
    with pytest.raises(cores.CoreTableError):
        cores.read_table(mixed)                                               # two layout versions
    typed = tuple((c, "string") if c == "position_id" else (c, t) for c, t in cores.TYPES)
    wrong = os.path.join(proj, "tma", "wrong.parquet")
    write_typed_table(wrong, typed, [dict(r, position_id=str(r["position_id"])) for r in rows],
                      cores.SCHEMA_NAME, cores.SCHEMA)
    with pytest.raises(cores.CoreTableError):
        cores.read_table(wrong)                                               # right schema name, wrong type
