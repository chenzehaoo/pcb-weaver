"""Explicit connector-body overhang and mechanical no-connect parity contracts."""
from copy import deepcopy
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from pcb_weaver.board import read_board
from pcb_weaver.models import Constraints
from pcb_weaver.netlist import inspect_netlist
from pcb_weaver.planning import audit_constraints, edge_envelope
from pcb_weaver.planning import plan_placements, completion_candidates


def test_body_overhang_requires_explicit_reference_and_keeps_pad_edge_rule():
    board = read_board(Path(__file__).resolve().parents[1] / "examples/manufacturing-demo/two-layer.kicad_pcb")
    fp = board["footprints"][0]
    intent = Constraints(board={"component_gap_mm":0}).model_dump()
    fp["bounds"][0] = board["outline"]["bounds"][0]-5
    before = audit_constraints(board,intent)
    assert any(v["code"] == "edge_clearance" and v["reference"] == fp["reference"] for v in before["violations"])
    intent["edge_overhang_references"] = [fp["reference"]]
    allowed = audit_constraints(board,intent)
    assert not any(v["code"] == "edge_clearance" and v["reference"] == fp["reference"] for v in allowed["violations"])
    fp["pads"][0]["x"] = board["outline"]["bounds"][0]
    assert any(v["code"] == "edge_clearance" for v in audit_constraints(board,intent)["violations"])


def test_overhang_unknown_reference_and_no_pads_fail():
    board = read_board(Path(__file__).resolve().parents[1] / "examples/manufacturing-demo/two-layer.kicad_pcb")
    rules = Constraints(edge_overhang_references=["UNKNOWN"]).model_dump()
    assert any(v["code"] == "missing_reference" for v in audit_constraints(board,rules)["violations"])
    fp = deepcopy(board["footprints"][0])
    fp["pads"] = []
    with pytest.raises(ValueError):
        edge_envelope(fp,{"edge_overhang_references":[fp["reference"]]})


def test_completion_variants_are_distinct_bounded_and_keep_fixed_poses():
    board = read_board(Path(__file__).resolve().parents[1] / "examples/manufacturing-demo/two-layer.kicad_pcb")
    anchor = board["footprints"][0]
    anchor["pads"] = anchor["pads"] * 4
    rules = Constraints(fixed_references=[anchor["reference"]],placement={"algorithm":"legalize"}).model_dump()
    plan = plan_placements(board,rules,3)
    result = completion_candidates(board,rules,plan,3,.5)
    assert len(result["candidates"]) >= 2
    baseline = {p["reference"]:p for p in result["candidates"][0]["placements"]}
    signatures = set()
    for candidate in result["candidates"]:
        assert candidate["feasible"] and not candidate["violations"]
        for p in candidate["placements"]:
            old = baseline[p["reference"]]
            assert (p["x"]-old["x"])**2+(p["y"]-old["y"])**2 <= 1.000001
            assert p["rotation"] == old["rotation"]
            if p["reference"] == anchor["reference"]:
                assert p == old
        signatures.add(str(candidate["placements"]))
    assert len(signatures) == len(result["candidates"])
    assert completion_candidates(board,rules,plan,3,0) == plan
    with pytest.raises(ValueError):
        completion_candidates(board,rules,plan,3,2.1)


@pytest.mark.parametrize("change,passed", [(None,True),("plated",False),("numbered",False),("connected",False),
    ("shared",False),("no_marker",False),("missing",False),("wrong_value",False)])
def test_only_explicit_singleton_nonplated_mechanical_pads_match(tmp_path,change,passed):
    fp = {"reference":"H1","value":"Mount","footprint":"Local:Hole","pads":[
        {"number":"","net":"","type":"np_thru_hole"}]}
    root = ET.fromstring('<export><components><comp ref="H1"><value>Mount</value><footprint>Local:Hole</footprint></comp></components><nets><net name="unconnected"><node ref="H1" pin="" pintype="passive+no_connect"/></net></nets></export>')
    if change == "plated": fp["pads"][0]["type"] = "thru_hole"
    if change == "numbered": fp["pads"][0]["number"] = "1"
    if change == "connected": fp["pads"][0]["net"] = "GND"
    if change == "shared": ET.SubElement(root.find("./nets/net"),"node",ref="H2",pin="1")
    if change == "no_marker": root.find("./nets/net/node").set("pintype","passive")
    if change == "missing": fp["pads"] = []
    if change == "wrong_value": fp["value"] = "Different"
    path = tmp_path / "netlist.xml"
    ET.ElementTree(root).write(path,encoding="utf-8")
    result = inspect_netlist(path,{"footprints":[fp]})
    assert (result["status"] == "passed") == passed
