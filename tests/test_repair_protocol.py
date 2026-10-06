"""Persistent repair protocol tests with no native EDA execution."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pcb_weaver import catalog, jobs, server
from pcb_weaver.jobs import IdempotencyConflict, JobQueue, JobRequest, _checksum
from pcb_weaver.storage import Store, digest
from pcb_weaver.toolchain import Toolchain


def repair_request(**updates):
    return {"project": "repair-test", "operation": "repair", "revision": "r-parent",
            "repair_nets": ["GND", "SIGNAL"], "repair_region": [0.0, 1.0, 10.0, 20.0],
            "passes": 3, **updates}


@pytest.mark.parametrize("updates", [
    {"revision": None}, {"revision": ""}, {"board_path": "board.kicad_pcb"},
    {"repair_nets": None}, {"repair_nets": []}, {"repair_nets": [""]},
    {"repair_nets": [" \t"]}, {"repair_nets": ["GND", "GND"]},
    {"repair_nets": [str(i) for i in range(9)]},
    {"repair_region": None}, {"repair_region": []}, {"repair_region": [0, 0, 1]},
    {"repair_region": [0, 0, 1, 1, 2]}, {"repair_region": [0, 0, 0, 1]},
    {"repair_region": [0, 0, 1, 0]}, {"repair_region": [2, 0, 1, 1]},
    {"repair_region": [0, 2, 1, 1]}, {"repair_region": [0, 0, float("inf"), 1]},
    {"repair_region": [0, float("nan"), 1, 1]},
    {"repair_region": [float("-inf"), 0, 1, 1]},
    {"repair_remove_ids": ["track-1", "track-1"]},
    {"repair_remove_ids": [str(i) for i in range(1001)]}, {"repair_remove_ids": None},
    {"passes": 0}, {"passes": 11}, {"passes": 101},
])
def test_invalid_repair_request(updates):
    with pytest.raises(ValidationError):
        JobRequest.model_validate(repair_request(**updates))


@pytest.mark.parametrize("passes", [1, 3, 10])
def test_repair_accepts_scope_boundaries_and_independent_defaults(passes):
    request = JobRequest.model_validate(repair_request(
        passes=passes, repair_nets=[str(i) for i in range(8)],
        repair_region=[-20, -10, 0, 0], repair_remove_ids=[str(i) for i in range(1000)]))
    assert request.passes == passes and len(request.repair_remove_ids) == 1000
    first = JobRequest.model_validate(repair_request())
    second = JobRequest.model_validate(repair_request())
    first.repair_remove_ids.append("track-1")
    assert second.repair_remove_ids == []


@pytest.mark.parametrize("operation", ["pipeline", "plan", "apply", "route", "verify", "release", "report", "constraints"])
@pytest.mark.parametrize("scope", [{"repair_nets": []}, {"repair_nets": ["GND"]},
                                  {"repair_region": []}, {"repair_region": [0, 0, 1, 1]},
                                  {"repair_remove_ids": ["track-1"]}])
def test_other_operations_reject_repair_fields(operation, scope):
    request = {"project": "repair-test", "revision": "r-parent", "operation": operation}
    if operation == "apply":
        request.update(plan_id="plan-1", candidate_id="candidate-1")
    if operation == "constraints":
        request["constraints"] = {}
    JobRequest.model_validate({**request, "repair_nets": None, "repair_region": None, "repair_remove_ids": []})
    with pytest.raises(ValidationError, match="only accepted for repair"):
        JobRequest.model_validate({**request, **scope})


class FakeEngine:
    def __init__(self, root, config=None, result=None):
        self.store = Store(Path(root))
        self.toolchain = Toolchain(config or {})
        self.result = result if result is not None else {"status": "repaired", "revision": "r-final"}
        self.calls = []

    def inspect_revision(self, project, revision):
        return {"project": project, "revision": revision}

    def repair_revision(self, project, revision, nets, region, remove_ids=None, passes=3):
        self.calls.append((project, revision, nets, region, remove_ids, passes))
        return self.result

    def route_revision(self, *args):
        self.calls.append(args)
        return {"status": "blocked", "reason": "Legacy runtime fixture"}


@pytest.fixture
def reports(monkeypatch):
    calls = []

    def report(engine, project, revision):
        calls.append((project, revision))
        return {"status": "generated", "revision": revision}

    monkeypatch.setattr(catalog, "generate_report", report)
    return calls


@pytest.mark.parametrize("status", ["improved", "repaired"])
def test_repair_dispatch_persists_final_child_and_evidence(tmp_path, reports, status):
    engine = FakeEngine(tmp_path, result={"status": status, "revision": "r-final"})
    sender = JobQueue(tmp_path, engine=engine)
    job = sender.submit(repair_request(repair_remove_ids=["track-1"], release=True))
    assert engine.calls == [] and sender.thread is None
    worker = JobQueue(tmp_path, engine=engine)
    assert worker.run_once()
    final = sender.get(job["id"])
    assert final["status"] == "completed"
    assert final["result"]["revision"] == "r-final"
    assert engine.calls == [("repair-test", "r-parent", ["GND", "SIGNAL"], [0.0, 1.0, 10.0, 20.0], ["track-1"], 3)]
    assert reports == [("repair-test", "r-final")]
    assert set(final["result"]["steps"]) == {"repair", "report"}
    evidence = next(e for e in final["events"] if e["stage"] == "repair" and e["state"] == "finished")
    assert digest(Path(evidence["evidence"])) == evidence["sha256"]
    assert json.loads(Path(evidence["evidence"]).read_text()) == engine.result
    assert sender.list()[0]["result"]["revision"] == "r-final"
    assert not worker.run_once()


def test_blocked_repair_keeps_rejected_candidate_in_details(tmp_path, reports):
    outcome = {"status": "blocked", "revision": "r-rejected", "reason": "Candidate worsened DRC",
               "rejected_candidate": {"revision": "r-rejected", "findings": [{"type": "clearance"}]}}
    engine = FakeEngine(tmp_path, result=outcome)
    queue = JobQueue(tmp_path, engine=engine)
    request = repair_request()
    job = queue.submit(request, client_id="client", idempotency_key="repair-1")
    assert queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == "blocked"
    assert final["result"]["revision"] == "r-parent"
    assert final["result"]["blocking"] == outcome
    assert final["result"]["steps"] == {"repair": outcome}
    assert reports == []
    assert queue.submit(request, client_id="client", idempotency_key="repair-1") == final
    assert not queue.run_once() and len(engine.calls) == 1


@pytest.mark.parametrize("change", [{"repair_nets": ["OTHER"]}, {"repair_region": [0, 1, 9, 20]},
                                   {"repair_remove_ids": ["track-1"]}, {"passes": 4}])
def test_repair_idempotency_covers_scope(tmp_path, change):
    queue = JobQueue(tmp_path, engine=FakeEngine(tmp_path))
    job = queue.submit(repair_request(), client_id="client", idempotency_key="repair-1")
    replay = queue.submit(repair_request(repair_remove_ids=[]), client_id="client", idempotency_key="repair-1")
    assert replay == job
    with pytest.raises(IdempotencyConflict):
        queue.submit(repair_request(**change), client_id="client", idempotency_key="repair-1")
    assert len(queue.list()) == 1


def test_existing_request_hash_and_runtime_survive_schema_extension(tmp_path):
    engine = FakeEngine(tmp_path)
    queue = JobQueue(tmp_path, engine=engine)
    legacy = {"project": "repair-test", "operation": "route", "revision": "r-parent",
              "board_path": None, "constraints": None, "candidate_count": 3, "passes": 10,
              "route": True, "release": False, "plan_id": None, "candidate_id": None}
    job = queue.submit(legacy, client_id="client", idempotency_key="old-route")
    assert job["request"] == legacy
    with queue.store.connect() as db:
        runtime = json.loads(db.execute("SELECT payload FROM job_runtime WHERE job=?", (job["id"],)).fetchone()[0])
        submission_hash = db.execute("SELECT request_sha256 FROM job_idempotency WHERE job=?", (job["id"],)).fetchone()[0]
    assert runtime["request_sha256"] == _checksum(legacy)
    assert submission_hash == _checksum({"request": legacy, "config_sha256": runtime["config_sha256"]})
    explicit = {**legacy, "repair_nets": None, "repair_region": None, "repair_remove_ids": []}
    assert queue.submit(explicit, client_id="client", idempotency_key="old-route") == job
    assert queue.run_once()
    assert queue.get(job["id"])["result"]["blocking"]["reason"] == "Legacy runtime fixture"
    assert len(engine.calls) == 1


def test_repair_worker_uses_submission_runtime(tmp_path, monkeypatch, reports):
    sender = JobQueue(tmp_path, engine=FakeEngine(tmp_path, {"fanout": True}))
    job = sender.submit(repair_request())
    other = FakeEngine(tmp_path, {"fanout": False})
    selected = []

    def create_engine(root, config):
        engine = FakeEngine(root, config)
        selected.append(engine)
        return engine

    monkeypatch.setattr(jobs, "EngineeringService", create_engine)
    assert JobQueue(tmp_path, engine=other).run_once()
    assert sender.get(job["id"])["status"] == "completed"
    assert len(selected) == 1 and selected[0].toolchain.fanout is True
    assert len(selected[0].calls) == 1 and other.calls == []


def test_cancellation_after_repair_retains_child_without_report(tmp_path, monkeypatch, reports):
    engine = FakeEngine(tmp_path)
    queue = JobQueue(tmp_path, engine=engine)
    job = queue.submit(repair_request())
    original = engine.repair_revision

    def cancel_after_repair(*args, **kwargs):
        result = original(*args, **kwargs)
        queue.cancel(job["id"])
        return result

    monkeypatch.setattr(engine, "repair_revision", cancel_after_repair)
    assert queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == "cancelled" and final["result"]["revision"] == "r-final"
    assert reports == []


@pytest.mark.asyncio
async def test_repair_tool_definitions_and_annotations():
    definitions = {tool.name: tool for tool in await server.mcp.list_tools()}
    diagnose = definitions["diagnose_pcb_repair"]
    submit = definitions["submit_pcb_repair"]
    assert diagnose.annotations.readOnlyHint is True
    assert submit.annotations.readOnlyHint is False
    for tool in (diagnose, submit):
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
    assert set(diagnose.inputSchema["required"]) == {"project", "revision"}
    assert set(submit.inputSchema["required"]) == {"project", "revision", "nets", "region"}
    assert submit.inputSchema["properties"]["passes"]["default"] == 3
    assert submit.inputSchema["properties"]["remove_ids"]["default"] is None
    automatic = definitions["submit_pcb_auto_repair"]
    assert set(automatic.inputSchema["required"]) == {"project", "revision"}
    assert automatic.annotations.readOnlyHint is False
    assert automatic.annotations.destructiveHint is False
    reference = definitions["submit_pcb_reference_repair"]
    assert set(reference.inputSchema["required"]) == {"project","revision","reference_project","reference_revision"}
    assert reference.annotations.readOnlyHint is False and reference.annotations.destructiveHint is False
    assert not any("repair" in name and name not in {"diagnose_pcb_repair", "submit_pcb_repair", "submit_pcb_clearance_repair", "submit_pcb_auto_repair", "submit_pcb_reference_repair"} for name in definitions)


@pytest.mark.parametrize("remove_ids", [None, ["track-1"]])
def test_mcp_submit_queues_without_executing(tmp_path, monkeypatch, remove_ids):
    engine = FakeEngine(tmp_path)
    queue = JobQueue(tmp_path, engine=engine)
    monkeypatch.setattr(server, "job_queue", lambda: queue)
    job = server.submit_pcb_repair("repair-test", "r-parent", ["GND"], [0, 0, 10, 10], remove_ids)
    assert job["status"] == "queued" and queue.thread is None and engine.calls == []
    assert job["request"]["operation"] == "repair"
    assert job["request"]["repair_remove_ids"] == (remove_ids or [])
    assert job["request"]["passes"] == 3 and job["request"]["release"] is False
    assert JobQueue(tmp_path, engine=engine).get(job["id"]) == job
    with pytest.raises(ValidationError):
        server.submit_pcb_repair("repair-test", "r-parent", [], [0, 0, 10, 10])
    assert len(queue.list()) == 1


def test_mcp_diagnosis_only_reads_service_record(monkeypatch):
    recorded = {"findings": [{"type": "clearance"}], "proposals": [{"nets": ["GND"]}]}
    calls = []

    class DiagnosisEngine:
        def diagnose_repair(self, project, revision):
            calls.append((project, revision))
            return recorded

    monkeypatch.setattr(server, "service", lambda: DiagnosisEngine())
    monkeypatch.setattr(server, "job_queue", lambda: pytest.fail("Diagnosis must not create a queue"))
    assert server.diagnose_pcb_repair("repair-test", "r-parent") is recorded
    assert calls == [("repair-test", "r-parent")]
