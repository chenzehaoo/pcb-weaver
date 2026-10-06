from copy import deepcopy
import json
from pathlib import Path

import pytest

from pcb_weaver.board import read_board, write_placements
from pcb_weaver.planning import audit_constraints, plan_placements


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "two-layer"


@pytest.mark.parametrize("algorithm,expected", [
    ("legalize", ["candidate-002", "candidate-001", "candidate-003", "candidate-004"]),
    ("block_coordinate", ["candidate-003", "candidate-002", "candidate-001", "candidate-004"]),
])
def test_candidate_ranking_respects_algorithm_objective(tmp_path, monkeypatch, algorithm, expected):
    import numpy as np
    from test_two_sided_geometry import RULES, pair_board

    board, _ = pair_board(tmp_path, second_side="F")
    rules = {**deepcopy(RULES), "placement": {"algorithm": algorithm}}
    # Solver outputs are controlled, but feasibility, displacement and pad HPWL
    # are computed by the real audit. The input overlaps, so no fallback applies.
    positions = [(39, 49), (41, 49), (30, 37), (40, 40)]

    def solved(board, constraints, refs, bounds, options, index):
        assert refs == ["A", "B"]
        a, b = positions[index]
        return np.array([a, 35, b, 35], dtype=float), {"method": "controlled test output"}

    function = "legalize_minimum_displacement" if algorithm == "legalize" else "optimize_blocks"
    monkeypatch.setattr("pcb_weaver.placement_large." + function, solved)
    candidates = plan_placements(board, rules, count=4)["candidates"]
    assert [candidate["id"] for candidate in candidates] == expected
    by_id = {candidate["id"]: candidate for candidate in candidates}
    assert [by_id[f"candidate-{i:03d}"]["metrics"]["squared_displacement_mm2"] for i in range(1, 5)] == [82, 82, 109, 0]
    assert [by_id[f"candidate-{i:03d}"]["metrics"]["weighted_hpwl_mm"] for i in range(1, 5)] == [14, 12, 11, 4]
    assert all(candidate["feasible"] for candidate in candidates[:-1])
    assert not candidates[-1]["feasible"]


@pytest.fixture
def board():
    return read_board(EXAMPLE / "two-layer.kicad_pcb")


@pytest.fixture
def constraints():
    return json.loads((EXAMPLE / "constraints.json").read_text())


def test_audit_reports_initial_proximity_and_real_hpwl(board, constraints):
    audit = audit_constraints(board, constraints)
    assert not audit["passed"]
    assert any(v["code"] == "proximity" for v in audit["violations"])
    expected = sum(max(p["x"] for p in net["pads"]) - min(p["x"] for p in net["pads"])
                   + max(p["y"] for p in net["pads"]) - min(p["y"] for p in net["pads"]) for net in board["nets"])
    assert audit["metrics"]["hpwl_mm"] == pytest.approx(expected)
    assert not audit["signoff"]
    assert not audit["all_constraints_verified"]
    assert all(c["status"] == "not_checked" for c in audit["external_checks"])
    assert "copper_clearance" in {c["code"] for c in audit["external_checks"]}


def test_scipy_plan_fixed_references_geometry_and_roundtrip(board, constraints, tmp_path):
    original = deepcopy(board)
    result = plan_placements(board, constraints, count=2)
    assert result["status"] == "ok"
    assert len(result["candidates"]) == 2
    assert board == original
    best = result["candidates"][0]
    assert best["feasible"]
    assert best["optimizer"]["method"] == "scipy.optimize.minimize/SLSQP"
    assert best["optimizer"]["evaluations"] > 0
    assert best["metrics"]["hpwl_improvement_mm"] > 0
    assert best["metrics"]["optimization_seconds"] > 0
    by_ref = {p["reference"]: p for p in best["placements"]}
    for fp in original["footprints"]:
        assert by_ref[fp["reference"]]["rotation"] == fp["rotation"]
        if fp["reference"] in {"J1", "J2"}:
            assert by_ref[fp["reference"]]["x"] == fp["x"]
            assert by_ref[fp["reference"]]["y"] == fp["y"]
    placed = write_placements(EXAMPLE / "two-layer.kicad_pcb", tmp_path / "candidate.kicad_pcb", best["placements"])
    checked = audit_constraints(placed, constraints)
    assert checked["passed"], checked
    assert checked["metrics"]["hpwl_mm"] == pytest.approx(best["metrics"]["hpwl_mm"])
    assert checked["metrics"]["minimum_axis_gap_mm"] >= 0.5 - 1e-6


def test_deterministic_seed_produces_same_geometry(board, constraints):
    first = plan_placements(board, constraints, count=1)
    second = plan_placements(board, constraints, count=1)
    assert first["candidates"][0]["placements"] == second["candidates"][0]["placements"]
    assert first["candidates"][0]["metrics"]["hpwl_mm"] == second["candidates"][0]["metrics"]["hpwl_mm"]


