from copy import deepcopy

from pcb_weaver.board import compare_boards
from pcb_weaver.eco import analyze_impact


def board():
    return {"footprints": [{"reference": ref, "value": "1k", "footprint": "R", "locked": False,
                            "x": x, "y": 0, "rotation": 0, "layer": "F.Cu", "pads": [{"number": "1", "net": net}]}
                           for ref, x, net in [("R1", 0, "SIG"), ("R2", 5, "SIG"), ("R3", 10, "OTHER")]],
            "track_items": [], "via_items": []}


def test_move_traces_direct_and_one_hop_impact_only():
    a = board()
    b = deepcopy(a)
    b["footprints"][0]["x"] = 1
    result = analyze_impact(a, b, compare_boards(a, b), {}, ["board.kicad_pcb"], False)
    assert result["direct_references"] == ["R1"]
    assert result["affected_references"] == ["R1", "R2"]
    assert [n["net"] for n in result["affected_nets"]] == ["SIG"]
    assert "full_board_DRC" in result["required_checks"]


def test_copper_change_without_component_move_still_impacts_net():
    a = board()
    b = deepcopy(a)
    b["track_items"] = [{"net": "SIG", "start": [0, 0], "end": [5, 0], "width_mm": 0.25}]
    result = analyze_impact(a, b, compare_boards(a, b), {}, ["board.kicad_pcb"], False)
    assert result["affected_nets"][0]["reasons"] == ["copper_geometry_changed"]


def test_global_rule_change_invalidates_all_networks():
    a = board()
    result = analyze_impact(a, a, compare_boards(a, a), {}, ["board.kicad_pro"], False)
    assert {n["net"] for n in result["affected_nets"]} == {"SIG", "OTHER"}
    assert result["global_rules_changed"]


def test_identical_revision_has_no_invalidated_artifacts():
    a = board()
    result = analyze_impact(a, a, compare_boards(a, a), {}, [], False)
    assert result["affected_nets"] == []
    assert result["invalidated_artifacts"] == []
