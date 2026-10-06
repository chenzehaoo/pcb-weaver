from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import zipfile

import pytest
import sexpdata

from pcb_weaver.board import read_board
from pcb_weaver.compiler import CRITICAL_CHECKS, compile_rules
from pcb_weaver.models import Constraints
from pcb_weaver.service import EngineeringService, design_files
from pcb_weaver.storage import digest, read_json, write_json


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "two-layer" / "two-layer.kicad_pcb"
PASS = {"status": "passed", "passed": True, "violations": [], "unknowns": []}


def schematic(path, *children):
    tree = [sexpdata.Symbol("kicad_sch")]
    for child in children:
        tree.append([sexpdata.Symbol("sheet"), [sexpdata.Symbol("property"), "Sheetfile", child]])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sexpdata.dumps(tree), encoding="utf-8")


class FakeTools:
    """Deterministic engine boundary; never evidence of an actual EDA pass."""

    def __init__(self, board):
        self.board = deepcopy(board)
        self.drc = {"status": "ok", "errors": 0, "unconnected": 0, "excluded": 0, "ignored_checks": []}
        self.erc = {"status": "ok", "errors": 0, "excluded": 0, "ignored_checks": []}
        self.exports = 0
        self.position_references = None

    def run_drc(self, source, report):
        write_json(report, self.drc)
        return deepcopy(self.drc)

    def run_erc(self, source, report):
        write_json(report, self.erc)
        return deepcopy(self.erc)

    def export_netlist(self, source, report):
        root = ET.Element("export")
        components = ET.SubElement(root, "components")
        nets = ET.SubElement(root, "nets")
        by_net = {}
        for fp in self.board["footprints"]:
            comp = ET.SubElement(components, "comp", ref=fp["reference"])
            for field in ("value", "footprint"):
                ET.SubElement(comp, field).text = fp[field]
            if fp.get("dnp"):
                ET.SubElement(comp, "property", name="dnp")
            for pad in fp["pads"]:
                if not pad["net"]:
                    continue
                if pad["net"] not in by_net:
                    by_net[pad["net"]] = ET.SubElement(nets, "net", name=pad["net"], code=str(len(by_net) + 100))
                ET.SubElement(by_net[pad["net"]], "node", ref=fp["reference"], pin=pad["number"])
        ET.ElementTree(root).write(report, encoding="utf-8")
        return {"status": "ok"}

    def export_manufacturing(self, source, output):
        self.exports += 1
        output.mkdir()
        (output / "test.gbr").write_text("fixture output, not manufacturing data")
        native_bom = output / "bom.csv"
        native_bom.write_text("native BOM fixture must remain unchanged\n")
        refs = self.position_references
        if refs is None:
            refs = [fp["reference"] for fp in self.board["footprints"] if not fp.get("dnp")]
        with (output / "positions.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"])
            for ref in refs:
                writer.writerow([ref, "fixture", "fixture", 0, 0, 0, "top"])
        return {"status": "ok", "artifacts": [{"kind": "bom", "path": str(native_bom), "sha256": digest(native_bom)}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    board = source / "demo.kicad_pcb"
    shutil.copy2(EXAMPLE, board)
    schematic(source / "demo.kicad_sch")
    service = EngineeringService(tmp_path / "managed")
    service.toolchain = FakeTools(read_board(board))
    monkeypatch.setattr("pcb_weaver.service.audit_constraints", lambda *args: deepcopy(PASS))
    return service, board


def imported(setup, constraints=None):
    service, board = setup
    data = service.import_project("demo", str(board), constraints)["revision"]
    return service, data


@pytest.mark.parametrize("stage", ["export_dsn", "autoroute", "import_ses"])
@pytest.mark.parametrize("timed_out", [False, True])
def test_route_failure_identifies_stage_and_preserves_revision(setup, monkeypatch, stage, timed_out):
    service, revision = imported(setup)
    original, folder = service._verified("demo", revision["id"])
    before = design_files(folder / "design")
    calls = []
    service.toolchain.route_timeout = 1800
    service.toolchain.timeout = 300

    def operation(name):
        def execute(*args):
            calls.append(name)
            if name == stage:
                return {"status": "failed", "reason": "Command timed out" if timed_out else "Native fixture error",
                        "timed_out": timed_out}
            return {"status": "ok"}
        return execute

    for method, name in [("export_dsn", "export_dsn"), ("route", "autoroute"), ("import_ses", "import_ses")]:
        monkeypatch.setattr(service.toolchain, method, operation(name), raising=False)
    result = service.route_revision("demo", revision["id"], 8)
    assert result["status"] == "blocked" and result["revision"] == revision["id"]
    assert result["failed_stage"] == stage and result["timed_out"] == timed_out
    assert stage in result["reason"]
    if timed_out:
        assert ("1800s" if stage == "autoroute" else "300s") in result["reason"]
    assert calls[-1] == stage
    assert read_json(Path(result["evidence_path"])) == result["details"]
    assert design_files(folder / "design") == before
    assert service._verified("demo", revision["id"])[0]["digest"] == original["digest"]


def assert_release_blocked(service, revision, reason):
    result = service.build_release("demo", revision)
    assert result["status"] == "blocked"
    assert any(reason in item for item in result["verification"]["reasons"])
    assert service.toolchain.exports == 0
    assert not list(service.store.root.rglob("*.zip"))
    return result["verification"]


@pytest.mark.parametrize("check", sorted(CRITICAL_CHECKS["DRC"]))
@pytest.mark.parametrize("severity", ["ignore", "warning"])
def test_disabled_critical_project_rule_blocks_without_rewriting(tmp_path, check, severity):
    board = tmp_path / "demo.kicad_pcb"
    project = board.with_suffix(".kicad_pro")
    write_json(project, {"board": {"design_settings": {"rule_severities": {check: severity}}}})
    before = project.read_bytes()
    with pytest.raises(ValueError, match=check):
        compile_rules(board, Constraints().model_dump(mode="json"))
    assert project.read_bytes() == before


def test_accepted_project_preserves_unrelated_rules_and_stronger_minima(tmp_path):
    board = tmp_path / "demo.kicad_pcb"
    project = board.with_suffix(".kicad_pro")
    settings = {"rule_severities": {"clearance": "error", "silk_over_copper": "ignore"},
                "rules": {"min_clearance": 0.8}, "custom": {"keep": True}}
    write_json(project, {"board": {"design_settings": settings}})
    compile_rules(board, Constraints().model_dump(mode="json"))
    actual = read_json(project)["board"]["design_settings"]
    assert actual["rules"]["min_clearance"] == 0.8
    assert actual["rule_severities"] == settings["rule_severities"]
    assert actual["custom"] == settings["custom"]


@pytest.mark.parametrize("settings", [
    {"board": {"design_settings": {"drc_exclusions": ["a finding"]}}},
    {"erc": {"erc_exclusions": ["a finding"]}},
    {"erc": {"rule_severities": {"power_pin_not_driven": "ignore"}}},
])
def test_project_exclusions_and_critical_erc_blocks_import(setup, settings):
    service, board = setup
    project = board.with_suffix(".kicad_pro")
    write_json(project, settings)
    before = project.read_bytes()
    with pytest.raises(ValueError, match="Project rules block"):
        service.import_project("demo", str(board))
    assert project.read_bytes() == before
    assert service.store.list_revisions("demo") == []


@pytest.mark.parametrize("kind,report,reason", [
    ("drc", {"ignored_checks": ["clearance"]}, "critical check clearance"),
    ("drc", {"ignored_checks": ["unconnected_items"]}, "critical check unconnected_items"),
    ("drc", {"excluded": 1}, "excluded findings"),
    ("drc", {"ignored_checks": [{"unknown": True}]}, "unsupported format"),
    ("erc", {"ignored_checks": ["power_pin_not_driven"]}, "critical check power_pin_not_driven"),
    ("erc", {"excluded": 1}, "excluded findings"),
])
def test_clean_counts_cannot_hide_disabled_checks_or_exclusions(setup, kind, report, reason):
    service, data = imported(setup)
    getattr(service.toolchain, kind).update(report)
    assert_release_blocked(service, data["id"], reason)


def test_preexisting_revision_with_disabled_rules_cannot_release(setup):
    service, data = imported(setup)
    revision, folder, old = service._new("demo", data["id"])
    project = (folder / "design" / old["board"]).with_suffix(".kicad_pro")
    rules = read_json(project)
    rules["board"]["design_settings"]["rule_severities"] = {"clearance": "ignore"}
    write_json(project, rules)
    # Model a revision sealed by the earlier implementation, not a hash bypass.
    legacy = service._seal("demo", revision, folder, old["board"], data["id"], "legacy-import")
    assert_release_blocked(service, legacy["id"], "critical check clearance")


@pytest.mark.parametrize("target", ["C:/external/child.kicad_sch", "C:child.kicad_sch",
                                    "//server/share/child.kicad_sch", "/external/child.kicad_sch",
                                    "../outside.kicad_sch", "${KIPRJMOD}/child.kicad_sch"])
def test_external_sheet_rejected_at_import(setup, target):
    service, board = setup
    schematic(board.with_suffix(".kicad_sch"), target)
    with pytest.raises(ValueError, match="Sheetfile"):
        service.import_project("demo", str(board))
    assert service.store.list_revisions("demo") == []


def test_absolute_sheet_rejected_even_if_inside_source(setup):
    service, board = setup
    child = board.parent / "child.kicad_sch"
    schematic(child)
    schematic(board.with_suffix(".kicad_sch"), str(child))
    with pytest.raises(ValueError, match="External"):
        service.import_project("demo", str(board))


def test_nested_relative_sheets_are_copied_and_bound_to_revision(setup):
    service, board = setup
    schematic(board.with_suffix(".kicad_sch"), "pages/child.kicad_sch")
    schematic(board.parent / "pages" / "child.kicad_sch", "deeper/grandchild.kicad_sch", "../shared.kicad_sch")
    schematic(board.parent / "pages" / "deeper" / "grandchild.kicad_sch", "../../shared.kicad_sch")
    schematic(board.parent / "shared.kicad_sch")
    service, data = imported(setup)
    folder = service.store.revision_dir("demo", data["id"]) / "design"
    assert design_files(folder) == data["files"]
    assert "pages/deeper/grandchild.kicad_sch" in data["files"]
    original = design_files(folder)
    schematic(board.parent / "shared.kicad_sch", "missing.kicad_sch")
    assert design_files(folder) == original
    nested = folder / "pages" / "deeper" / "grandchild.kicad_sch"
    nested.write_text(nested.read_text() + "\n")
    assert design_files(folder) != original
    with pytest.raises(ValueError, match="changed outside"):
        service.inspect_revision("demo", data["id"])


@pytest.mark.parametrize("target", ["../../outside.kicad_sch", "missing.kicad_sch", "../demo.kicad_sch"])
def test_nested_escape_missing_and_cycle_rejected(setup, target):
    service, board = setup
    schematic(board.with_suffix(".kicad_sch"), "pages/child.kicad_sch")
    schematic(board.parent / "pages" / "child.kicad_sch", target)
    with pytest.raises(ValueError, match="Sheetfile"):
        service.import_project("demo", str(board))


@pytest.mark.parametrize("field,new_value", [("value", "DIFFERENT_VALUE"), ("footprint", "Other:Footprint")])
@pytest.mark.parametrize("require_erc", [True, False])
def test_component_differences_block_release_even_in_board_only_mode(setup, field, new_value, require_erc):
    service, data = imported(setup, {"release": {"require_erc": require_erc}})
    service.toolchain.board["footprints"][0][field] = new_value
    verification = assert_release_blocked(service, data["id"], "values/footprints")
    differences = verification["connectivity"]["component_differences"]
    assert differences[0]["field"] == field
    assert differences[0]["schematic_value"] == new_value


BAD_AUDITS = [{"status": "unknown", "passed": False, "unknowns": ["unsupported"]},
              {"status": "unknown", "passed": True},
              {"status": "passed"}, {"passed": 1}, {"passed": "true"},
              {"passed": False}, {"passed": True, "unknowns": ["unsupported"]},
              {"passed": True, "violations": ["failed constraint"]}]


@pytest.mark.parametrize("audit", BAD_AUDITS)
def test_audit_must_explicitly_pass_to_release(setup, monkeypatch, audit):
    service, data = imported(setup)
    monkeypatch.setattr("pcb_weaver.service.audit_constraints", lambda *args: deepcopy(audit))
    assert_release_blocked(service, data["id"], "Physical constraints")


@pytest.mark.parametrize("audit", BAD_AUDITS)
def test_audit_must_explicitly_pass_to_apply_layout(setup, monkeypatch, audit):
    service, data = imported(setup)
    monkeypatch.setattr("pcb_weaver.service.plan_placements", lambda *args: {
        "candidates": [{"id": "c-1", "feasible": True, "placements": []}]})
    plan = service.plan_layout("demo", data["id"])
    monkeypatch.setattr("pcb_weaver.service.audit_constraints", lambda *args: deepcopy(audit))
    with pytest.raises(ValueError, match="constraint re-evaluation"):
        service.apply_layout("demo", data["id"], plan["plan_id"], "c-1")
    assert len(service.store.list_revisions("demo")) == 1


@pytest.mark.parametrize("audit_passed", [True, False])
def test_apply_audits_quantized_coordinates_before_sealing(setup, monkeypatch, audit_passed):
    service, data = imported(setup)
    monkeypatch.setattr("pcb_weaver.service.plan_placements", lambda *args: {
        "candidates": [{"id": "c-1", "feasible": True, "placements": [
            {"reference": "R1", "x": 39.04550508, "y": 28.12345678}]}]})
    plan = service.plan_layout("demo", data["id"])
    calls = []

    def audit(board, constraints):
        fp = next(item for item in board["footprints"] if item["reference"] == "R1")
        calls.append((fp["x"], fp["y"]))
        return {**deepcopy(PASS), "passed": audit_passed}

    monkeypatch.setattr("pcb_weaver.service.audit_constraints", audit)
    if audit_passed:
        result = service.apply_layout("demo", data["id"], plan["plan_id"], "c-1")
        assert result["status"] == "created"
        assert len(service.store.list_revisions("demo")) == 2
    else:
        with pytest.raises(ValueError, match="constraint re-evaluation"):
            service.apply_layout("demo", data["id"], plan["plan_id"], "c-1")
        assert len(service.store.list_revisions("demo")) == 1
    assert calls == [(39.045505, 28.123457)]


def test_positive_release_and_integrity_with_fake_engines(setup):
    service, data = imported(setup)
    service.toolchain.drc["ignored_checks"] = ["silk_over_copper"]
    result = service.build_release("demo", data["id"])
    assert result["status"] == "released"
    assert result["manifest"]["revision_digest"] == data["digest"]
    assert service.verify_release(result["archive"])["status"] == "verified"
    assert service.toolchain.exports == 1
    assert digest(Path(result["archive"])) == result["sha256"]
    with zipfile.ZipFile(result["archive"]) as archive:
        operations = json.loads(archive.read("export-operations.json"))
        native_bom = archive.read("manufacturing/bom.csv")
        assert hashlib.sha256(native_bom).hexdigest() == operations["artifacts"][0]["sha256"]
        assert native_bom.decode().startswith("native BOM fixture")
        assert "manufacturing/bom-engineering.csv" in archive.namelist()
    assert "not approved for direct SMT" in result["manifest"]["assembly_status"]


def test_dnp_is_excluded_from_engineering_bom_and_positions(setup):
    service, data = imported(setup)
    service.toolchain.board["footprints"][0]["dnp"] = True
    excluded_ref = service.toolchain.board["footprints"][0]["reference"]
    result = service.build_release("demo", data["id"])
    assert result["status"] == "released"
    with zipfile.ZipFile(result["archive"]) as archive:
        bom = list(csv.DictReader(io.StringIO(archive.read("manufacturing/bom-engineering.csv").decode("utf-8-sig"))))
        positions = list(csv.DictReader(io.StringIO(archive.read("manufacturing/positions.csv").decode("utf-8-sig"))))
    assert excluded_ref not in {row["Reference"] for row in bom}
    assert {row["Reference"] for row in bom} == {row["Ref"] for row in positions}


@pytest.mark.parametrize("mode", ["dnp_present", "missing", "duplicate"])
def test_position_reference_mismatch_blocks_archive(setup, mode):
    service, data = imported(setup)
    refs = [fp["reference"] for fp in service.toolchain.board["footprints"]]
    if mode == "dnp_present":
        service.toolchain.board["footprints"][0]["dnp"] = True
        service.toolchain.position_references = refs
    elif mode == "missing":
        service.toolchain.position_references = refs[1:]
    else:
        service.toolchain.position_references = refs + [refs[0]]
    result = service.build_release("demo", data["id"])
    assert result["status"] == "blocked"
    assert "references differ" in result["reason"]
    assert not list(service.store.root.rglob("*.zip"))


def test_explicit_pass_allows_layout_application(setup, monkeypatch):
    service, data = imported(setup)
    monkeypatch.setattr("pcb_weaver.service.plan_placements", lambda *args: {
        "candidates": [{"id": "c-1", "feasible": True, "placements": []}]})
    plan = service.plan_layout("demo", data["id"])
    result = service.apply_layout("demo", data["id"], plan["plan_id"], "c-1")
    assert result["status"] == "created"
    assert result["audit"]["passed"] is True
    assert len(service.store.list_revisions("demo")) == 2


@pytest.mark.parametrize("severity", ["ignore", "warning", "exclusion"])
def test_custom_rule_cannot_downgrade_checks_without_project_settings(setup, severity):
    service, board = setup
    custom = board.with_suffix(".kicad_dru")
    custom.write_text(f'(version 1)\n(rule "Clearance waiver" (severity {severity}) (constraint clearance (min 0.2)))')
    before = custom.read_bytes()
    with pytest.raises(ValueError, match="custom rule"):
        service.import_project("demo", str(board))
    assert custom.read_bytes() == before


def test_custom_error_rules_preserved(setup):
    service, board = setup
    custom = board.with_suffix(".kicad_dru")
    custom.write_text('(version 1)\n(rule "Clearance" (severity error) (constraint clearance (min 0.2)))')
    before = custom.read_bytes()
    service, data = imported(setup)
    copied = service.store.revision_dir("demo", data["id"]) / "design" / custom.name
    assert copied.read_bytes() == before


def test_compare_identical_revision_has_no_eco_invalidation(setup):
    service, data = imported(setup)
    result = service.compare_revisions("demo", data["id"], data["id"])
    assert result["impact"]["direct_references"] == []
    assert result["impact"]["affected_nets"] == []
    assert result["impact"]["invalidated_artifacts"] == []


def test_compare_movement_returns_one_hop_eco_impact(setup, monkeypatch):
    service, data = imported(setup)
    board = service.inspect_revision("demo", data["id"])["board"]
    resistor = next(fp for fp in board["footprints"] if fp["reference"] == "R1")
    monkeypatch.setattr("pcb_weaver.service.plan_placements", lambda *args: {
        "candidates": [{"id": "c-1", "feasible": True, "placements": [
            {"reference": "R1", "x": resistor["x"] + 1, "y": resistor["y"]}]}]})
    plan = service.plan_layout("demo", data["id"])
    after = service.apply_layout("demo", data["id"], plan["plan_id"], "c-1")["revision"]
    result = service.compare_revisions("demo", data["id"], after["id"])
    impact = result["impact"]
    assert impact["direct_references"] == ["R1"]
    nets = {pad["net"] for pad in resistor["pads"] if pad["net"]}
    assert {net["net"] for net in impact["affected_nets"]} == nets
    expected_refs = {fp["reference"] for fp in board["footprints"] if any(pad["net"] in nets for pad in fp["pads"])}
    assert set(impact["affected_references"]) == expected_refs
    assert "manufacturing_package" in impact["invalidated_artifacts"]
    assert result["changed_design_files"] == [data["board"]]


def test_compare_uses_after_constraints_and_updated_diff(setup, monkeypatch):
    from pcb_weaver.eco import analyze_impact

    service, data = imported(setup)
    constraints = service.inspect_revision("demo", data["id"])["constraints"]
    constraints["fabrication"]["min_track_mm"] = 0.4
    after = service.update_constraints("demo", data["id"], constraints)["revision"]
    calls = []

    def checked_impact(before_board, after_board, diff, actual_constraints, files, changed):
        assert diff["before"] == data["id"] and diff["after"] == after["id"]
        assert actual_constraints == constraints
        assert files == diff["changed_design_files"]
        assert "demo.kicad_pro" in files
        assert changed is True and diff["constraints_changed"] is True
        calls.append(True)
        return analyze_impact(before_board, after_board, diff, actual_constraints, files, changed)

    monkeypatch.setattr("pcb_weaver.service.analyze_impact", checked_impact)
    result = service.compare_revisions("demo", data["id"], after["id"])
    assert calls == [True]
    assert result["impact"]["global_rules_changed"] is True
    assert {"constraint": "board/fabrication", "reason": "global_rule_change"} in result["impact"]["affected_constraints"]


def test_compare_pure_copper_change_returns_net_impact(setup):
    service, data = imported(setup)
    child, folder, old = service._new("demo", data["id"])
    target = folder / "design" / old["board"]
    tree = sexpdata.loads(target.read_text(encoding="utf-8"))
    tree.append(sexpdata.loads('(segment (start 25 35) (end 28 39) (width 0.25) (layer "F.Cu") (net 2))'))
    target.write_text(sexpdata.dumps(tree), encoding="utf-8")
    after = service._seal("demo", child, folder, old["board"], data["id"], "test-copper-change")
    result = service.compare_revisions("demo", data["id"], after["id"])
    assert result["impact"]["direct_references"] == []
    affected = result["impact"]["affected_nets"]
    assert len(affected) == 1
    assert affected[0]["net"] == "VIN"
    assert affected[0]["reasons"] == ["copper_geometry_changed"]
    assert "full_board_DRC" in result["impact"]["required_checks"]


def library_table(path, uri, library_type="KiCad"):
    tag = "fp_lib_table" if path.name == "fp-lib-table" else "sym_lib_table"
    tree = [sexpdata.Symbol(tag), [sexpdata.Symbol("version"), 7],
            [sexpdata.Symbol("lib"), [sexpdata.Symbol("name"), "Local"],
             [sexpdata.Symbol("type"), library_type], [sexpdata.Symbol("uri"), uri],
             [sexpdata.Symbol("options"), ""], [sexpdata.Symbol("descr"), "Test fixture"]]]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sexpdata.dumps(tree), encoding="utf-8")


def local_library(root, table_name):
    if table_name == "fp-lib-table":
        library = root / "libraries" / "Local.pretty"
        library.mkdir(parents=True, exist_ok=True)
        (library / "One.kicad_mod").write_text('(footprint "One")')
        nested = library / "nested"
        nested.mkdir()
        (nested / "Two.kicad_mod").write_text('(footprint "Two")')
        (nested / "Related.kicad_sym").write_text("(kicad_symbol_lib)")
    else:
        library = root / "libraries" / "Local.kicad_sym"
        library.parent.mkdir(parents=True, exist_ok=True)
        library.write_text("(kicad_symbol_lib)")
    return library


@pytest.mark.parametrize("table_name", ["fp-lib-table", "sym-lib-table"])
@pytest.mark.parametrize("form", ["kiprjmod", "relative", "normalized_parent", "backslash"])
def test_local_library_uris_are_copied_and_hashed(setup, table_name, form):
    service, board = setup
    library = local_library(board.parent, table_name)
    relative = library.relative_to(board.parent).as_posix()
    uri = {"kiprjmod": "${KIPRJMOD}/" + relative,
           "relative": relative,
           "normalized_parent": "${KIPRJMOD}/libraries/../" + relative,
           "backslash": "${KIPRJMOD}\\" + relative.replace("/", "\\")}[form]
    table = board.parent / table_name
    library_table(table, uri)
    original_table = table.read_bytes()
    service, data = imported(setup)
    design = service.store.revision_dir("demo", data["id"]) / "design"
    members = [p for p in library.rglob("*") if p.is_file()] if library.is_dir() else [library]
    for member in members:
        name = member.relative_to(board.parent).as_posix()
        assert name in data["files"]
        assert data["files"][name] == digest(member) == digest(design / name)
    assert (design / table_name).read_bytes() == original_table
    assert data["dependency_scope"]["project_library_uris"] == "validated_local_snapshot"
    assert data["dependency_scope"]["global_libraries_snapshotted"] is False
    assert design_files(design) == data["files"]


@pytest.mark.parametrize("table_name", ["fp-lib-table", "sym-lib-table"])
@pytest.mark.parametrize("uri", [
    "C:/external/Library", "C:Library", "/external/Library", "//server/share/Library",
    "https://example.invalid/Library", "file:///tmp/Library", "git+https://example.invalid/Library",
    "${KICAD9_SYMBOL_DIR}/Library", "$(KIPRJMOD)/Library", "%KIPRJMOD%/Library",
    "${KIPRJMOD}/${OTHER}/Library", "~/Library", "../external/Library",
    "${KIPRJMOD}/../../external/Library", "${KIPRJMOD}//server/share/Library",
    "${KIPRJMOD}/C:/external/Library", "libraries/%2e%2e/Library",
])
def test_library_uri_escape_variables_and_network_rejected(setup, table_name, uri):
    service, board = setup
    table = board.parent / table_name
    library_table(table, uri)
    before = table.read_bytes()
    with pytest.raises(ValueError, match="library URI|Library URI"):
        service.import_project("demo", str(board))
    assert table.read_bytes() == before
    assert service.store.list_revisions("demo") == []


@pytest.mark.parametrize("table_name", ["fp-lib-table", "sym-lib-table"])
def test_absolute_uri_inside_project_still_rejected(setup, table_name):
    service, board = setup
    library = local_library(board.parent, table_name)
    library_table(board.parent / table_name, str(library))
    with pytest.raises(ValueError, match="library URI"):
        service.import_project("demo", str(board))


@pytest.mark.parametrize("table_name", ["fp-lib-table", "sym-lib-table"])
def test_missing_library_target_rejected(setup, table_name):
    service, board = setup
    library_table(board.parent / table_name, "${KIPRJMOD}/missing")
    with pytest.raises(ValueError, match="existing native"):
        service.import_project("demo", str(board))


def test_empty_footprint_library_rejected_instead_of_disappearing_from_copy(setup):
    service, board = setup
    (board.parent / "Empty.pretty").mkdir()
    library_table(board.parent / "fp-lib-table", "Empty.pretty")
    with pytest.raises(ValueError, match="no snapshot-compatible"):
        service.import_project("demo", str(board))


@pytest.mark.parametrize("table_name", ["fp-lib-table", "sym-lib-table"])
@pytest.mark.parametrize("library_type", ["Legacy", "Github", "Database"])
def test_unsupported_library_plugins_rejected(setup, table_name, library_type):
    service, board = setup
    library = local_library(board.parent, table_name)
    library_table(board.parent / table_name, library.relative_to(board.parent).as_posix(), library_type)
    with pytest.raises(ValueError, match="Only native KiCad"):
        service.import_project("demo", str(board))


@pytest.mark.parametrize("entry", [
    '(fp_lib_table (lib (type "KiCad")))',
    '(fp_lib_table (lib (type "KiCad") (uri "a") (uri "b")))',
    '(fp_lib_table (lib (uri "a")))',
    '(fp_lib_table (lib (type "KiCad") (uri "")))',
    '(fp_lib_table (table (uri "outside")))',
    '(sym_lib_table)',
])
def test_malformed_or_indirect_library_table_is_not_ignored(setup, entry):
    service, board = setup
    (board.parent / "fp-lib-table").write_text(entry)
    with pytest.raises(ValueError):
        service.import_project("demo", str(board))


@pytest.mark.parametrize("table_name", ["fp-lib-table", "sym-lib-table"])
def test_referenced_library_members_must_all_be_in_manifest(setup, table_name):
    from pcb_weaver.service import _validate_library_tables

    _, board = setup
    library = local_library(board.parent, table_name)
    library_table(board.parent / table_name, library.relative_to(board.parent).as_posix())
    files = design_files(board.parent)
    member = library / "nested" / "Two.kicad_mod" if library.is_dir() else library
    del files[member.relative_to(board.parent).as_posix()]
    with pytest.raises(ValueError, match="not included in the snapshot"):
        _validate_library_tables(board.parent, files)


@pytest.mark.parametrize("table_name", ["fp-lib-table", "sym-lib-table"])
def test_library_edits_invalidate_revision_but_source_edits_do_not(setup, table_name):
    service, board = setup
    library = local_library(board.parent, table_name)
    library_table(board.parent / table_name, "${KIPRJMOD}/" + library.relative_to(board.parent).as_posix())
    service, data = imported(setup)
    source = library / "nested" / "Two.kicad_mod" if library.is_dir() else library
    source.write_text(source.read_text() + "\n")
    assert service.inspect_revision("demo", data["id"])["revision"]["digest"] == data["digest"]
    new_data = service.import_project("demo", str(board))["revision"]
    assert new_data["digest"] != data["digest"]
    target = service.store.revision_dir("demo", data["id"]) / "design" / source.relative_to(board.parent)
    target.write_text(target.read_text() + "\n")
    with pytest.raises(ValueError, match="changed outside"):
        service.inspect_revision("demo", data["id"])


def test_nested_table_kiprjmod_is_rooted_at_project_not_table_directory(setup):
    service, board = setup
    library = local_library(board.parent, "sym-lib-table")
    library_table(board.parent / "nested" / "sym-lib-table", "${KIPRJMOD}/libraries/Local.kicad_sym")
    service, data = imported(setup)
    assert library.relative_to(board.parent).as_posix() in data["files"]
    assert "nested/sym-lib-table" in data["dependency_scope"]["project_library_tables"]


def test_nested_table_cannot_escape_using_kiprjmod_parent(setup):
    service, board = setup
    library_table(board.parent / "nested" / "sym-lib-table", "${KIPRJMOD}/../external.kicad_sym")
    with pytest.raises(ValueError, match="escapes"):
        service.import_project("demo", str(board))


def test_no_tables_allows_embedded_design_but_never_claims_global_libraries_pinned(setup):
    service, data = imported(setup)
    scope = data["dependency_scope"]
    assert scope["project_library_tables"] == []
    assert scope["project_library_uris"] == "not_declared"
    assert scope["global_libraries_snapshotted"] is False
    result = service.build_release("demo", data["id"])
    assert result["status"] == "released"
    assert result["manifest"]["dependency_scope"] == scope
    with zipfile.ZipFile(result["archive"]) as archive:
        report = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith("/result.json"))))
    assert report["dependency_scope"] == scope


@pytest.mark.parametrize("table_name", ["fp-lib-table", "sym-lib-table"])
def test_valid_unquoted_library_uri_is_supported(setup, table_name):
    service, board = setup
    library = local_library(board.parent, table_name)
    uri = "${KIPRJMOD}/" + library.relative_to(board.parent).as_posix()
    tag = "fp_lib_table" if table_name == "fp-lib-table" else "sym_lib_table"
    (board.parent / table_name).write_text(f'({tag} (lib (name Local) (type KiCad) (uri {uri})))')
    service, data = imported(setup)
    assert data["dependency_scope"]["project_library_uris"] == "validated_local_snapshot"


@pytest.mark.parametrize("table_name", ["fp-lib-table", "sym-lib-table"])
def test_library_symlink_cannot_import_external_files(setup, tmp_path, table_name):
    service, board = setup
    external = local_library(tmp_path / "external", table_name)
    link = board.parent / external.name
    try:
        link.symlink_to(external, target_is_directory=external.is_dir())
    except OSError:
        pytest.skip("Creating a symlink requires unavailable OS privileges")
    library_table(board.parent / table_name, link.name)
    with pytest.raises(ValueError, match="Symlinks"):
        service.import_project("demo", str(board))
