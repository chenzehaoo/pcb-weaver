"""Orchestration fault injection only; native acceptance is separate."""
from pathlib import Path
from types import SimpleNamespace
import shutil

import pytest

from pcb_weaver import repair as module
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest


@pytest.fixture
def engine(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    service = EngineeringService(tmp_path / "data")
    imported = service.import_project("repair-test", str(root / "examples/manufacturing-demo/two-layer.kicad_pcb"))
    revision = imported["revision"]["id"]
    data, folder = service._verified("repair-test", revision)
    path = folder / "design" / data["board"]
    from test_repair import check, report
    baseline = check(1)
    baseline["verification_id"] = "v-test"
    monkeypatch.setattr(service, "_verify", lambda *a: baseline)
    monkeypatch.setattr(module, "_report", lambda *a: report([("a", "b")]))
    monkeypatch.setattr(module, "_erc_report", lambda *a: {"$schema":"https://schemas.kicad.org/erc.v1.json", "sheets": []})
    monkeypatch.setattr(module, "_net_index", lambda *a: {"a":"VIN", "b":"VIN"})
    return service, revision, path


class FakeRouter:
    def __init__(self, config):
        self.route_timeout = config["route_timeout_seconds"]

    def export_dsn(self, board, output):
        output.write_text("fake-boundary-not-EDA")
        return {"status":"ok"}

    def route(self, source, output, passes):
        output.write_text("fake-session-not-EDA")
        return {"status":"ok"}

    def import_ses(self, empty, ses, output):
        output.parent.mkdir(parents=True)
        shutil.copy2(empty, output)
        return {"status":"ok"}


def setup_router(monkeypatch):
    from pcb_weaver import repair_dsn
    monkeypatch.setattr(module, "Toolchain", FakeRouter)
    def protect(source, destination):
        shutil.copy2(source, destination)
        return {"protected_count":0}
    monkeypatch.setattr(repair_dsn, "protect_dsn", protect)


def test_export_failure_preserves_parent_and_records_attempt(engine,monkeypatch):
    service, revision, path = engine
    setup_router(monkeypatch)
    monkeypatch.setattr(FakeRouter,"export_dsn",lambda *a:{"status":"blocked","reason":"Injected missing tool"})
    before = digest(path)
    result = service.repair_revision("repair-test",revision,["VIN"],[20,20,40,45],passes=1)
    assert result["status"] == "blocked" and result["revision"] == revision
    assert digest(path) == before
    assert "candidate_revision" not in result
    assert service.store.history("repair-test")["events"][-1]["kind"] == "repair_completed"


def test_no_progress_candidate_retained_but_not_adopted(engine,monkeypatch):
    service, revision, path = engine
    setup_router(monkeypatch)
    before = digest(path)
    result = service.repair_revision("repair-test",revision,["VIN"],[20,20,40,45],passes=1)
    assert result["status"] == "blocked" and result["revision"] == revision
    assert result["candidate_revision"] != revision
    assert result["comparison"]["accepted"] is False
    assert result["manufacturing_authorized"] is False
    assert digest(path) == before
    assert len(service.store.list_revisions("repair-test")) == 2


def test_baseline_other_error_stops_before_router(engine,monkeypatch):
    service, revision, _ = engine
    setup_router(monkeypatch)
    monkeypatch.setattr(service,"_verify",lambda *a:{"verification_id":"v-bad","drc":{},"erc":{},"reasons":["Engine changed the verification design snapshot"]})
    result = service.repair_revision("repair-test",revision,["VIN"],[20,20,40,45],passes=1)
    assert result["status"] == "blocked" and not result["operations"]
    assert "Baseline" in result["reason"]


def test_failed_native_route_is_not_imported(engine,monkeypatch):
    service, revision, _ = engine
    setup_router(monkeypatch)
    monkeypatch.setattr(FakeRouter,"route",lambda *a:{"status":"failed","timed_out":True})
    result = service.repair_revision("repair-test",revision,["VIN"],[20,20,40,45],passes=1)
    assert result["status"] == "blocked" and result["operations"][-1]["stage"] == "route"
    assert len(service.store.list_revisions("repair-test")) == 1


def test_unknown_copper_ids_fail_before_native(engine,monkeypatch):
    service, revision, _ = engine
    setup_router(monkeypatch)
    result = service.repair_revision("repair-test",revision,["VIN"],[20,20,40,45],["unknown"],passes=1)
    assert result["status"] == "blocked" and result["operations"] == []
