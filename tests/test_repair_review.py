"""Independent safety-review reproductions; synthetic results, never native EDA.

Only temporary projects are written. Regressions require fail-closed behavior,
including rejection before merge. The real ledger/report reader, geometry merge
and native export hash wrapper run; a fault-free control exercises acceptance.
"""

from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sexpdata

from pcb_weaver import catalog, repair
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, now, read_json, write_json
from pcb_weaver.toolchain import Toolchain, _parse_check


def _finding(kind="unconnected_items", severity="error", identities=("a", "b")):
    return {"type": kind, "severity": severity, "description": kind,
            "items": [{"uuid": identity, "description": identity,
                       "pos": {"x": 30, "y": 35}} for identity in identities]}


def _report(missing, violations=()):
    return {"$schema": "https://schemas.kicad.org/drc.v1.json",
            "source": "two-layer.kicad_pcb", "date": "2026-09-07T00:00:00Z",
            "kicad_version": "9.0.0", "coordinate_units": "mm",
            "unconnected_items": missing, "violations": list(violations), "schematic_parity": []}


def _check(report, erc_warnings=0):
    drc = {**_parse_check(report, "drc"), "status": "ok", "report_valid": True}
    return {"status": "blocked" if drc["errors"] or drc["unconnected"] else "passed",
            "reasons": ["DRC unavailable, errors present, or connections incomplete"]
                       if drc["errors"] or drc["unconnected"] else [],
            "drc": drc,
            "erc": {"status": "ok", "report_valid": True, "errors": 0,
                    "warnings": erc_warnings, "excluded": 0, "ignored_checks": []},
            "connectivity": {"status": "passed"}, "constraints": {"passed": True},
            "persisted_track_minima": {"passed": True}}


@pytest.fixture(autouse=True)
def no_native_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Review reproductions must not start native processes")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def test_review_rejects_errors_balanced_by_warning_connections():
    warning = _finding(severity="warning")
    error = _finding("clearance")
    before = _report([warning, deepcopy(warning)], [error, deepcopy(error)])
    after = _report([deepcopy(warning)], [deepcopy(error)])
    old, new = _check(before), _check(after)
    assert old["drc"]["errors"] == old["drc"]["unconnected"] == 2
    assert new["drc"]["errors"] == new["drc"]["unconnected"] == 1
    result = repair.compare_checks(old, new, before, after,
                                   {"a": "VIN", "b": "VIN"}, {"a": "VIN", "b": "VIN"}, ["VIN"])
    assert not result["accepted"], "Existing clearance error survived the repair acceptance gate"


def test_review_rejects_new_erc_warning():
    before, after = _report([_finding()]), _report([])
    result = repair.compare_checks(_check(before), _check(after, erc_warnings=1), before, after,
                                   {"a": "VIN", "b": "VIN"}, {"a": "VIN", "b": "VIN"}, ["VIN"])
    assert not result["accepted"], "New ERC warning is accepted alongside connection improvement"


def _parse(path):
    return sexpdata.loads(path.read_text(encoding="utf-8-sig"), nil=None, true=None, false=None)


def _children(node, name):
    return [n for n in node if isinstance(n, list) and n and n[0] == sexpdata.Symbol(name)]


def _write_board(path, tree):
    path.write_text(sexpdata.dumps(tree) + "\n", encoding="utf-8")


def _segment(identity, x):
    return sexpdata.loads(f'(segment (start {x} 35) (end {x + 2} 35) (width 0.4) '
                          f'(layer "F.Cu") (net 2) (uuid "{identity}"))')


