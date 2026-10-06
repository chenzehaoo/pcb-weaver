"""Read-only real ColdFire benchmark; never repairs another owner's fixtures."""

from hashlib import sha256
import json
from pathlib import Path
import time

import pytest

from pcb_weaver.board import compare_boards, read_board, write_placements
from pcb_weaver.planning import audit_constraints, plan_placements


SOURCE = Path(__file__).resolve().parents[1] / "examples/system-controller/kit-dev-coldfire-xilinx_5213.kicad_pcb"


@pytest.mark.skipif(not SOURCE.exists(), reason="Real system-controller fixture is not installed")
def test_coldfire_scaling_and_independent_roundtrip(tmp_path):
    constraints_path = SOURCE.parent / "constraints.json"
    inputs = [SOURCE.read_bytes(), constraints_path.read_bytes()]
    snapshot = tmp_path / SOURCE.name
    snapshot.write_bytes(inputs[0])
    board = read_board(snapshot)
    constraints = json.loads(inputs[1])
    assert not board["unsupported"]
    assert len(board["footprints"]) == 160
    assert sum(len(fp["pads"]) for fp in board["footprints"]) == 825
    assert len(board["nets"]) == 278
    assert len(board["copper_layers"]) == 4
    assert sum(fp["layer"] == "B.Cu" for fp in board["footprints"]) == 14
    baseline = audit_constraints(board, constraints)
    fixed = set(constraints["fixed_references"]) | {fp["reference"] for fp in board["footprints"] if fp["locked"]}
    fixed_conflicts = [v for v in baseline["violations"]
                       if v["code"] == "component_gap" and set(v["reference"].split(",")) <= fixed]
    started = time.perf_counter()
    result = plan_placements(board, constraints, count=1)
    elapsed = time.perf_counter() - started
    candidate = result["candidates"][0]
    assert candidate["optimizer"]["max_subproblem_variables"] <= 2 * constraints.get("placement", {}).get("block_size", 8)
    output = tmp_path / "placed.kicad_pcb"
    placed = write_placements(snapshot, output, candidate["placements"])
    verified = audit_constraints(placed, constraints)
    assert candidate["feasible"] == verified["passed"]
    assert candidate["metrics"]["weighted_hpwl_mm"] == pytest.approx(verified["metrics"]["weighted_hpwl_mm"])
    delta = compare_boards(board, placed)
    assert not delta["connectivity_changed"]
    assert not ({m["reference"] for m in delta["moved"]} & fixed)
    assert {fp["reference"]: fp["layer"] for fp in board["footprints"]} == {
        fp["reference"]: fp["layer"] for fp in placed["footprints"]}
    if fixed_conflicts:
        assert not candidate["feasible"]
        assert all(v in verified["violations"] for v in fixed_conflicts)
    assert inputs == [SOURCE.read_bytes(), constraints_path.read_bytes()], "Benchmark changed during test"
    measurement = {"input_sha256": [sha256(data).hexdigest() for data in inputs], "components": 160,
                   "pads": 825, "nets": 278, "layers": 4, "seconds": elapsed,
                   "feasible": candidate["feasible"], "baseline_hpwl_mm": baseline["metrics"]["weighted_hpwl_mm"],
                   "candidate_hpwl_mm": candidate["metrics"]["weighted_hpwl_mm"],
                   "baseline_violations": baseline["violations"], "candidate_violations": candidate["violations"],
                   "fixed_pair_conflicts": fixed_conflicts, "optimizer": candidate["optimizer"]}
    (tmp_path / "measurement.json").write_text(json.dumps(measurement, indent=2), encoding="utf-8")
    print("SYSTEM_PLACEMENT_MEASUREMENT " + json.dumps(measurement))
