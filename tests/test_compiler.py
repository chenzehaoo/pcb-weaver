import json
import hashlib
from pathlib import Path

import pytest

from pcb_weaver.compiler import compile_rules
from pcb_weaver.models import Constraints


def test_existing_minima_and_assignment_preserved(tmp_path):
    board = tmp_path / "test.kicad_pcb"
    project = {"board": {"design_settings": {"rules": {"min_track_width": 1.0, "min_clearance": 0.6}}},
               "net_settings": {"classes": [{"name": "Default", "track_width": 0.8, "via_drill": 0.5}],
                                "netclass_assignments": {"VCC": "Default"}}}
    board.with_suffix(".kicad_pro").write_text(json.dumps(project))
    result = compile_rules(board, Constraints().model_dump())
    actual = json.loads(board.with_suffix(".kicad_pro").read_text())
    assert actual["board"]["design_settings"]["rules"]["min_track_width"] == 1.0
    assert actual["net_settings"]["netclass_assignments"] == {"VCC": "Default"}
    assert result["status"] == "ok"


def _constraints():
    return {"fabrication": {"min_track_mm": 0.25, "min_clearance_mm": 0.2, "min_via_drill_mm": 0.3},
            "net_rules": [{"nets": ["POWER"], "min_width_mm": 0.6}, {"nets": ["POWER"], "min_width_mm": 0.4}]}


def _via_project(tmp_path, rules=None):
    board = tmp_path / "vias.kicad_pcb"
    board.write_text("Explicit simulated board fixture")
    project = {"board": {"design_settings": {"rules": rules if rules is not None else {
        "min_via_annular_width": 0.05, "min_via_diameter": 0.5, "min_through_hole_diameter": 0.4}}},
        "net_settings": {"meta": {"version": 4}, "classes": [
            {"name": "POWER", "track_width": 0.4, "via_diameter": 0.8, "via_drill": 0.4},
            {"name": "Default", "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.4}]}}
    path = board.with_suffix(".kicad_pro")
    path.write_text(json.dumps(project))
    constraints = _constraints()
    constraints["net_rules"] = []
    return board, path, project, constraints


@pytest.mark.parametrize("rules,drill,diameter,annular", [
    ({"min_via_annular_width": 0.05, "min_via_diameter": 0.5}, 0.4, 0.6, 0.05),
    ({"min_via_annular_width": 0.1}, 0.4, 0.6, 0.1),
    ({"min_via_annular_width": 0.15}, 0.4, 0.7, 0.15),
    ({"min_via_annular_width": 0.05, "min_via_diameter": 0.75}, 0.4, 0.75, 0.05),
    ({"min_via_annular_width": 0.05, "min_through_hole_diameter": 0.6}, 0.6, 0.7, 0.05),
    ({}, 0.4, 0.8, 0.2),
])
def test_declared_via_geometry_preserves_preferences_and_real_floors(tmp_path, rules, drill, diameter, annular):
    board, path, original, constraints = _via_project(tmp_path, rules)
    result = compile_rules(board, constraints)
    actual = json.loads(path.read_text())
    classes = {c["name"]: c for c in actual["net_settings"]["classes"]}
    assert classes["Default"]["via_diameter"] == diameter
    assert classes["Default"]["via_drill"] == drill
    assert classes["POWER"]["via_diameter"] == 0.8
    assert result["via_policy"]["minimum_annular_width_mm"] == annular
    assert result["via_policy"]["annular_source"] == (
        "project.min_via_annular_width" if "min_via_annular_width" in rules else "conservative_missing_rule_fallback")
    for key, value in rules.items():
        assert actual["board"]["design_settings"]["rules"][key] >= value
    # Recompiling must not repeatedly inflate exact boundary values.
    snapshot = path.read_bytes()
    compile_rules(board, constraints)
    assert path.read_bytes() == snapshot


def test_higher_fabrication_drill_recomputes_annular_floor_without_extra_margin(tmp_path):
    board, path, _, constraints = _via_project(tmp_path)
    constraints["fabrication"]["min_via_drill_mm"] = 0.5
    compile_rules(board, constraints)
    default = next(c for c in json.loads(path.read_text())["net_settings"]["classes"] if c["name"] == "Default")
    assert (default["via_diameter"], default["via_drill"]) == (0.6, 0.5)


def test_via_inheritance_uses_effective_default_drill_and_preserves_unset_fields(tmp_path):
    board, path, project, constraints = _via_project(tmp_path)
    project["net_settings"]["classes"][:0] = [
        {"name": "Inherited", "via_drill": None, "via_diameter": None},
        {"name": "ExplicitDiameter", "via_diameter": 0.45},
        {"name": "LargerDrill", "via_drill": 0.6, "via_diameter": None}]
    path.write_text(json.dumps(project))
    compile_rules(board, constraints)
    classes = {c["name"]: c for c in json.loads(path.read_text())["net_settings"]["classes"]}
    assert classes["Inherited"]["via_drill"] is None and classes["Inherited"]["via_diameter"] is None
    assert classes["ExplicitDiameter"]["via_diameter"] == 0.5
    assert "via_drill" not in classes["ExplicitDiameter"]
    assert classes["LargerDrill"]["via_diameter"] == 0.7


@pytest.mark.parametrize("native_diameter", [0.6, 0.8])
def test_native_derived_class_uses_same_annular_policy(tmp_path, native_diameter):
    board, path, _, constraints = _via_project(tmp_path)
    constraints["net_rules"] = [{"nets": ["POWER"], "min_width_mm": 0.6}]
    state = {"status": "ok", "version": "9.0.9", "board_sha256": hashlib.sha256(board.read_bytes()).hexdigest(),
        "project_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "nets": {"POWER": {
            "class_name": "POWER", "class_names": ["POWER"], "track_width": 0.4,
            "clearance": 0.2, "via_drill": 0.4, "via_diameter": native_diameter}}}
    result = compile_rules(board, constraints, native_state=state)
    actual = json.loads(path.read_text())
    generated = next(c for c in actual["net_settings"]["classes"] if c["name"] == result["net_classes"]["POWER"])
    assert (generated["via_diameter"], generated["via_drill"]) == (native_diameter, 0.4)
    assert actual["pcb_weaver_net_rules"]["POWER"]["minimums"]["via_diameter"] == native_diameter


@pytest.mark.parametrize("setting", ["min_via_annular_width", "min_via_diameter", "min_through_hole_diameter"])
@pytest.mark.parametrize("value", [None, True, "0.05", -0.1, float("nan"), float("inf"), float("-inf")])
def test_invalid_declared_via_floor_rejected_without_write(tmp_path, setting, value):
    board, path, project, constraints = _via_project(tmp_path)
    project["board"]["design_settings"]["rules"][setting] = value
    path.write_text(json.dumps(project))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="finite nonnegative"):
        compile_rules(board, constraints)
    assert path.read_bytes() == original


@pytest.mark.parametrize("setting", ["via_drill", "via_diameter"])
@pytest.mark.parametrize("value", [True, "0.4", -0.1, float("nan"), float("inf")])
def test_invalid_class_via_geometry_rejected_without_write(tmp_path, setting, value):
    board, path, project, constraints = _via_project(tmp_path)
    project["net_settings"]["classes"][0][setting] = value
    path.write_text(json.dumps(project))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="finite nonnegative"):
        compile_rules(board, constraints)
    assert path.read_bytes() == original