def _record_check(service, project, revision, raw, erc_findings=()):
    """Publish synthetic native bytes through the real immutable evidence ledger."""
    data, folder = service._verified(project, revision)
    run_id = "v-" + uuid4().hex[:12]
    evidence = folder / "verification" / run_id
    shutil.copytree(folder / "design", evidence / "design")
    path = evidence / "drc.json"
    raw = deepcopy(raw)
    raw["source"] = data["board"]
    write_json(path, raw)
    erc_path = evidence / "erc.json"
    schematic = Path(data["board"]).with_suffix(".kicad_sch")
    erc_raw = {"$schema": "https://schemas.kicad.org/erc.v1.json",
               "source": schematic.name, "date": raw["date"],
               "kicad_version": raw["kicad_version"],
               "sheets": [{"path": "/", "uuid_path": "/",
                           "violations": deepcopy(list(erc_findings))}]}
    write_json(erc_path, erc_raw)
    result = _check(raw)
    result.update(verification_id=run_id, revision=revision, revision_digest=data["digest"],
                  created=now(), evidence_hashes={"drc.json": digest(path), "erc.json": digest(erc_path)})
    result["drc"].update(report_path=str(path), report_sha256=digest(path),
                         source_sha256=data["files"][data["board"]])
    result["erc"] = {**_parse_check(erc_raw, "erc"), "status": "ok", "report_valid": True,
                     "report_path": str(erc_path), "report_sha256": digest(erc_path),
                     "source_sha256": data["files"][schematic.as_posix()]}
    write_json(evidence / "result.json", result)
    service.store.event(project, "verification_completed", {
        "revision": revision, "verification_id": run_id,
        "status": result["status"], "sha256": digest(evidence / "result.json")})
    return result


