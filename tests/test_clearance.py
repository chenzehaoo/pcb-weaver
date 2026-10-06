from pathlib import Path

import pytest
from shapely.geometry import LineString

from pcb_weaver.clearance import propose
from pcb_weaver.repair_geometry import _read, _board, _serialize
from test_repair_geometry import load, segment, tag


def source_board(tmp_path, locked=False):
    ast = load(Path(__file__).resolve().parents[1] / "examples/two-layer/two-layer.kicad_pcb")
    ast = [n for n in ast if tag(n) not in {"footprint", "segment", "via"}]
    ast.extend([segment("a", (32, 30), (37, 30), .2, 2, "(locked yes)" if locked else ""),
                segment("a2", (37, 30), (42, 30), .2, 2, "(locked yes)" if locked else ""),
                segment("b", (32, 30.33), (42, 30.33), .2, 1)])
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(_serialize(ast))
    return source


def test_nudge_preserves_shared_joints_and_other_copper(tmp_path):
    source = source_board(tmp_path)
    before = source.read_bytes()
    output = tmp_path / "output.kicad_pcb"
    result = propose(source, output, ["VIN"], [30, 25, 45, 35])
    assert result["status"] == "proposed", result
    assert result["non_endpoint_ast_preserved"] and result["outside_scope_copper_preserved"]
    assert source.read_bytes() == before
    old = {c["id"]: c for c in _board(_read(source)[0], source=True)["copper"]}
    new = {c["id"]: c for c in _board(_read(output)[0], source=True)["copper"]}
    assert new["b"]["node"] == old["b"]["node"]
    assert new["a"]["geometry"]["end"] == new["a2"]["geometry"]["start"]
    for key in ("a", "a2"):
        g, wall = new[key]["geometry"], new["b"]["geometry"]
        assert g["width"] == old[key]["geometry"]["width"]
        assert LineString([g["start"], g["end"]]).distance(LineString([wall["start"], wall["end"]])) >= .35
    assert not result["manufacturing_authorized"] and result["requires_native_verification"]


@pytest.mark.parametrize("value", [True, 0, -1, .3, float("nan"), float("inf"), "0.08"])
def test_invalid_movement_rejected(tmp_path, value):
    source = source_board(tmp_path)
    with pytest.raises(ValueError):
        propose(source, tmp_path / "out.kicad_pcb", ["VIN"], [30, 25, 45, 35], max_move_mm=value)


def test_locked_copper_and_existing_outputs_are_not_modified(tmp_path):
    source = source_board(tmp_path, locked=True)
    before = source.read_bytes()
    with pytest.raises(ValueError):
        propose(source, tmp_path / "out.kicad_pcb", ["VIN"], [30, 25, 45, 35])
    with pytest.raises(ValueError):
        propose(source, source, ["VIN"], [30, 25, 45, 35])
    assert source.read_bytes() == before


def test_insufficient_movement_is_blocked_without_output(tmp_path):
    source = source_board(tmp_path)
    output = tmp_path / "out.kicad_pcb"
    result = propose(source, output, ["VIN"], [30, 25, 45, 35], max_move_mm=.005)
    assert result["status"] == "blocked" and not output.exists()


def test_fixed_vertex_precision_survives_rounding(tmp_path):
    source = source_board(tmp_path)
    ast = _read(source)[0]
    ast = [n for n in ast if tag(n) != "segment"]
    x = 32.00000004
    ast.extend([segment("a", (x, 30), (37, 30), .2, 2),
                segment("a2", (37, 30), (42, 30), .2, 2),
                segment("anchor", (x, 24), (x, 30), .2, 2),
                segment("b", (35, 30.33), (42, 30.33), .2, 1)])
    source.write_bytes(_serialize(ast))
    output = tmp_path / "output.kicad_pcb"
    result = propose(source, output, ["VIN"], [30, 25, 45, 35])
    assert result["status"] == "proposed", result
    copper = {c["id"]: c["geometry"] for c in _board(_read(output)[0], source=True)["copper"]}
    assert copper["a"]["start"] == [x, 30]
    assert copper["anchor"]["end"] == copper["a"]["start"]


def test_interior_pad_joint_can_move_while_pad_is_fixed(tmp_path, monkeypatch):
    from pcb_weaver import clearance
    source = source_board(tmp_path)
    original = clearance.read_board
    def with_pad(path):
        board = original(path)
        board["footprints"] = [{"pads": [{"size": [.3, .3], "shape": "rect", "rotation": 0,
            "x": 37.05, "y": 30, "net": "VIN", "layers": ["F.Cu"]}]}]
        return board
    monkeypatch.setattr(clearance, "read_board", with_pad)
    result = propose(source, tmp_path / "out.kicad_pcb", ["VIN"], [30, 25, 45, 35])
    assert result["status"] == "proposed"
    assert ("VIN", 37.0, 30.0) in result["free_joints"]


def test_constant_geometry_does_not_make_variable_clearance_problem_infeasible(tmp_path):
    source = source_board(tmp_path)
    ast = _read(source)[0]
    ast.extend([segment("fixed",(32,33),(34,33),.2,1),
                segment("anchor1",(32,33),(32,40),.2,1),
                segment("anchor2",(34,33),(34,40),.2,1),
                segment("static-neighbor",(32,33.351),(34,33.351),.2,2,"(locked yes)")])
    source.write_bytes(_serialize(ast))
    output = tmp_path / "out.kicad_pcb"
    result = propose(source,output,["VIN","GND"],[30,25,45,35])
    assert result["status"] == "proposed",result
    assert result["constant_segments"] >= 1 and result["requires_native_verification"]
    old = {c["id"]:c["node"] for c in _board(_read(source)[0],source=True)["copper"]}
    new = {c["id"]:c["node"] for c in _board(_read(output)[0],source=True)["copper"]}
    for key in ("fixed","anchor1","anchor2","static-neighbor"):
        assert old[key] == new[key]