def test_frozen_system_project_copy_keeps_original_default_and_power_vias(tmp_path):
    source = Path(__file__).resolve().parents[1] / "examples" / "system-controller"
    original = source / "kit-dev-coldfire-xilinx_5213.kicad_pro"
    original_bytes = original.read_bytes()
    board = tmp_path / "system-copy.kicad_pcb"
    path = board.with_suffix(".kicad_pro")
    path.write_bytes(original_bytes)
    constraints = json.loads((source / "constraints.json").read_text())
    result = compile_rules(board, constraints)
    project = json.loads(path.read_text())
    classes = {c["name"]: c for c in project["net_settings"]["classes"]}
    assert (classes["Default"]["via_diameter"], classes["Default"]["via_drill"]) == (0.6, 0.4)
    assert (classes["POWER"]["via_diameter"], classes["POWER"]["via_drill"]) == (0.8, 0.4)
    assert result["via_policy"]["minimum_annular_width_mm"] == 0.05
    assert result["via_policy"]["minimum_via_diameter_mm"] == 0.5
    assert original.read_bytes() == original_bytes


def test_per_net_width_does_not_widen_other_nets(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    result = compile_rules(board, _constraints())
    settings = json.loads(board.with_suffix(".kicad_pro").read_text())["net_settings"]
    classes = {c["name"]: c for c in settings["classes"]}
    assert classes["Default"]["track_width"] == 0.25
    assert classes[settings["netclass_assignments"]["POWER"]]["track_width"] == 0.6
    assert result["per_net_widths_mm"] == {"POWER": 0.6}
    assert not result["high_speed_supported"]


def test_patterns_require_native_evidence_and_do_not_write_on_failure(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    path = board.with_suffix(".kicad_pro")
    path.write_text(json.dumps({"net_settings": {"netclass_patterns": [{"pattern": "*", "netclass": "Default"}]}}))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="inspect_board"):
        compile_rules(board, _constraints())
    assert path.read_bytes() == original


def test_recompilation_preserves_stronger_explicit_neckdown_floor(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("board fixture")
    compile_rules(board, _constraints())
    path = board.with_suffix(".kicad_pro")
    project = json.loads(path.read_text())
    name = project["pcb_weaver_net_rules"]["POWER"]["class"]
    state = {"status": "ok", "board_sha256": hashlib.sha256(board.read_bytes()).hexdigest(),
             "project_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
             "nets": {"POWER": {"class_name": name, "track_width": 0.6}}}
    constraints = _constraints()
    constraints["net_rules"] = [{"nets": ["POWER"], "min_width_mm": 0.4}]
    compile_rules(board, constraints, native_state=state)
    assert json.loads(path.read_text())["pcb_weaver_net_rules"]["POWER"]["declared_min_width_mm"] == 0.6


def test_native_effective_rules_and_schema4_assignments_survive(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("board fixture")
    path = board.with_suffix(".kicad_pro")
    patterns = [{"pattern": "*", "netclass": "PowerBase"}]
    project = {"net_settings": {"meta": {"version": 4}, "classes": [
        {"name": "Default", "track_width": 0.25}, {"name": "PowerBase", "priority": 0, "track_width": 0.8,
        "diff_pair_gap": 0.5, "pcb_color": "rgba(1,2,3,1)"}], "netclass_patterns": patterns,
        "netclass_assignments": {"POWER": ["PowerBase"], "OTHER": ["PowerBase"]}}}
    path.write_text(json.dumps(project))
    state = {"status": "ok", "board_sha256": hashlib.sha256(board.read_bytes()).hexdigest(),
        "project_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "nets": {
        "POWER": {"class_name": "PowerBase", "track_width": 0.8, "clearance": 0.4, "via_drill": 0.5,
                  "via_diameter": 1.0, "diff_pair_gap": 0.5}}}
    result = compile_rules(board, _constraints(), native_state=state)
    actual = json.loads(path.read_text())["net_settings"]
    assert actual["netclass_patterns"] == patterns
    assert actual["netclass_assignments"]["OTHER"] == ["PowerBase"]
    assert actual["netclass_assignments"]["POWER"][0] == "PowerBase"
    derived = next(c for c in actual["classes"] if c["name"] == result["net_classes"]["POWER"])
    assert json.loads(path.read_text())["pcb_weaver_net_rules"]["POWER"]["declared_min_width_mm"] == 0.6
    assert (derived["track_width"], derived["clearance"], derived["diff_pair_gap"]) == (0.8, 0.4, 0.5)
    assert "pcb_color" not in derived
    assert next(c for c in actual["classes"] if c["name"] == "PowerBase")["pcb_color"] == "rgba(1,2,3,1)"
    with pytest.raises(ValueError, match="stale"):
        compile_rules(board, _constraints(), native_state=state)


def test_unset_composite_properties_remain_inherited(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    path = board.with_suffix(".kicad_pro")
    path.write_text(json.dumps({"net_settings": {"meta": {"version": 4}, "classes": [
        {"name": "Default", "track_width": 0.8}, {"name": "Inherited", "track_width": None, "via_drill": None}]}}))
    constraints = _constraints()
    constraints["net_rules"] = []
    compile_rules(board, constraints)
    inherited = json.loads(path.read_text())["net_settings"]["classes"][1]
    assert inherited["track_width"] is None and inherited["via_drill"] is None


@pytest.mark.parametrize("condition", ["A.hasExactNetclass('PowerBase')", "A.NetClass == B.NetClass",
                                        "A.NetClass == 'PowerBase,Default'"])
def test_class_set_sensitive_custom_rules_block_before_write(tmp_path, condition):
    board = tmp_path / "board.kicad_pcb"
    board.with_suffix(".kicad_dru").write_text('(version 1)\n(rule protected (condition "' + condition + '") (constraint clearance (min 0.4mm)))')
    with pytest.raises(ValueError, match="custom-rule conditions"):
        compile_rules(board, _constraints())
    assert not board.with_suffix(".kicad_pro").exists()