def test_locked_without_fixed_list_stays_put(board, constraints):
    constraints["fixed_references"] = []
    result = plan_placements(board, constraints, count=1)
    j1 = next(p for p in result["candidates"][0]["placements"] if p["reference"] == "J1")
    assert (j1["x"], j1["y"]) == (25, 35)


def test_all_fixed_infeasibility_not_optimizer_success(board, constraints):
    constraints["fixed_references"] = [fp["reference"] for fp in board["footprints"]]
    result = plan_placements(board, constraints)
    assert result["status"] == "infeasible"
    assert not result["candidates"][0]["feasible"]
    assert result["candidates"][0]["optimizer"]["evaluations"] == 0


@pytest.mark.parametrize("rule", ["edge", "gap", "region"])
def test_envelope_constraints_audited(board, constraints, rule):
    constraints["proximity"] = []
    if rule == "edge":
        board["footprints"][2]["bounds"] = [19, 25, 23, 27]
        code = "edge_clearance"
    elif rule == "gap":
        board["footprints"][2]["bounds"] = board["footprints"][3]["bounds"][:]
        code = "component_gap"
    else:
        constraints["regions"][0]["bounds"] = [41, 27, 57, 48]
        code = "region"
    audit = audit_constraints(board, constraints)
    assert any(v["code"] == code for v in audit["violations"])
    assert not audit["passed"]


def test_impossible_region_is_explicit(board, constraints):
    constraints["regions"][0]["bounds"] = [40, 30, 40.1, 30.1]
    result = plan_placements(board, constraints)
    assert result["status"] == "infeasible"
    assert not result["candidates"]


def test_fabrication_and_net_width_audit(board, constraints):
    constraints["proximity"] = []
    board["tracks"] = 2
    board["track_items"] = [{"net": "VIN", "width_mm": 0.3}, {"net": "GND", "width_mm": 0.2}]
    board["vias"] = 1
    board["via_items"] = [{"net": "VIN", "drill_mm": 0.2}]
    audit = audit_constraints(board, constraints)
    widths = [v for v in audit["violations"] if v["code"] == "track_width"]
    assert [v["required_mm"] for v in widths] == [0.4, 0.25]
    assert any(v["code"] == "via_drill" for v in audit["violations"])
    board["track_items"][0]["width_mm"] = 0.4
    board["track_items"][1]["width_mm"] = 0.25
    board["via_items"][0]["drill_mm"] = 0.3
    assert audit_constraints(board, constraints)["passed"]
    assert plan_placements(board, constraints)["status"] == "blocked"


def test_missing_track_evidence_is_unknown(board, constraints):
    constraints["proximity"] = []
    board["tracks"] = 1
    audit = audit_constraints(board, constraints)
    assert audit["status"] == "unknown"
    assert not audit["passed"]
    assert any("track width" in u for u in audit["unknowns"])


def test_unknown_geometry_and_constraint_fields_block(board, constraints):
    board["unsupported"] = ["curved outline"]
    constraints["unimplemented_rule"] = True
    audit = audit_constraints(board, constraints)
    assert not audit["feasible"]
    assert len(audit["unknowns"]) == 2
    assert plan_placements(board, constraints)["status"] == "blocked"


def test_missing_references_and_nets_are_not_ignored(board, constraints):
    constraints["fixed_references"].append("U999")
    constraints["critical_nets"].append("NONEXISTENT")
    audit = audit_constraints(board, constraints)
    assert {"missing_reference", "missing_net"} <= {v["code"] for v in audit["violations"]}


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "0.2", True])
def test_invalid_numeric_constraint_rejected(board, constraints, value):
    constraints["board"]["component_gap_mm"] = value
    with pytest.raises(ValueError):
        audit_constraints(board, constraints)


def test_invalid_schema_and_counts(board, constraints):
    for count in (0, 21, 1.5, True):
        with pytest.raises(ValueError):
            plan_placements(board, constraints, count)
    constraints["schema_version"] = 2
    with pytest.raises(ValueError, match="schema_version"):
        audit_constraints(board, constraints)


def test_zero_area_geometry_is_unknown(board, constraints):
    board["footprints"][2]["bounds"] = [40, 28, 40, 28]
    audit = audit_constraints(board, constraints)
    assert not audit["passed"]
    assert any("zero area" in item for item in audit["unknowns"])


def test_fixed_drift_detected_with_baseline(board, constraints):
    board["_baseline_placements"] = {"J1": {"x": 24, "y": 35, "rotation": 0}}
    assert any(v["code"] == "fixed_placement" for v in audit_constraints(board, constraints)["violations"])


def test_copper_layer_mismatch_rejected(board, constraints):
    board["copper_layers"].insert(1, "In1.Cu")
    assert any(v["code"] == "copper_layers" for v in audit_constraints(board, constraints)["violations"])
