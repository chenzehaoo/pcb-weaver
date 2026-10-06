from copy import deepcopy
import json
import math
from pathlib import Path
import time

import numpy as np
import pytest
import sexpdata

from pcb_weaver.board import _child, _children, _tag, read_board, write_placements, compare_boards
from pcb_weaver.models import Constraints, PlacementOptions
from pcb_weaver.planning import _measure, _translated, audit_constraints, plan_placements
from pcb_weaver.placement_large import _Problem


FIXTURES = Path(__file__).parent / "fixtures" / "kicad"
PACKAGES = ["R_0603_1608Metric", "SOIC-8_3.9x4.9mm_P1.27mm", "SOT-23",
            "PinHeader_1x04_P2.54mm_Vertical", "LQFP-48_7x7mm_P0.5mm", "CP_Radial_D5.0mm_P2.00mm"]


def make_board(tmp_path, count=60, layers=4, stacked=False):
    templates = [sexpdata.loads((FIXTURES / (name + ".kicad_mod")).read_text()) for name in PACKAGES]
    rows = math.ceil(count / 10)
    tree = sexpdata.loads(f'''(kicad_pcb (version 20221018) (generator "pcb_weaver_test")
        (general (thickness 1.6)) (paper "A4") (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
        (setup (pad_to_mask_clearance 0)) (net 0 "")
        (gr_rect (start 10 10) (end 230 {30 + rows * 20})
          (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts")))''')
    layer_table = _child(tree, "layers")
    for index in range(1, layers - 1):
        layer_table.insert(index + 1, [index, f"In{index}.Cu", sexpdata.Symbol("signal")])
    for number, name in [(35, "F.Paste"), (37, "F.SilkS"), (39, "F.Mask"), (44, "Edge.Cuts"), (47, "F.CrtYd"), (49, "F.Fab")]:
        layer_table.append([number, name, sexpdata.Symbol("user")])
    names = ["GND"] + [f"PAIR_{index}" for index in range(math.ceil(count / 2))]
    for code, name in enumerate(names, 1):
        tree.append([sexpdata.Symbol("net"), code, name])
    rng = np.random.default_rng(55)
    fixed = []
    for index in range(count):
        fp = deepcopy(templates[index % len(templates)])
        fp[0] = sexpdata.Symbol("footprint")
        fp[1] = "KiCadFixture:" + PACKAGES[index % len(templates)]
        ref = f"P{index:03d}"
        reference = next(p for p in _children(fp, "fp_text") if str(p[1]) == "reference")
        reference[2] = ref
        x, y = (100, 80) if stacked else (30 + (index % 10) * 20 + rng.uniform(-1, 1),
                                         30 + (index // 10) * 20 + rng.uniform(-1, 1))
        fp.append([sexpdata.Symbol("at"), float(x), float(y)])
        if index in {0, 9} and not stacked:
            fixed.append(ref)
            if index == 0:
                fp.append([sexpdata.Symbol("locked"), sexpdata.Symbol("yes")])
        for pad in _children(fp, "pad"):
            if not str(pad[1]):
                continue
            code = 1 if str(pad[1]) == "2" else 2 + index // 2
            pad.append([sexpdata.Symbol("net"), code, names[code - 1]])
        tree.append(fp)
    source = tmp_path / f"real-packages-{count}-{layers}.kicad_pcb"
    source.write_text(sexpdata.dumps(tree), encoding="utf-8")
    board = read_board(source)
    rules = {"schema_version": 1, "board": {"layers": layers, "component_gap_mm": 0.5, "edge_clearance_mm": 1},
             "fixed_references": fixed, "critical_nets": ["PAIR_0"],
             "regions": [], "proximity": [], "placement": {"algorithm": "auto"}}
    if not stacked:
        for row in range(rows):
            rules["regions"].append({"references": [f"P{i:03d}" for i in range(row * 10, min(count, (row + 1) * 10))],
                                     "bounds": [15, 15 + row * 20, 225, 45 + row * 20]})
        rules["proximity"] = [{"reference": "P002", "target": "P003", "max_distance_mm": 25}]
    return source, board, Constraints.model_validate(rules).model_dump(mode="json")


@pytest.mark.parametrize("layers", [2, 4, 6, 8])
def test_real_packages_and_declared_copper_layers(tmp_path, layers):
    source, board, constraints = make_board(tmp_path, count=6, layers=layers)
    assert not board["unsupported"], board["unsupported"]
    assert len(board["copper_layers"]) == layers
    assert {fp["footprint"].split(":")[1] for fp in board["footprints"]} == set(PACKAGES)
    assert {pad["shape"] for fp in board["footprints"] for pad in fp["pads"]} >= {"roundrect", "rect", "circle", "oval"}
    assert all(fp["courtyard"]["supported"] for fp in board["footprints"])
    assert audit_constraints(board, constraints)["passed"]


@pytest.mark.parametrize("count", [60, 100])
def test_real_package_scaling_and_independent_roundtrip_audit(tmp_path, count):
    source, board, constraints = make_board(tmp_path, count=count, layers=4)
    original = deepcopy(board)
    assert audit_constraints(board, constraints)["passed"]
    started = time.perf_counter()
    planned = plan_placements(board, constraints, 1)
    elapsed = time.perf_counter() - started
    best = planned["candidates"][0]
    assert planned["algorithm"] == "block_coordinate"
    assert best["feasible"], best["violations"]
    assert best["optimizer"]["subproblems"] > 0
    assert best["optimizer"]["max_subproblem_variables"] <= 16
    assert best["optimizer"]["analytic_jacobians"]
    assert best["metrics"]["hpwl_improvement_mm"] > 0
    destination = tmp_path / "placed.kicad_pcb"
    written = write_placements(source, destination, best["placements"])
    audited = audit_constraints(written, constraints)
    assert audited["passed"], audited
    assert not compare_boards(source, destination)["connectivity_changed"]
    for fp, before in zip(written["footprints"], original["footprints"]):
        if before["locked"] or before["reference"] in constraints["fixed_references"]:
            assert (fp["x"], fp["y"], fp["rotation"]) == (before["x"], before["y"], before["rotation"])
    assert board == original
    measurement = {"components": count, "pads": sum(len(fp["pads"]) for fp in board["footprints"]),
                   "seconds": elapsed, "hpwl_before_mm": planned["baseline"]["metrics"]["hpwl_mm"],
                   "hpwl_after_mm": best["metrics"]["hpwl_mm"], "feasible": best["feasible"],
                   "optimizer": best["optimizer"]}
    print("PLACEMENT_MEASUREMENT " + json.dumps(measurement, sort_keys=True))
    (tmp_path / "measurement.json").write_text(json.dumps(measurement, indent=2))


def test_large_solver_is_deterministic(tmp_path):
    _, board, constraints = make_board(tmp_path, 40)
    constraints["placement"].update(passes=1, max_iterations=30)
    a = plan_placements(board, constraints, 1)
    b = plan_placements(board, constraints, 1)
    assert a["candidates"][0]["placements"] == b["candidates"][0]["placements"]


def test_stacked_footprints_are_legalized_before_local_optimization(tmp_path):
    _, board, constraints = make_board(tmp_path, 60, stacked=True)
    constraints["placement"].update(passes=1, max_iterations=30)
    assert not audit_constraints(board, constraints)["passed"]
    result = plan_placements(board, constraints, 1)
    assert result["candidates"][0]["feasible"], result["candidates"][0]["violations"]
    assert result["candidates"][0]["optimizer"]["legalization_unresolved"] == 0


def test_dense_global_slsqp_is_explicitly_guarded(tmp_path):
    _, board, constraints = make_board(tmp_path, 60)
    constraints["placement"]["algorithm"] = "slsqp"
    result = plan_placements(board, constraints)
    assert result["status"] == "blocked"
    assert "32 movable" in result["reason"]


def test_analytic_pad_hpwl_gradient_matches_independent_differences(tmp_path):
    _, board, constraints = make_board(tmp_path, 6)
    refs = [fp["reference"] for fp in board["footprints"]]
    bounds = [(0, 250)] * (2 * len(refs))
    problem = _Problem(board, constraints, refs, bounds)
    value, gradient = problem.hpwl(problem.positions, gradient=True)
    assert value == pytest.approx(_measure(board, constraints)["weighted_hpwl_mm"])
    direction = np.random.default_rng(9).normal(size=problem.positions.shape)
    h = 1e-5
    derivative = (problem.hpwl(problem.positions + h * direction) - problem.hpwl(problem.positions - h * direction)) / (2 * h)
    assert float((gradient * direction).sum()) == pytest.approx(derivative, abs=1e-6)


def test_independent_audit_rejects_fixed_drift_and_keeps_original_baseline(tmp_path):
    _, board, constraints = make_board(tmp_path, 6)
    ref = board["footprints"][0]["reference"]
    before = board["footprints"][0]
    first = _translated(board, [ref], np.array([before["x"] + 1, before["y"]]))
    second = _translated(first, [], np.array([]))
    assert any(v["code"] == "fixed_placement" for v in audit_constraints(second, constraints)["violations"])
