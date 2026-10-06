from pathlib import Path

from pcb_weaver.connection_geometry import contacts
from pcb_weaver.repair_geometry import _read, _serialize
from test_clearance import source_board
from test_repair_geometry import segment, tag


def pair(a, b):
    return {"items": [{"uuid": a}, {"uuid": b}]}


def test_actual_copper_edges_not_report_origins(tmp_path):
    path = source_board(tmp_path)
    ast = [n for n in _read(path)[0] if tag(n) != "segment"]
    ast.extend([segment("a", (32, 30), (35, 30), .2, 2), segment("b", (36, 30), (40, 30), .2, 2)])
    path.write_bytes(_serialize(ast))
    before = path.read_bytes()
    result = contacts(path, [pair("a", "b")])[0]
    assert result["classification"] == "same_layer_gap" and result["shared_layers"] == ["F.Cu"]
    assert .799 < result["projected_gap_lower_bound_mm"] <= .8
    assert result["net_copper_items"] == 2
    assert result["route_feasibility"] == "not_evaluated" and not result["manufacturing_authorized"]
    assert path.read_bytes() == before


def test_cross_layer_overlap_is_not_contact(tmp_path):
    path = source_board(tmp_path)
    ast = [n for n in _read(path)[0] if tag(n) != "segment"]
    a, b = segment("a", (32, 30), (35, 30), .2, 2), segment("b", (32, 30), (35, 30), .2, 2)
    next(n for n in b if tag(n) == "layer")[1] = "B.Cu"
    ast.extend([a, b])
    path.write_bytes(_serialize(ast))
    result = contacts(path, [pair("a", "b")])[0]
    assert result["classification"] == "layer_transition" and result["shared_layers"] == []
    assert result["projected_gap_lower_bound_mm"] == 0


def test_unknown_cross_net_and_malformed_pairs_are_unresolved(tmp_path):
    path = source_board(tmp_path)
    findings = [pair("a", "unknown"), pair("a", "b"), {"items": []}]
    assert all(r["status"] == "unresolved" for r in contacts(path, findings))


def test_unrouted_real_pad_pair(tmp_path):
    from pcb_weaver.board import read_board
    import sexpdata
    from uuid import uuid4
    source = Path(__file__).resolve().parents[1] / "examples/two-layer/two-layer.kicad_pcb"
    ast = _read(source)[0]
    for footprint in (n for n in ast if tag(n) == "footprint"):
        for pad in (n for n in footprint if tag(n) == "pad"):
            pad.append([sexpdata.Symbol("uuid"), str(uuid4())])
    path = tmp_path / "pads.kicad_pcb"
    path.write_bytes(_serialize(ast))
    pads = [p for f in read_board(path)["footprints"] for p in f["pads"] if p["net"] == "VIN"]
    result = contacts(path, [pair(pads[0]["uuid"], pads[1]["uuid"])])[0]
    assert result["classification"] == "unrouted_net"
