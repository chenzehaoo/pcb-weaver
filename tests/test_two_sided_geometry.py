import numpy as np
import pytest
import sexpdata

from pcb_weaver.board import read_board, write_placements
from pcb_weaver.planning import _translated, audit_constraints, plan_placements
from pcb_weaver.placement_large import _Problem
from test_board import children, fixture_ast, save_ast


def pair_board(tmp_path, second_side="B", through=False, npth=False):
    ast = fixture_ast()
    for fp in children(ast, "footprint"):
        ast.remove(fp)
    for ref, side in [("A", "F"), ("B", second_side)]:
        pads = []
        for number, x in [(1, -2), (2, 2)]:
            hole = through and ref == "A"
            kind = "np_thru_hole" if hole and npth else "thru_hole" if hole else "smd"
            layers = '"*.Cu" "*.Mask"' if hole else f'"{side}.Cu" "{side}.Mask"'
            drill = '(drill 0.4)' if hole else ''
            pads.append(f'(pad "{number}" {kind} rect (at {x} 0) (size 0.8 0.8) {drill} (layers {layers}) (net 1 "GND"))')
        ast.append(sexpdata.loads(f'''(footprint "original:pair" (layer "{side}.Cu") (at 40 35)
            (property "Reference" "{ref}") (property "Value" "test")
            (fp_rect (start -3 -1) (end 3 1) (layer "{side}.CrtYd") (width 0.05))
            {' '.join(pads)})'''))
    path = save_ast(tmp_path, ast)
    return read_board(path), path


RULES = {"schema_version": 1, "board": {"layers": 2, "component_gap_mm": 0.25, "edge_clearance_mm": 0.5}}


def problem(board):
    return _Problem(board, RULES, ["A", "B"], [(24, 66), (22, 53)] * 2)


def test_opposite_smd_bodies_can_overlap_without_false_collision(tmp_path):
    board, _ = pair_board(tmp_path)
    assert not board["unsupported"]
    assert audit_constraints(board, RULES)["passed"]
    assert problem(board).quality(problem(board).positions)[0] == 0
    for method in ["slsqp", "block_coordinate"]:
        result = plan_placements(board, {**RULES, "placement": {"algorithm": method}}, count=1)
        assert result["candidates"][0]["feasible"]


def test_same_side_bodies_collide(tmp_path):
    board, _ = pair_board(tmp_path, second_side="F")
    audit = audit_constraints(board, RULES)
    assert any(v["code"] == "component_gap" for v in audit["violations"])
    assert problem(board).quality(problem(board).positions)[0] > 0


@pytest.mark.parametrize("npth", [False, True])
def test_through_pads_reserve_both_faces_without_filling_pin_gaps(tmp_path, npth):
    board, source = pair_board(tmp_path, through=True, npth=npth)
    assert not board["unsupported"]
    assert not audit_constraints(board, RULES)["passed"]
    assert problem(board).quality(problem(board).positions)[0] > 0
    # A small back-side body between the two pins is physically disjoint.
    back = board["footprints"][1]
    back["bounds"] = [39.5, 34.5, 40.5, 35.5]
    for pad in back["pads"]:
        pad["x"], pad["y"] = 40, 35
    assert audit_constraints(board, RULES)["passed"]
    assert problem(board).quality(problem(board).positions)[0] == 0
    moved = _translated(board, ["A"], np.array([42, 35]))
    assert not audit_constraints(moved, RULES)["passed"]
    assert moved["footprints"][0]["through_hole_bounds"][0][0] == pytest.approx(39.6)
    reread = write_placements(source, tmp_path / "translated.kicad_pcb", [{"reference": "A", "x": 42, "y": 35}])
    assert reread["footprints"][0]["through_hole_bounds"] == moved["footprints"][0]["through_hole_bounds"]


def test_mixed_face_local_optimizer_legalizes_through_pad_collision(tmp_path):
    board, _ = pair_board(tmp_path, through=True)
    rules = {**RULES, "fixed_references": ["A"], "placement": {"algorithm": "block_coordinate"}}
    result = plan_placements(board, rules, count=1)
    assert result["status"] == "ok"
    candidate = result["candidates"][0]
    assert candidate["feasible"]
    assert candidate["placements"][0]["x"] == 40
    assert candidate["placements"][0]["y"] == 35


def test_legacy_two_sided_board_requires_cross_face_occupancy_evidence(tmp_path):
    board, _ = pair_board(tmp_path)
    del board["footprints"][0]["through_hole_bounds"]
    assert any("occupancy unavailable" in s for s in audit_constraints(board, RULES)["unknowns"])
    assert plan_placements(board, RULES)["status"] == "blocked"


@pytest.mark.parametrize("size,drill,allowed", [
    ((4.8006, 3.5), (4.0, 1.016), True),
    ((4.8006, 5.0), (1.016, 4.0), True),
    ((4.8006, 3.5), (1.016, 4.0), False),
    ((4.8006, 5.0), (5.0, 1.016), False),
])
def test_slot_axes_checked_in_pad_frame_not_sorted(size, drill, allowed, tmp_path):
    ast = fixture_ast()
    fp = children(ast, "footprint")[0]
    children(fp, "at")[0][1:] = [25, 35, -90]
    pad = children(fp, "pad")[0]
    children(pad, "at")[0].append(270)
    children(pad, "size")[0][1:] = size
    children(pad, "drill")[0][1:] = [sexpdata.Symbol("oval"), *drill]
    board = read_board(save_ast(tmp_path, ast))
    assert (not any("drill envelope" in s for s in board["unsupported"])) == allowed
    assert board["footprints"][0]["pads"][0]["drill_size"] == list(drill)
