"""MOCK/SYNTHETIC ONLY: no native or benchmark acceptance.

Services and authenticated report readers are mocked; core finding fingerprint
helpers and publication comparisons execute against synthetic report data.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import json
from uuid import uuid4

import pytest

from pcb_weaver.storage import digest, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publish_completion_review", ROOT / "scripts/publish_completion_review.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def finding(uuid, kind="silk_overlap", severity="warning"):
    return {"type": kind, "severity": severity, "description": "Synthetic finding " + uuid,
            "items": [{"uuid": uuid}, {"uuid": uuid + "-other"}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def no_native(*args, **kwargs):
        pytest.fail("Mock publication tests must never start native processes")

    monkeypatch.setattr(subprocess, "Popen", no_native)
    root, isolated = tmp_path / "desktop-copy", tmp_path / "isolated"
    root.mkdir()
    config_path = root / "toolchain.unified.json"
    config_path.write_text('{"synthetic": true}\n', encoding="utf-8")
    (root / ".mcp.json").write_text("mock MCP config remains unchanged", encoding="utf-8")
    original_configs = {p.name: p.read_bytes() for p in root.iterdir()}
    folder = isolated / "projects" / "source-project" / "revisions" / "r-source"
    design = folder / "design"
    design.mkdir(parents=True)
    for name in ("board.kicad_pcb", "board.kicad_pro", "board.kicad_sch", "libs/part.kicad_mod", "LICENSE"):
        path = design / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic bytes: " + name, encoding="utf-8")
    write_json(folder / "constraints.json", {"synthetic": True, "release": {"require_erc": True}})
    source_data = {"id": "r-source", "project": "source-project", "board": "board.kicad_pcb",
                   "files": publisher.file_hashes(design), "constraints_hash": digest(folder / "constraints.json"),
                   "digest": "synthetic-revision-digest"}
    write_json(folder / "revision.json", source_data)
    (folder / "review.html").write_text("original mock report", encoding="utf-8")
    before = {"verification_id": "v-source", "revision": "r-source", "revision_digest": source_data["digest"],
              "status": "passed", "drc": {"status": "ok", "errors": 0, "unconnected": 0, "warnings": 8,
                                          "source_sha256": source_data["files"]["board.kicad_pcb"]},
              "erc": {"status": "ok", "errors": 0, "warnings": 2},
              "connectivity": {"status": "passed", "differences": [], "missing_references": [],
                               "components": [{"reference": "U1", "value": "synthetic"}],
                               "operation": {"output_path": "isolated/netlist.xml", "elapsed_seconds": 1}}}
    state = SimpleNamespace(imports=0, verifications=0, reports=0, services=[], history=[], hook=lambda stage: None,
                            before=before, target_check=None, target_data=None, target_folder=None)
    state.source_drc = {"violations": [finding("drc-" + str(i)) for i in range(8)],
                        "unconnected_items": [], "schematic_parity": []}
    state.source_erc = {"$schema": "https://schemas.kicad.org/erc.v1.json", "sheets": [
        {"uuid_path": "/sheet-id", "violations": [finding("erc-" + str(i), "pin_warning") for i in range(2)]}]}
    source = SimpleNamespace(_verified=lambda project, revision: (deepcopy(source_data), folder))
    target = SimpleNamespace(store=SimpleNamespace(list_revisions=lambda project: state.history))

    def import_project(project, board, constraints):
        state.imports += 1
        assert (root / "data/projects" / project).is_dir(), "Publisher must reserve the new name first"
        state.target_folder = root / "data/projects" / project / "revisions/r-target"
        shutil.copytree(Path(board).parent, state.target_folder / "design")
        write_json(state.target_folder / "constraints.json", constraints)
        state.target_data = {**deepcopy(source_data), "id": "r-target", "project": project}
        write_json(state.target_folder / "revision.json", state.target_data)
        state.hook("import")
        return {"revision": deepcopy(state.target_data)}

    def verify(project, revision):
        state.verifications += 1
        state.target_check = deepcopy(state.before)
        state.target_drc = deepcopy(state.source_drc)
        state.target_erc = deepcopy(state.source_erc)
        state.target_check.update(verification_id="v-target", revision=revision)
        state.target_check["connectivity"]["operation"] = {"output_path": "default/netlist.xml", "elapsed_seconds": 2}
        state.hook("verify")
        return deepcopy(state.target_check)

    target.import_project = import_project
    target.verify_revision = verify
    target._verified = lambda project, revision: (deepcopy(state.target_data), state.target_folder)

    def engine(workspace, config):
        state.services.append(Path(workspace))
        assert config == {"synthetic": True}
        return source if Path(workspace) == isolated else target

    def runtime(workspace, path):
        assert workspace == root / "data" and path == config_path
        return workspace, {"synthetic": True}

    def catalog_verification(service, project, revision):
        state.hook("source_catalog" if service is source else "target_catalog")
        return deepcopy(state.before if service is source else state.target_check)

    def report(service, project, revision):
        assert service is target
        state.reports += 1
        (state.target_folder / "review.html").write_text("new mock report", encoding="utf-8")
        state.hook("report")
        return {"status": "generated", "verification_status": state.target_check["status"]}

    monkeypatch.setattr(publisher, "EngineeringService", engine)
    monkeypatch.setattr(publisher, "load_runtime", runtime)
    monkeypatch.setattr(publisher.catalog, "verification", catalog_verification)
    monkeypatch.setattr(publisher.catalog, "generate_report", report)
    monkeypatch.setattr(publisher, "_report", lambda service, *a: deepcopy(
        state.source_drc if service is source else state.target_drc))
    monkeypatch.setattr(publisher, "_erc_report", lambda service, *a: deepcopy(
        state.source_erc if service is source else state.target_erc))
    pointer = tmp_path / "source-result.json"
    write_json(pointer, {"project": "source-project", "revision": "r-source"})
    args = {"root": root, "source_workspace": isolated, "source_result": pointer,
            "project": "new-review", "output": tmp_path / "publication.json"}
    return SimpleNamespace(args=args, state=state, source_folder=folder, source_data=source_data,
                           config_bytes=original_configs, project_dir=root / "data/projects/new-review")


def test_mock_publication_preserves_exact_bytes_and_records_both_verifications(setup):
    original = publisher.file_hashes(setup.source_folder)
    record_hash = digest(setup.args["source_result"])
    result = publisher.publish_review(**setup.args)
    assert result["status"] == "published_review" and result["engineering_status"] == "passed"
    assert result["wholeboard_benchmark"] is False and result["manufacturing_authorized"] is False
    assert result["source_immutable"] is True and result["preservation_checks_passed"] is True
    assert result["source_hashes"] == original == publisher.file_hashes(setup.source_folder)
    assert result["target_sha256"] == result["source_sha256"] == digest(setup.state.target_folder / "design/board.kicad_pcb")
    assert result["target_files"] == result["source_files"] == setup.source_data["files"]
    assert result["target_constraints_sha256"] == result["source_constraints_sha256"]
    assert result["source_result_sha256"] == record_hash
    assert result["source_verification"]["verification_id"] == "v-source"
    assert result["target_verification"]["verification_id"] == "v-target"
    assert result["source_finding_fingerprints"] == result["target_finding_fingerprints"]
    assert read_json(setup.args["output"]) == result
    assert setup.state.services == [setup.args["source_workspace"], setup.args["root"] / "data"]
    assert (setup.state.imports, setup.state.verifications, setup.state.reports) == (1, 1, 1)
    assert {name: (setup.args["root"] / name).read_bytes() for name in setup.config_bytes} == setup.config_bytes


@pytest.fixture
def synthetic_power(setup):
    """Build real S-expression structure with random UUIDs, no production-rule patches."""
    s = setup
    s.root_uuid, s.stable_uuid, s.pin_a, s.pin_b = (str(uuid4()) for _ in range(4))
    s.schematic = s.source_folder / "design/arbitrary-power.kicad_sch"
    s.sheet_path = "/" + s.root_uuid
    s.net_name = "TEST_POWER"
    s.pin_definition = '(pin power_in line (at 0 0 90) (length 0) (hide yes) (name "TEST_POWER") (number "9"))'
    s.library = '(symbol "test:Supply" (power) (symbol "Supply_1_1" ' + s.pin_definition + '))'
    s.instances = [f'''(symbol (lib_id "test:Supply") (unit 1) (at {i} 0 0) (uuid "{uuid4()}")
        (property "Reference" "#PWR{i}") (property "Value" "TEST_POWER") (dnp no) (exclude_from_sim no)
        (pin "9" (uuid "{u}")) (instances (project "anything" (path "{s.sheet_path}" (reference "#PWR{i}") (unit 1)))))'''
        for i, u in enumerate((s.pin_a, s.pin_b))]
    s.label = f'(label "OTHER_NAME" (at 0 2 0) (uuid "{s.stable_uuid}"))'
    s.text = f'(kicad_sch (version 20250114) (uuid "{s.root_uuid}") (lib_symbols {s.library}) ' + ' '.join(s.instances) + s.label + ')'
    warning = {"type": "multiple_net_names", "severity": "warning", "description": "Synthetic conflicting net names",
               "items": [{"uuid": s.pin_a, "description": "synthetic representative"},
                         {"uuid": s.stable_uuid, "description": "Label OTHER_NAME"}]}
    s.state.source_erc = {"$schema": "https://schemas.kicad.org/erc.v1.json", "sheets": [
        {"uuid_path": s.sheet_path, "violations": [warning]}]}
    s.netlist = '<export><nets><net name="TEST_POWER"><node ref="U7" pin="1"/><node ref="R5" pin="2"/></net><net name="OTHER"><node ref="J4" pin="5"/></net></nets></export>'

    def artifacts(folder, check, report):
        vdir = folder / "verification" / check["verification_id"]
        write_json(vdir / "erc.json", report)
        (vdir / "netlist.xml").write_text(s.netlist, encoding="utf-8")
        check["evidence_hashes"] = {name: digest(vdir / name) for name in ("erc.json", "netlist.xml")}
        check["erc"].update(warnings=len(report["sheets"][0]["violations"]),
                            report_sha256=digest(vdir / "erc.json"), source_sha256=digest(s.schematic))

    def reseal():
        s.schematic.write_text(s.text, encoding="utf-8")
        s.source_data["files"] = publisher.file_hashes(s.schematic.parent)
        write_json(s.source_folder / "revision.json", s.source_data)
        artifacts(s.source_folder, s.state.before, s.state.source_erc)
    s.reseal = reseal
    s.reseal()
    s.mutate = lambda: None
    def hook(stage):
        if stage == "verify":
            for v in s.state.target_erc["sheets"][0]["violations"]:
                v["items"][0]["uuid"] = s.pin_b
            s.mutate()
            artifacts(s.state.target_folder, s.state.target_check, s.state.target_erc)
    s.state.hook = hook
    return s


@pytest.mark.parametrize("name", ["TEST_POWER", "RETURN_24V", "ANALOG_SUPPLY"])
def test_synthetic_structural_representative_keeps_raw_identity_and_proof(synthetic_power, name):
    s = synthetic_power
    s.text = s.text.replace("TEST_POWER", name)
    s.netlist = s.netlist.replace("TEST_POWER", name)
    s.reseal()
    before = publisher.file_hashes(s.source_folder)
    result = publisher.publish_review(**s.args)
    assert result["schema_version"] == 3 and result["status"] == "published_review"
    assert result["source_finding_fingerprints"] != result["target_finding_fingerprints"]
    proof = result["finding_equivalence"]
    assert proof["rule"] == "embedded-global-power-representative-v1"
    assert proof["sheet_chain"] == [{"file": s.schematic.name, "sha256": digest(s.schematic)}]
    assert proof["net_count"] == 2 and len(proof["global_net_nodes"]) == 2
    assert proof["source_pin"]["global_net_name"] == name
    assert proof["source_pin"]["pin_number"] == "9"
    assert proof["source_pin"]["definition"] == proof["target_pin"]["definition"]
    assert proof["source_pin"]["uuid"] == s.pin_a and proof["target_pin"]["uuid"] == s.pin_b
    assert proof["artifacts"][0]["erc_sha256"] != proof["artifacts"][1]["erc_sha256"]
    assert publisher.file_hashes(s.source_folder) == before
    assert publisher.finding_equivalence(s.source_folder, s.state.target_folder,
        result["source_verification"], result["target_verification"], result["source_finding_fingerprints"],
        result["target_finding_fingerprints"]) == proof
    # Proofs are directional but both selections of the reviewed pair are valid.
    reverse = publisher.finding_equivalence(s.state.target_folder, s.source_folder,
        result["target_verification"], result["source_verification"], result["target_finding_fingerprints"],
        result["source_finding_fingerprints"])
    assert reverse["source_difference"] == proof["target_difference"]


@pytest.mark.parametrize("change", ["uuid", "type", "severity", "description", "sheet", "multiplicity", "label", "netlist", "bytes", "drc"])
def test_synthetic_representative_exception_is_narrow(synthetic_power, change):
    s = synthetic_power
    def mutate():
        sheet = s.state.target_erc["sheets"][0]
        v = sheet["violations"][0]
        if change == "uuid":
            v["items"][0]["uuid"] = "unknown-pin"
        elif change in ("type", "severity", "description"):
            v[change] = "changed"
        elif change == "sheet":
            sheet["uuid_path"] += "/child"
        elif change == "multiplicity":
            sheet["violations"].append(deepcopy(v))
        elif change == "label":
            v["items"][1]["description"] = "different label metadata"
        elif change == "netlist":
            s.netlist = s.netlist.replace('ref="J4"', 'ref="J99"')
        elif change == "bytes":
            (s.state.target_folder / "design" / s.schematic.name).write_text("changed schematic", encoding="utf-8")
        else:
            s.state.target_drc["violations"][0]["description"] = "different DRC warning"
    s.mutate = mutate
    with pytest.raises(ValueError):
        publisher.publish_review(**s.args)
    result = read_json(s.args["output"])
    assert result["status"] == "failed" and "preservation_checks_passed" not in result
    assert s.state.reports == 0


@pytest.mark.parametrize("change", ["erc.json", "netlist.xml", "binding", "report_binding"])
def test_synthetic_proof_revalidation_rejects_tampered_evidence(synthetic_power, change):
    s = synthetic_power
    r = publisher.publish_review(**s.args)
    if change in ("erc.json", "netlist.xml"):
        artifact = s.state.target_folder / "verification/v-target" / change
        artifact.write_bytes(artifact.read_bytes() + b" ")
    elif change == "binding":
        r["target_verification"]["erc"]["source_sha256"] = "not-the-schematic"
    else:
        r["target_verification"]["erc"]["report_sha256"] = "not-the-report"
    with pytest.raises(ValueError):
        publisher.finding_equivalence(s.source_folder, s.state.target_folder, r["source_verification"],
            r["target_verification"], r["source_finding_fingerprints"], r["target_finding_fingerprints"])


@pytest.mark.parametrize("change", ["ordinary_hidden", "local_power", "visible", "different_lib", "value", "pin_number",
    "ambiguous_uuid", "inherited", "multiple_pins", "offset", "power_out", "missing_lib", "unit", "path",
    "variable", "no_global_net", "format", "missing_stable", "ambiguous_root"])
def test_synthetic_identical_bytes_do_not_replace_semantic_proof(synthetic_power, change):
    s = synthetic_power
    if change == "ordinary_hidden":
        s.text = s.text.replace("(power)", "")
    elif change == "local_power":
        s.text = s.text.replace("(power)", "(power local)")
    elif change == "visible":
        s.text = s.text.replace("(hide yes)", "(hide no)")
    elif change == "different_lib":
        second_lib = s.library.replace("Supply", "Alternate")
        s.text = s.text.replace(s.library, s.library + second_lib).replace(s.instances[1], s.instances[1].replace("test:Supply", "test:Alternate"))
    elif change == "value":
        s.text = s.text.replace(s.instances[1], s.instances[1].replace('"TEST_POWER"', '"OTHER"'))
    elif change == "pin_number":
        s.text = s.text.replace(s.instances[1], s.instances[1].replace('(pin "9"', '(pin "3"'))
    elif change == "ambiguous_uuid":
        s.text = s.text.replace(s.label, s.label + f'(label "duplicate" (uuid "{s.pin_a}"))')
    elif change == "inherited":
        s.text = s.text.replace("(power)", '(power) (extends "external")')
    elif change == "multiple_pins":
        s.text = s.text.replace(s.pin_definition, s.pin_definition + s.pin_definition.replace('"9"', '"8"'))
    elif change == "offset":
        s.text = s.text.replace("(at 0 0 90)", "(at 1 0 90)")
    elif change == "power_out":
        s.text = s.text.replace("pin power_in", "pin power_out")
    elif change == "missing_lib":
        s.text = s.text.replace(s.library, "")
    elif change == "unit":
        s.text = s.text.replace(s.instances[1], s.instances[1].replace("(unit 1)", "(unit 2)"))
    elif change == "path":
        s.text = s.text.replace(s.instances[1], s.instances[1].replace(s.sheet_path, "/other-instance"))
    elif change == "variable":
        s.text = s.text.replace("TEST_POWER", "${POWER}")
        s.netlist = s.netlist.replace("TEST_POWER", "${POWER}")
    elif change == "no_global_net":
        s.netlist = s.netlist.replace("TEST_POWER", "not_the_proven_global_net")
    elif change == "format":
        s.text = s.text.replace("20250114", "20990101")
    elif change == "missing_stable":
        s.text = s.text.replace(s.label, "")
    else:
        s.schematic.with_name("duplicate-root.kicad_sch").write_text(s.text, encoding="utf-8")
    # Reseal both sides as the same synthetic design: byte equality alone must
    # never qualify unsupported or ambiguous electrical semantics.
    s.reseal()
    with pytest.raises(ValueError):
        publisher.publish_review(**s.args)
    assert read_json(s.args["output"])["status"] == "failed" and s.state.reports == 0


def test_synthetic_equivalent_multiplicity_is_preserved(synthetic_power):
    s = synthetic_power
    findings = s.state.source_erc["sheets"][0]["violations"]
    findings.append(deepcopy(findings[0]))
    s.reseal()
    result = publisher.publish_review(**s.args)
    proof = result["finding_equivalence"]
    assert proof["source_difference"][0]["count"] == proof["target_difference"][0]["count"] == 2
    assert len(proof["raw_findings"][0]) == len(proof["raw_findings"][1]) == 2


def test_synthetic_hierarchical_sheet_resolves_exact_instance(synthetic_power):
    s = synthetic_power
    instance = str(uuid4())
    path = s.sheet_path + "/" + instance
    child = s.text.replace(s.sheet_path, path).replace(f'(uuid "{s.root_uuid}")', f'(uuid "{uuid4()}")')
    s.schematic.with_name("child.kicad_sch").write_text(child, encoding="utf-8")
    s.text = f'(kicad_sch (version 20250114) (uuid "{s.root_uuid}") (sheet (uuid "{instance}") (property "Sheetfile" "child.kicad_sch")))'
    s.state.source_erc["sheets"][0]["uuid_path"] = path
    s.reseal()
    result = publisher.publish_review(**s.args)
    proof = result["finding_equivalence"]
    assert proof["sheet_uuid_path"] == path
    assert [p["file"] for p in proof["sheet_chain"]] == [s.schematic.name, "child.kicad_sch"]
    assert "child_instance" in proof["sheet_chain"][0]


def test_mock_blocked_review_requires_explicit_opt_in(setup):
    setup.state.before["status"] = "blocked"
    setup.state.before["drc"].update(errors=1, unconnected=1)
    setup.state.source_drc["unconnected_items"] = [finding("missing", "unconnected_items", "error")]
    with pytest.raises(ValueError, match="--allow-blocked"):
        publisher.publish_review(**setup.args)
    assert setup.state.imports == setup.state.verifications == 0 and not setup.project_dir.exists()
    failed = read_json(setup.args["output"])
    assert failed["status"] == "failed" and failed["source_verification"]["status"] == "blocked"
    setup.args["output"] = setup.args["output"].with_name("explicit-blocked.json")
    result = publisher.publish_review(**setup.args, allow_blocked=True)
    assert result["status"] == "published_review" and result["engineering_status"] == "blocked"
    assert result["wholeboard_benchmark"] is False
    assert result["target_verification"]["drc"]["unconnected"] == 1


def test_mock_blocked_connectivity_review_can_be_preserved(setup):
    setup.state.before["status"] = "blocked"
    setup.state.before["connectivity"].update(status="failed", differences=[{"reference": "U1", "pad": "1"}])
    result = publisher.publish_review(**setup.args, allow_blocked=True)
    assert result["engineering_status"] == "blocked"
    assert result["target_verification"]["connectivity"]["differences"] == [{"reference": "U1", "pad": "1"}]


@pytest.mark.parametrize("kind", ["output", "project", "ledger"])
def test_mock_existing_names_are_not_reused(setup, kind):
    if kind == "output":
        setup.args["output"].write_bytes(b"existing record")
    elif kind == "project":
        setup.project_dir.mkdir(parents=True)
        (setup.project_dir / "keep.txt").write_bytes(b"existing project")
    else:
        setup.state.history = [{"id": "r-old"}]
    with pytest.raises(ValueError, match="new"):
        publisher.publish_review(**setup.args)
    assert setup.state.imports == setup.state.verifications == 0
    if kind == "output":
        assert setup.args["output"].read_bytes() == b"existing record" and not setup.state.services
    elif kind == "project":
        assert (setup.project_dir / "keep.txt").read_bytes() == b"existing project"
        assert not setup.args["output"].exists()


def test_mock_output_race_is_rejected_before_services_or_import(setup, monkeypatch):
    original_open = Path.open

    def raced_open(path, mode="r", *args, **kwargs):
        if path == setup.args["output"] and mode == "x":
            path.write_bytes(b"another publisher reserved this output")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", raced_open)
    with pytest.raises(FileExistsError):
        publisher.publish_review(**setup.args)
    assert not setup.state.services
    assert setup.args["output"].read_bytes() == b"another publisher reserved this output"


def test_mock_project_race_is_rejected_without_import(setup):
    def hook(stage):
        if stage == "source_catalog":
            setup.project_dir.mkdir(parents=True, exist_ok=True)
            (setup.project_dir / "keep.txt").write_bytes(b"concurrent project")

    setup.state.hook = hook
    with pytest.raises(FileExistsError):
        publisher.publish_review(**setup.args)
    assert setup.state.imports == setup.state.verifications == 0
    assert (setup.project_dir / "keep.txt").read_bytes() == b"concurrent project"
    assert read_json(setup.args["output"])["status"] == "failed"


@pytest.mark.parametrize("stage", ["import", "verify", "report"])
@pytest.mark.parametrize("file", ["design/board.kicad_pcb", "design/libs/part.kicad_mod", "constraints.json"])
def test_mock_target_byte_changes_are_never_published(setup, stage, file):
    def hook(current):
        if current == stage:
            (setup.state.target_folder / file).write_bytes(b"changed target bytes")

    setup.state.hook = hook
    with pytest.raises(ValueError, match="bytes|constraints"):
        publisher.publish_review(**setup.args)
    result = read_json(setup.args["output"])
    assert result["status"] == "failed" and result["project_reserved"] is True
    assert "preservation_checks_passed" not in result
    if stage == "import":
        assert setup.state.verifications == 0


def test_mock_resealed_changed_target_board_is_still_rejected(setup):
    def hook(stage):
        if stage == "import":
            board = setup.state.target_folder / "design/board.kicad_pcb"
            board.write_bytes(b"changed and resealed")
            setup.state.target_data["files"]["board.kicad_pcb"] = digest(board)

    setup.state.hook = hook
    with pytest.raises(ValueError, match="differ from source"):
        publisher.publish_review(**setup.args)
    assert setup.state.verifications == 0
    result = read_json(setup.args["output"])
    assert result["target_sha256"] == digest(setup.state.target_folder / "design/board.kicad_pcb")
    assert result["target_sha256"] != result["source_sha256"]


@pytest.mark.parametrize("change", ["board", "companion", "constraints", "metadata", "source_result", "extra"])
def test_mock_source_mutation_is_detected(setup, change):
    def hook(stage):
        if stage == "report":
            if change == "source_result":
                setup.args["source_result"].write_bytes(b"changed pointer")
            else:
                relative = {"board": "design/board.kicad_pcb", "companion": "design/libs/part.kicad_mod",
                            "constraints": "constraints.json", "metadata": "revision.json", "extra": "extra.txt"}[change]
                (setup.source_folder / relative).write_bytes(b"changed source")

    setup.state.hook = hook
    with pytest.raises(ValueError, match="bytes|constraints|Source checkpoint"):
        publisher.publish_review(**setup.args)
    assert read_json(setup.args["output"])["status"] == "failed"


@pytest.mark.parametrize("field,value", [
    ("drc.errors", 1), ("drc.unconnected", 1), ("drc.warnings", 9),
    ("erc.errors", 1), ("erc.warnings", 3), ("status", "blocked"),
    ("connectivity.status", "failed"), ("connectivity.differences", [{"reference": "U2"}]),
    ("connectivity.components", [{"reference": "U1", "value": "different"}]),
    ("drc.source_sha256", "wrong board"), ("revision_digest", "wrong revision"),
])
def test_mock_native_or_connectivity_mismatch_is_rejected(setup, field, value):
    def hook(stage):
        if stage == "verify":
            target = setup.state.target_check
            keys = field.split(".")
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = value

    setup.state.hook = hook
    with pytest.raises(ValueError):
        publisher.publish_review(**setup.args)
    result = read_json(setup.args["output"])
    assert result["status"] == "failed" and result["target_verification"] == setup.state.target_check
    assert setup.state.reports == 0


@pytest.mark.parametrize("field,value", [("status", "not_verified"), ("status", "invalid_evidence"),
                                        ("drc.warnings", None), ("erc.errors", False)])
def test_mock_missing_source_evidence_is_not_zero_filled(setup, field, value):
    target = setup.state.before
    keys = field.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(ValueError):
        publisher.publish_review(**setup.args, allow_blocked=True)
    assert not setup.project_dir.exists() and setup.state.verifications == 0


def test_mock_post_report_verification_change_fails(setup):
    def hook(stage):
        if stage == "report":
            setup.state.target_check["drc"]["warnings"] += 1

    setup.state.hook = hook
    with pytest.raises(ValueError, match="verification changed"):
        publisher.publish_review(**setup.args)
    assert read_json(setup.args["output"])["status"] == "failed"


def test_mock_native_exception_retains_diagnostic_record_and_new_project(setup):
    def hook(stage):
        if stage == "verify":
            raise RuntimeError("synthetic verification failure")

    setup.state.hook = hook
    with pytest.raises(RuntimeError, match="synthetic"):
        publisher.publish_review(**setup.args)
    result = read_json(setup.args["output"])
    assert result["status"] == "failed" and result["revision"] == "r-target"
    assert result["source_hashes"] and setup.project_dir.exists()
    assert result["wholeboard_benchmark"] is False and setup.state.reports == 0


@pytest.mark.parametrize("project", ["../escape", "", "bad/name"])
def test_mock_invalid_project_has_no_side_effects(setup, project):
    setup.args["project"] = project
    with pytest.raises(ValueError):
        publisher.publish_review(**setup.args)
    assert not setup.state.services and not setup.args["output"].exists()


def test_mock_output_cannot_modify_source_checkpoint(setup):
    setup.args["output"] = setup.source_folder / "new-publication.json"
    with pytest.raises(ValueError, match="outside source"):
        publisher.publish_review(**setup.args)
    assert not setup.state.services and not setup.args["output"].exists()


def test_mock_cli_keeps_pointer_contract_and_exposes_blocked_option(setup):
    setup.state.before["status"] = "blocked"
    argv = []
    for name, value in setup.args.items():
        argv.extend(["--" + name.replace("_", "-"), str(value)])
    result = publisher.main([*argv, "--allow-blocked"])
    assert result["source_project"] == "source-project" and result["source_revision"] == "r-source"
    assert result["engineering_status"] == "blocked"


@pytest.mark.parametrize("kind", ["drc", "erc"])
@pytest.mark.parametrize("change", ["type", "severity", "description", "uuid", "multiplicity"])
def test_mock_equal_counts_with_different_native_findings_are_rejected(setup, kind, change):
    def hook(stage):
        if stage != "verify":
            return
        findings = (setup.state.target_drc["violations"] if kind == "drc"
                    else setup.state.target_erc["sheets"][0]["violations"])
        if change == "uuid":
            findings[0]["items"][0]["uuid"] = "different-item"
        elif change == "multiplicity":
            findings[0] = deepcopy(findings[1])
        else:
            findings[0][change] = "different-" + change

    setup.state.hook = hook
    with pytest.raises(ValueError, match="finding fingerprints differ"):
        publisher.publish_review(**setup.args)
    result = read_json(setup.args["output"])
    assert result["status"] == "failed" and setup.state.reports == 0
    assert result["source_finding_fingerprints"] != result["target_finding_fingerprints"]
    for field in ("errors", "warnings"):
        assert result["source_verification"][kind][field] == result["target_verification"][kind][field]


@pytest.mark.parametrize("section", ["unconnected_items", "schematic_parity"])
def test_mock_blocked_drc_finding_identity_must_still_match(setup, section):
    setup.state.before["status"] = "blocked"
    setup.state.before["drc"]["errors"] = 1
    setup.state.before["drc"]["unconnected"] = int(section == "unconnected_items")
    setup.state.source_drc[section] = [finding("missing-original", section, "error")]

    def hook(stage):
        if stage == "verify":
            setup.state.target_drc[section][0]["items"][0]["uuid"] = "different-missing-item"

    setup.state.hook = hook
    with pytest.raises(ValueError, match="finding fingerprints differ"):
        publisher.publish_review(**setup.args, allow_blocked=True)
    assert read_json(setup.args["output"])["status"] == "failed"


def test_mock_erc_sheet_identity_is_part_of_fingerprint(setup):
    def hook(stage):
        if stage == "verify":
            setup.state.target_erc["sheets"][0]["uuid_path"] = "/other-sheet"

    setup.state.hook = hook
    with pytest.raises(ValueError, match="finding fingerprints differ"):
        publisher.publish_review(**setup.args)


def test_mock_finding_order_and_host_report_metadata_are_not_identity(setup):
    def hook(stage):
        if stage != "verify":
            return
        for report in (setup.state.target_drc, setup.state.target_erc):
            report.update(date="another date", source="different/host/path")
        for findings in (setup.state.target_drc["violations"], setup.state.target_erc["sheets"][0]["violations"]):
            findings.reverse()
            for entry in findings:
                entry["items"].reverse()

    setup.state.hook = hook
    result = publisher.publish_review(**setup.args)
    assert result["status"] == "published_review"
    assert result["source_finding_fingerprints"] == result["target_finding_fingerprints"]


@pytest.mark.parametrize("reader", ["_report", "_erc_report"])
def test_mock_unauthenticated_native_artifact_is_rejected(setup, monkeypatch, reader):
    def unavailable(*args):
        raise ValueError("Synthetic unauthenticated artifact")

    monkeypatch.setattr(publisher, reader, unavailable)
    with pytest.raises(ValueError, match="unauthenticated"):
        publisher.publish_review(**setup.args)
    assert setup.state.imports == setup.state.verifications == 0
    result = read_json(setup.args["output"])
    assert result["status"] == "failed" and result["source_hashes"]
