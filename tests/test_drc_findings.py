"""Ledger-bound diagnostic tests; synthetic reports are not EDA signoff."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from pcb_weaver import catalog
from pcb_weaver.storage import Store, digest, write_json


@pytest.fixture
def engine(tmp_path):
    store = Store(tmp_path / "workspace")
    folder = store.revision_dir("demo", "r-a")
    board = folder / "design/board.kicad_pcb"
    board.parent.mkdir(parents=True)
    board.write_bytes(b"synthetic board bytes, not a native EDA fixture")
    data = {"digest": "revision-a", "board": board.name, "files": {board.name: digest(board)}}

    def verified(project, revision):
        assert project == "demo"
        if revision != "r-a":
            raise ValueError("Unknown revision")
        if digest(board) != data["files"][board.name]:
            raise ValueError("Revision changed outside PCB Weaver")
        return deepcopy(data), folder

    return SimpleNamespace(store=store, _verified=verified, data=data)


def finding(kind="unconnected_items", severity="error", x=1.25, y=-2.5):
    return {"type": kind, "severity": severity, "description": "Actual report description retained <&>",
            "items": [{"description": "Pad 1 [GND] of U1", "uuid": "8321b67f-37f5-4036-a340-29e5fdf9a75e",
                       "pos": {"x": x, "y": y}}]}


def native_report(**changes):
    return {"$schema": "https://schemas.kicad.org/drc.v1.json", "source": "board.kicad_pcb",
            "date": "2026-09-07T01:00:00Z", "kicad_version": "9.0.9", "coordinate_units": "mm",
            "unconnected_items": [finding()], "violations": [finding("silk_overlap", "warning")],
            "schematic_parity": [], **changes}


def record(engine, report=None, run="v-one", created="2026-09-07T01:00:00Z", raw=None,
           drc_changes=None, result_changes=None):
    data, folder = engine._verified("demo", "r-a")
    directory = folder / "verification" / run
    directory.mkdir(parents=True)
    shutil.copytree(folder / "design", directory / "design")
    path = directory / "drc.json"
    if raw is None:
        raw = json.dumps(native_report() if report is None else report).encode()
    path.write_bytes(raw)
    drc = {"status": "ok", "report_valid": True, "report_path": str(path),
           "source_sha256": data["files"][data["board"]], "report_sha256": digest(path), **(drc_changes or {})}
    result = {"verification_id": run, "revision": "r-a", "revision_digest": data["digest"],
              "status": "blocked", "created": created, "drc": drc,
              "evidence_hashes": {"drc.json": digest(path)}, **(result_changes or {})}
    write_json(directory / "result.json", result)
    engine.store.event("demo", "verification_completed", {"verification_id": run, "revision": "r-a",
                       "status": result["status"], "sha256": digest(directory / "result.json")})
    return directory


def read(engine):
    return catalog.drc_findings(engine, "demo", "r-a")


def invalid(engine):
    result = read(engine)
    assert result["status"] == "invalid_evidence", result
    assert result["items"] == []
    assert result["total"] is None
    return result


def test_blocked_verification_with_authentic_report_returns_ok(engine):
    record(engine)
    original = catalog.verification(engine, "demo", "r-a")
    result = read(engine)
    assert result["status"] == "ok"
    assert result["verification_id"] == "v-one" and result["revision"] == "r-a"
    assert result["total"] == 2 and result["truncated"] == 0
    assert [f["severity"] for f in result["items"]] == ["error", "warning"]
    assert result["items"][0]["items"] == native_report()["unconnected_items"][0]["items"]
    assert result["items"][0]["description"] == "Actual report description retained <&>"
    assert catalog.verification(engine, "demo", "r-a") == original
    assert read(engine) == result


@pytest.mark.parametrize("units,scale", [("mm", 1), ("in", 25.4), ("mils", 0.0254)])
def test_coordinates_are_returned_in_millimetres(engine, units, scale):
    record(engine, native_report(coordinate_units=units))
    result = read(engine)
    assert result["status"] == "ok"
    assert result["items"][0]["items"][0]["pos"] == pytest.approx({"x": 1.25 * scale, "y": -2.5 * scale})


@pytest.mark.parametrize("position", [None, "missing"])
def test_unlocated_native_item_remains_null_not_origin(engine, position):
    report = native_report()
    item = report["unconnected_items"][0]["items"][0]
    if position == "missing":
        del item["pos"]
    else:
        item["pos"] = position
    record(engine, report)
    result = read(engine)
    assert result["status"] == "ok"
    assert result["items"][0]["items"][0]["pos"] is None


def test_no_evidence_and_unavailable_engine_do_not_mean_zero_findings(engine):
    result = read(engine)
    assert result["status"] == "not_verified" and result["total"] is None
    record(engine, drc_changes={"status": "blocked", "report_valid": False})
    result = read(engine)
    assert result["status"] == "unavailable" and result["items"] == []


def test_empty_real_report_can_return_zero(engine):
    record(engine, native_report(unconnected_items=[], violations=[]))
    result = read(engine)
    assert result["status"] == "ok" and result["total"] == result["truncated"] == 0


def test_limit_has_exact_total_including_warnings_and_unique_stable_ids(engine):
    record(engine, native_report(violations=[finding("silk_overlap", "warning") for _ in range(204)]))
    result = read(engine)
    assert result["status"] == "ok"
    assert result["total"] == 205 and result["truncated"] == 5 and len(result["items"]) == 200
    assert len({f["id"] for f in result["items"]}) == 200
    assert sum(f["severity"] == "warning" for f in result["items"]) == 199


@pytest.mark.parametrize("coordinate", [True, False, "2.0", None, float("nan"), float("inf"), -float("inf")])
def test_coordinates_must_be_strict_finite_numbers(engine, coordinate):
    record(engine, native_report(unconnected_items=[finding(x=coordinate)]))
    invalid(engine)


def test_unit_conversion_overflow_is_rejected(engine):
    record(engine, native_report(coordinate_units="in", unconnected_items=[finding(x=1e308)]))
    invalid(engine)


def test_malformed_finding_after_limit_is_not_hidden(engine):
    report = native_report(unconnected_items=[finding() for _ in range(201)], violations=[])
    report["unconnected_items"][-1]["items"][0]["pos"] = {"x": True, "y": 0}
    record(engine, report)
    invalid(engine)


@pytest.mark.parametrize("changes", [
    {"coordinate_units": "cm"}, {"coordinate_units": None}, {"coordinate_units": []},
    {"$schema": "https://schemas.kicad.org/drc.v2.json"}, {"source": "other.kicad_pcb"},
    {"violations": None}, {"unconnected_items": {}},
    {"violations": [None]}, {"violations": [{"severity": "error"}]},
])
def test_unknown_schema_units_and_incomplete_entries_rejected(engine, changes):
    record(engine, native_report(**changes))
    invalid(engine)


@pytest.mark.parametrize("position", [{"x": 2}, {}, [1, 2], {"x": 1, "y": "2"}])
def test_incomplete_position_is_not_silently_removed(engine, position):
    report = native_report()
    report["unconnected_items"][0]["items"][0]["pos"] = position
    record(engine, report)
    invalid(engine)


def test_report_and_result_tampering_are_rejected(engine):
    directory = record(engine)
    (directory / "drc.json").write_bytes(b"{}")
    invalid(engine)


@pytest.mark.parametrize("changes", [{"report_sha256": "0" * 64}, {"source_sha256": "0" * 64},
                                    {"report_sha256": None}, {"source_sha256": None}])
def test_even_recorded_mismatching_report_or_input_hashes_are_rejected(engine, changes):
    record(engine, drc_changes=changes)
    invalid(engine)


@pytest.mark.parametrize("target", ["source", "snapshot", "result"])
def test_source_snapshot_and_result_bytes_bound_to_evidence(engine, target):
    directory = record(engine)
    _, folder = engine._verified("demo", "r-a")
    path = {"source": folder / "design/board.kicad_pcb", "snapshot": directory / "design/board.kicad_pcb",
            "result": directory / "result.json"}[target]
    path.write_bytes(b"changed")
    invalid(engine)


@pytest.mark.parametrize("changes", [{"revision": "r-b"}, {"revision_digest": "stale"}])
def test_stale_or_other_revision_result_is_not_displayed(engine, changes):
    record(engine, result_changes=changes)
    invalid(engine)


def test_latest_verification_wins_even_if_blocked(engine):
    record(engine, native_report(unconnected_items=[], violations=[]), result_changes={"status": "passed"})
    record(engine, run="v-two", created="2026-09-07T02:00:00Z")
    result = read(engine)
    assert result["status"] == "ok" and result["verification_id"] == "v-two" and result["total"] == 2


def test_missing_latest_result_cannot_fall_back(engine):
    record(engine)
    latest = record(engine, run="v-two", created="2026-09-07T02:00:00Z")
    (latest / "result.json").unlink()
    invalid(engine)


@pytest.mark.parametrize("pointer", ["../drc.json", "../../secret", "/outside/drc.json", "C:/outside/drc.json",
                                     "C:outside", "\\\\server\\share\\drc.json", "drc.json:stream", "other.json",
                                     "https://host/v-one/drc.json", "file:///archive/v-one/drc.json",
                                     "C:archive/v-one/drc.json", "C:/archive/../v-one/drc.json",
                                     "C:/archive:stream/v-one/drc.json", "/archive/v-other/drc.json",
                                     "C:/archive/v-one/other.json", "\\\\?\\C:\\archive\\v-one\\drc.json"])
def test_untrusted_report_pointer_never_selects_an_external_file(engine, pointer):
    record(engine, drc_changes={"report_path": pointer})
    invalid(engine)


@pytest.mark.parametrize("pointer", ["C:/archived/workspace/verification/v-one/drc.json",
                                     "D:\\archived\\verification\\v-one\\drc.json",
                                     "/archived/workspace/verification/v-one/drc.json", "drc.json"])
def test_old_absolute_path_is_metadata_not_a_read_target(engine, pointer, monkeypatch):
    record(engine, drc_changes={"report_path": pointer})
    original = catalog._read
    accessed = []

    def local_only(root, artifact):
        assert Path(artifact).is_relative_to(engine.store.root)
        accessed.append(Path(artifact))
        return original(root, artifact)

    monkeypatch.setattr(catalog, "_read", local_only)
    result = read(engine)
    assert result["status"] == "ok", result
    assert result["total"] == 2
    assert any(p.name == "drc.json" for p in accessed)


def test_consistent_copy_root_relocation_preserves_immutable_evidence(engine, tmp_path, monkeypatch):
    directory = record(engine)
    before = read(engine)
    original_result = (directory / "result.json").read_bytes()
    original_ledger = (engine.store.root / "ledger.sqlite3").read_bytes()
    relocated_root = tmp_path / "Desktop delivery" / "data"
    shutil.copytree(engine.store.root, relocated_root)
    engine.store.root.rename(tmp_path / "original-no-longer-at-recorded-path")
    store = Store.__new__(Store)
    store.root = relocated_root.resolve()
    data = deepcopy(engine.data)

    def verified(project, revision):
        folder = store.revision_dir(project, revision)
        if digest(folder / "design" / data["board"]) != data["files"][data["board"]]:
            raise ValueError("Relocated revision changed")
        return deepcopy(data), folder

    relocated = SimpleNamespace(store=store, _verified=verified)
    original_read = catalog._read

    def read_only_new_root(root, artifact):
        assert Path(artifact).is_relative_to(relocated_root)
        return original_read(root, artifact)

    monkeypatch.setattr(catalog, "_read", read_only_new_root)
    assert read(relocated) == before
    copied_result = store.revision_dir("demo", "r-a") / "verification/v-one/result.json"
    assert copied_result.read_bytes() == original_result
    assert (relocated_root / "ledger.sqlite3").read_bytes() == original_ledger
    assert not Path(json.loads(original_result)["drc"]["report_path"]).exists()
    copied_result.with_name("drc.json").write_bytes(b"tampered relocated report")
    invalid(relocated)


def test_unlisted_report_cannot_be_used_even_with_its_own_hash(engine):
    record(engine, result_changes={"evidence_hashes": {}})
    invalid(engine)


@pytest.mark.parametrize("field", ["description", "items"])
def test_nested_output_size_is_bounded(engine, field):
    report = native_report()
    report["violations"][0][field] = "x" * 4097 if field == "description" else [finding()["items"][0]] * 65
    record(engine, report)
    invalid(engine)


@pytest.mark.parametrize("target", ["source", "snapshot", "run"])
def test_directory_aliases_are_rejected_including_internal_targets(engine, target):
    directory = record(engine)
    _, folder = engine._verified("demo", "r-a")
    path = {"source": folder / "design", "snapshot": directory / "design", "run": directory}[target]
    real = path.with_name(path.name + "-real")
    path.rename(real)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(real)], check=True, capture_output=True)
    else:
        path.symlink_to(real, target_is_directory=True)
    try:
        invalid(engine)
    finally:
        if os.name == "nt":
            path.rmdir()
        else:
            path.unlink()


def test_report_replacement_between_verification_and_read_is_rejected(engine, monkeypatch):
    directory = record(engine)
    path = directory / "drc.json"
    original = catalog._read
    changed = False

    def read_then_replace(root, artifact):
        nonlocal changed
        raw = original(root, artifact)
        if artifact == path and not changed:
            changed = True
            path.write_bytes(b"{}")
        return raw

    monkeypatch.setattr(catalog, "_read", read_then_replace)
    invalid(engine)


def test_new_verification_while_reading_does_not_return_old_findings(engine, monkeypatch):
    directory = record(engine)
    path = directory / "drc.json"
    original, reads = catalog._read, 0

    def read_then_complete_another_run(root, artifact):
        nonlocal reads
        raw = original(root, artifact)
        if artifact == path:
            reads += 1
            if reads == 2:
                record(engine, run="v-two", created="2026-09-07T02:00:00Z")
        return raw

    monkeypatch.setattr(catalog, "_read", read_then_complete_another_run)
    invalid(engine)


@pytest.mark.skipif(not os.environ.get("PCB_WEAVER_REAL_DRC"), reason="Opt-in read of existing real 160-board evidence")
def test_real_160_revision_ledger_and_native_report():
    from pcb_weaver.service import EngineeringService

    root = Path(os.environ["PCB_WEAVER_REAL_DRC"])
    project = os.environ.get("PCB_WEAVER_REAL_DRC_PROJECT", "system-mcp-acceptance")
    revision = os.environ.get("PCB_WEAVER_REAL_DRC_REVISION", "r-9b5f5075e65e4931")
    # Do not initialize a new store, run an engine, or change existing evidence.
    store = Store.__new__(Store)
    store.root = root.resolve()
    service = EngineeringService.__new__(EngineeringService)
    service.store = store
    result = catalog.drc_findings(service, project, revision)
    assert result["status"] == "ok", result
    check = catalog.verification(service, project, revision)
    folder = store.revision_dir(project, revision) / "verification" / result["verification_id"]
    raw = (folder / "drc.json").read_bytes()
    report = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == check["drc"]["report_sha256"]
    assert result["total"] == len(report["unconnected_items"]) + len(report["violations"])
    assert result["items"][0]["items"] == report["unconnected_items"][0]["items"]
    print(json.dumps({key: result[key] for key in ("status", "verification_id", "revision", "total", "truncated")}))
