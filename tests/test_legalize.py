from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import time

import numpy as np
import pytest

from pcb_weaver.board import compare_boards
from pcb_weaver.models import Constraints
from pcb_weaver.planning import _translated, audit_constraints, plan_placements
from pcb_weaver.placement_large import _Problem
from pcb_weaver.service import EngineeringService
from test_placement_large import make_board
from test_two_sided_geometry import RULES, pair_board


ROOT = Path(__file__).resolve().parents[1]


def legal_rules(**kwargs):
    return {**deepcopy(RULES), "placement": {"algorithm": "legalize"}, **kwargs}


def test_legalize_is_validated_in_model():
    assert Constraints.model_validate(legal_rules()).placement.algorithm == "legalize"


def test_legalize_retains_all_poses_when_already_feasible(tmp_path):
    _, board, rules = make_board(tmp_path, count=60)
    rules["placement"]["algorithm"] = "legalize"
    plan = plan_placements(board, rules, 2)
    for candidate in plan["candidates"]:
        assert candidate["feasible"]
        assert candidate["metrics"]["total_displacement_mm"] == 0
        assert candidate["metrics"]["maximum_displacement_mm"] == 0
        assert candidate["metrics"]["moved_components"] == 0
        assert candidate["optimizer"]["subproblems"] == 0
        assert candidate["optimizer"]["objective"] == "sum_squared_displacement_mm2"


@pytest.mark.parametrize("kind", ["edge", "region", "proximity"])
def test_legalize_repairs_noncollision_constraints(tmp_path, kind):
    board, _ = pair_board(tmp_path)
    rules = legal_rules()
    if kind == "edge":
        board = _translated(board, ["B"], np.array([21, 35]))
    elif kind == "region":
        rules["regions"] = [{"references": ["B"], "bounds": [45, 30, 65, 45]}]
    else:
        board = _translated(board, ["B"], np.array([60, 35]))
        rules["fixed_references"] = ["A"]
        rules["proximity"] = [{"reference": "A", "target": "B", "max_distance_mm": 5}]
    assert not audit_constraints(board, rules)["passed"]
    candidate = plan_placements(board, rules, 1)["candidates"][0]
    assert candidate["feasible"], candidate["violations"]
    refs = [p["reference"] for p in candidate["placements"]]
    positions = np.array([[p["x"], p["y"]] for p in candidate["placements"]]).ravel()
    assert audit_constraints(_translated(board, refs, positions), rules)["passed"]


def test_legalize_preserves_locked_and_fixed_conflict_failure(tmp_path):
    board, _ = pair_board(tmp_path, second_side="F")
    board["footprints"][0]["locked"] = True
    candidate = plan_placements(board, legal_rules(fixed_references=["B"]), 1)["candidates"][0]
    assert not candidate["feasible"]
    assert candidate["metrics"]["total_displacement_mm"] == 0


def test_legalize_uses_bounded_blocks_without_constructive_fallback(tmp_path, monkeypatch):
    _, board, rules = make_board(tmp_path, count=12, stacked=True)
    rules["placement"] = {"algorithm": "legalize", "passes": 1, "max_iterations": 2, "block_size": 3}

    def forbidden(*args):
        raise AssertionError("Minimum-displacement repair must not silently repack the board")

    monkeypatch.setattr(_Problem, "legalize", forbidden)
    candidate = plan_placements(board, rules, 1)["candidates"][0]
    assert candidate["optimizer"]["max_subproblem_variables"] <= 6
    assert not candidate["feasible"]
    assert candidate["violations"]


def test_legalize_repeated_runs_are_deterministic(tmp_path):
    board, _ = pair_board(tmp_path, second_side="F")
    first = plan_placements(board, legal_rules(), 3)
    second = plan_placements(board, legal_rules(), 3)
    assert [c["placements"] for c in first["candidates"]] == [c["placements"] for c in second["candidates"]]
    assert all(c["feasible"] for c in first["candidates"])


def test_legalize_two_sided_through_pad_occupancy(tmp_path):
    board, _ = pair_board(tmp_path, through=True)
    candidate = plan_placements(board, legal_rules(fixed_references=["A"]), 1)["candidates"][0]
    assert candidate["feasible"]
    assert candidate["placements"][0]["x"] == 40
    assert candidate["placements"][0]["y"] == 35


@pytest.mark.skipif(not (ROOT / "examples/system-controller").exists(), reason="Real benchmark not installed")
def test_real_160_legalize_through_recorded_service_plan_apply(tmp_path):
    source = ROOT / "examples/system-controller/kit-dev-coldfire-xilinx_5213.kicad_pcb"
    profile = ROOT / "profiles/system-controller.json"
    rules = json.loads(profile.read_text())
    assert rules["placement"]["algorithm"] == "legalize"
    before_hash = sha256(source.read_bytes()).hexdigest()
    service = EngineeringService(tmp_path / "managed")
    imported = service.import_project("real-legalize", str(source), rules)["revision"]
    revision = imported["id"]
    original = service.inspect_revision("real-legalize", revision)["board"]
    started = time.perf_counter()
    plan = service.plan_layout("real-legalize", revision, 3)
    elapsed = time.perf_counter() - started
    assert plan["algorithm"] == "legalize"
    assert len(plan["candidates"]) == 3
    for candidate in plan["candidates"]:
        assert candidate["feasible"], candidate["violations"]
        assert candidate["metrics"]["total_displacement_mm"] < 5
        assert candidate["metrics"]["maximum_displacement_mm"] < 0.5
        assert candidate["metrics"]["weighted_hpwl_mm"] < 7950
        assert candidate["optimizer"]["max_subproblem_variables"] <= 16
    best = plan["candidates"][0]
    applied = service.apply_layout("real-legalize", revision, plan["plan_id"], best["id"])
    assert applied["audit"]["passed"]
    child = applied["revision"]
    assert child["parent"] == revision and child["operation"] == "placement"
    assert child["plan_id"] == plan["plan_id"]
    inspected = service.inspect_revision("real-legalize", child["id"])
    assert inspected["constraints"]["placement"]["algorithm"] == "legalize"
    delta = compare_boards(original, inspected["board"])
    assert not delta["connectivity_changed"]
    assert not ({m["reference"] for m in delta["moved"]} & set(rules["fixed_references"]))
    assert source.read_bytes() and sha256(source.read_bytes()).hexdigest() == before_hash
    assert service.inspect_revision("real-legalize", revision)["board"] == original
    measurement = {"source_sha256": before_hash, "managed_root": str(service.store.root),
                   "parent_revision": revision, "applied_revision": child["id"], "plan_id": plan["plan_id"],
                   "total_plan_seconds": elapsed, "candidates": [
                       {"id": c["id"], "hpwl_mm": c["metrics"]["weighted_hpwl_mm"],
                        "total_displacement_mm": c["metrics"]["total_displacement_mm"],
                        "maximum_displacement_mm": c["metrics"]["maximum_displacement_mm"],
                        "moved_components": c["metrics"]["moved_components"], "optimizer": c["optimizer"]}
                       for c in plan["candidates"]]}
    (tmp_path / "measurement.json").write_text(json.dumps(measurement, indent=2))
    print("RECORDED_LEGALIZE_MEASUREMENT " + json.dumps(measurement))