@pytest.fixture
def harness(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[1] / "examples" / "manufacturing-demo"
    staging = tmp_path / "input"
    shutil.copytree(example, staging)
    staged = staging / "two-layer.kicad_pcb"
    tree = _parse(staged)
    for footprint in _children(tree, "footprint"):
        for pad in _children(footprint, "pad"):
            if not _children(pad, "uuid") and not _children(pad, "tstamp"):
                pad.append([sexpdata.Symbol("uuid"), str(uuid4())])
    tree.append(_segment("review-outside", 55))
    _write_board(staged, tree)
    project = "review-only"
    service = EngineeringService(tmp_path / "managed")
    revision = service.import_project(project, str(staged))["revision"]["id"]
    data, folder = service._verified(project, revision)
    source = folder / "design" / data["board"]
    pins = [key for key, net in repair._net_index(source).items() if net == "VIN" and key != "review-outside"]
    assert len(pins) >= 2
    baseline = _report([_finding(identities=pins[:2])])
    h = SimpleNamespace(service=service, project=project, revision=revision, source=source,
                        folder=folder, data=data, changed=[], before_hash=digest(source),
                        baseline_path=None, fault=None, native_operations=[])

    def verify(p, r):
        erc_findings = []
        if h.fault == "erc_same_count":
            warning = _finding("lib_symbol_mismatch", "warning", pins[:1])
            warning["description"] = "Original warning" if r == revision else "Different warning"
            erc_findings.append(warning)
        result = _record_check(service, p, r, baseline if r == revision else _report([]), erc_findings)
        if r == revision:
            h.baseline_path = folder / "verification" / result["verification_id"] / "drc.json"
        return result

    monkeypatch.setattr(service, "_verify", verify)

    def mutate(path, content=None):
        old = digest(path)
        if content is None:
            path.write_bytes(path.read_bytes() + b"\n")
        else:
            path.write_text(content, encoding="utf-8")
        h.changed.append((path, old, digest(path)))

    class SyntheticNative(Toolchain):
        # Keep _native_export, _prepare and their input-hash checks intact.
        def _native(self, operation, *paths):
            h.native_operations.append(operation)
            if operation == "export-dsn":
                board, output = paths
                h.working = board
                h.empty = output.parent / "empty" / board.name
                output.write_text('''(pcb "review.dsn"
                  (parser (string_quote ") (host_cad "KiCad's Pcbnew"))
                  (resolution um 10)
                  (structure (layer F.Cu (type signal)) (layer B.Cu (type signal))
                    (boundary (rect pcb 20 20 90 65)) (rule (width 400) (clearance 200)))
                  (placement) (library)
                  (network (net GND) (net VIN) (net FILTERED))
                  (wiring (wire (path F.Cu 400 55000 35000 57000 35000) (net VIN) (type fix))))''',
                                  encoding="utf-8")
                if h.fault == "empty_board":
                    mutate(h.empty)
                elif h.fault == "working_rules":
                    rules = read_json(board.with_suffix(".kicad_pro"))
                    rules["board"]["design_settings"]["rules"]["min_clearance"] = 0
                    import json
                    mutate(board.with_suffix(".kicad_pro"), json.dumps(rules))
            elif operation == "import-ses":
                empty, ses, output = paths
                if h.fault == "protected_after_route":
                    mutate(ses.parent / "protected.dsn")
                if h.fault == "import_alias":
                    output.hardlink_to(h.working)
                else:
                    imported = _parse(empty)
                    imported.append(_segment("review-addition", 30))
                    _write_board(output, imported)
            else:
                pytest.fail("Unexpected native operation: " + operation)
            return {"status": "ok", "native": {}}

        def route(self, dsn, ses, passes):
            assert 1 <= passes <= 10
            if h.fault == "baseline_report":
                mutate(h.baseline_path)
            elif h.fault == "working_board":
                mutate(h.working)
            elif h.fault == "empty_rules":
                mutate(h.empty.with_suffix(".kicad_pro"))
            elif h.fault == "raw_dsn":
                mutate(dsn.parent / "raw.dsn")
            ses.write_text("synthetic session; no routing execution", encoding="utf-8")
            return {"status": "ok", "dsn_sha256": digest(dsn), "ses_sha256": digest(ses)}

    monkeypatch.setattr(repair, "Toolchain", SyntheticNative)
    return h


@pytest.mark.parametrize("fault", ["working_board", "empty_board", "working_rules",
                                   "empty_rules", "raw_dsn", "protected_after_route"])
def test_review_rejects_cross_stage_input_mutation(harness, fault):
    h = harness
    h.fault = fault
    result = h.service.repair_revision(h.project, h.revision, ["VIN"], [20, 20, 40, 45], passes=1)
    assert h.changed and all(old != new for _, old, new in h.changed)
    assert digest(h.source) == h.before_hash
    assert result["status"] == "blocked", f"Changed {fault} accepted: {result['status']}"


def test_review_rejects_changed_baseline_evidence(harness):
    h = harness
    h.fault = "baseline_report"
    result = h.service.repair_revision(h.project, h.revision, ["VIN"], [20, 20, 40, 45], passes=1)
    assert h.changed and all(old != new for _, old, new in h.changed)
    assert digest(h.source) == h.before_hash
    assert catalog.verification(h.service, h.project, h.revision)["status"] == "invalid_evidence"
    assert result["status"] == "blocked", "Repair accepted although its baseline ledger is now invalid"


def test_review_rejects_imported_working_board_alias(harness):
    h = harness
    h.fault = "import_alias"
    result = h.service.repair_revision(h.project, h.revision, ["VIN"], [20, 20, 40, 45], passes=1)
    attempt = h.folder / "repairs" / result["attempt_id"]
    imported = attempt / "imported" / h.source.name
    assert imported.samefile(h.working)
    assert digest(h.source) == h.before_hash
    assert result["status"] == "blocked", "Import alias accepted as independently produced routing output"


def test_review_accepts_fault_free_control_with_authenticated_erc(harness):
    h = harness
    result = h.service.repair_revision(h.project, h.revision, ["VIN"], [20, 20, 40, 45], passes=1)
    assert not h.changed
    assert digest(h.source) == h.before_hash
    assert result["status"] == "repaired", result.get("reason")
    assert result["patch"]["added"] == 1
    assert result["patch"]["preservation"]["retained_copper_count"] == 1
    assert result["comparison"]["accepted"] is True
    assert result["manufacturing_authorized"] is False
    for revision in (h.revision, result["revision"]):
        check = catalog.verification(h.service, h.project, revision)
        assert check["status"] not in {"invalid_evidence", "not_verified"}
        assert check["erc"]["report_sha256"] == check["evidence_hashes"]["erc.json"]


def test_review_rejects_changed_erc_finding_with_same_warning_count(harness):
    h = harness
    h.fault = "erc_same_count"
    result = h.service.repair_revision(h.project, h.revision, ["VIN"], [20, 20, 40, 45], passes=1)
    assert digest(h.source) == h.before_hash
    checks = [catalog.verification(h.service, h.project, r)
              for r in (h.revision, result["candidate_revision"])]
    assert all(check["status"] not in {"invalid_evidence", "not_verified"} for check in checks)
    assert [check["erc"]["warnings"] for check in checks] == [1, 1]
    reports = [read_json(Path(check["erc"]["report_path"])) for check in checks]
    findings = [report["sheets"][0]["violations"] for report in reports]
    assert [len(items) for items in findings] == [1, 1]
    assert findings[0][0]["description"] != findings[1][0]["description"]
    assert result["status"] == "blocked"
    assert result["revision"] == h.revision
    assert result["comparison"]["accepted"] is False
    assert "New native ERC finding appeared" in result["comparison"]["reasons"]
    assert "New ERC warnings appeared" not in result["comparison"]["reasons"]
    assert result["manufacturing_authorized"] is False
