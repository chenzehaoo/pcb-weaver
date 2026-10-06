import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading

import pytest

from pcb_weaver.jobs import JobQueue, JobRequest


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "routing-demo"


def request(**overrides):
    return {"project": "workbench", "operation": "pipeline", "board_path": str(EXAMPLE / "two-layer.kicad_pcb"),
            "constraints": json.loads((EXAMPLE / "constraints.json").read_text()), "route": False, **overrides}


def assert_list_summary(queue, identifier):
    detail = queue.get(identifier)
    listed = next(row for row in queue.list(detail["project"]) if row["id"] == identifier)
    assert listed["summary"] is True
    assert "summary" not in detail
    for key in ("id", "project", "status", "stage", "cancel_requested", "request", "updated"):
        assert listed[key] == detail[key]
    if detail["result"] is None:
        assert listed["result"] is None
    else:
        for key in ("revision", "project", "error"):
            value = detail["result"].get(key)
            if isinstance(value, str):
                assert listed["result"][key] == value[:1024 if key == "error" else 256]
        assert "steps" not in listed["result"]
        assert len(json.dumps(listed["result"]).encode("utf-8")) < 40000
    return listed


def test_persisted_pipeline_generates_real_revision_and_report(tmp_path):
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    assert job["status"] == "queued"
    recovered = JobQueue(tmp_path)
    assert recovered.run_once()
    final = recovered.get(job["id"])
    assert final["status"] == "completed"
    assert set(final["result"]["steps"]) == {"import", "plan", "apply", "report"}
    assert final["result"]["steps"]["report"]["verification_status"] == "not_verified"
    assert Path(final["result"]["steps"]["report"]["path"]).is_file()
    assert_list_summary(recovered, job["id"])
    assert not recovered.run_once()


def test_queued_cancel_never_runs(tmp_path):
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    assert queue.cancel(job["id"])["status"] == "cancelled"
    assert_list_summary(queue, job["id"])
    assert not queue.run_once()
    assert not queue.store.list_revisions("workbench")


def test_checkpoint_reads_only_control_state(tmp_path,monkeypatch):
    from pcb_weaver.jobs import JobCancelled,JobInterrupted
    queue = JobQueue(tmp_path)
    identifier = queue.submit(request())["id"]
    statements = []
    original_connect = queue.store.connect
    @contextmanager
    def traced():
        with original_connect() as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(queue.store,"connect",traced)
    monkeypatch.setattr(queue,"get",lambda *args:pytest.fail("Checkpoint decoded full job evidence"))
    queue._checkpoint(identifier)
    assert any("SELECT cancel_requested FROM jobs" in sql for sql in statements)
    assert not any("job_events" in sql or "result" in sql for sql in statements)
    queue.stop_event.set()
    with pytest.raises(JobInterrupted):
        queue._checkpoint(identifier)
    queue.stop_event.clear()
    with original_connect() as db:
        db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?",(identifier,))
    with pytest.raises(JobCancelled):
        queue._checkpoint(identifier)
    with pytest.raises(ValueError,match="Unknown job"):
        queue._checkpoint("job-unknown")


@pytest.mark.parametrize("algorithm,chosen", [("legalize", "near"), ("block_coordinate", "short")])
def test_pipeline_selects_the_declared_placement_objective(tmp_path, monkeypatch, algorithm, chosen):
    queue = JobQueue(tmp_path)
    candidates = [{"id": "short", "feasible": True, "metrics": {"weighted_hpwl_mm": 10, "squared_displacement_mm2": 80}},
                  {"id": "near", "feasible": True, "metrics": {"weighted_hpwl_mm": 15, "squared_displacement_mm2": 2}}]
    monkeypatch.setattr(queue.engine, "plan_layout", lambda *args: {
        "status": "ok", "algorithm": algorithm, "plan_id": "p-test", "candidates": candidates})
    selected = []
    def apply(project, revision, plan, candidate):
        selected.append(candidate)
        return {"status": "blocked", "reason": "Selection-only test"}
    monkeypatch.setattr(queue.engine, "apply_layout", apply)
    job = queue.submit(request())
    queue.run_once()
    assert queue.get(job["id"])["status"] == "blocked"
    assert selected == [chosen]


def test_running_cancel_stops_at_boundary_without_erasing_child(tmp_path, monkeypatch):
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    original = queue.engine.plan_layout
    def cancel_after_plan(*args):
        result = original(*args)
        queue.cancel(job["id"])
        return result
    monkeypatch.setattr(queue.engine, "plan_layout", cancel_after_plan)
    queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == "cancelled"
    assert "plan" in final["result"]["steps"] and "apply" not in final["result"]["steps"]
    assert len(queue.store.list_revisions("workbench")) == 1
    assert_list_summary(queue, job["id"])


def test_blocked_stage_is_not_a_success(tmp_path, monkeypatch):
    queue = JobQueue(tmp_path)
    monkeypatch.setattr(queue.engine, "route_revision", lambda *args: {"status": "blocked", "reason": "Unavailable native engine"})
    job = queue.submit(request(route=True))
    queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == "blocked"
    assert "verify" not in final["result"]["steps"]
    assert final["result"]["revision"]
    assert assert_list_summary(queue, job["id"])["result"]["blocking"]["reason"] == "Unavailable native engine"


def test_exception_is_recorded_and_remaining_jobs_continue(tmp_path, monkeypatch):
    queue = JobQueue(tmp_path)
    monkeypatch.setattr(queue.engine, "plan_layout", lambda *args: (_ for _ in ()).throw(ValueError("bad geometry")))
    job = queue.submit(request())
    queue.run_once()
    assert queue.get(job["id"])["status"] == "failed"
    assert "bad geometry" in queue.get(job["id"])["result"]["error"]
    assert_list_summary(queue, job["id"])


@pytest.mark.parametrize("changes", [dict(project="../escape"), dict(candidate_count=0), dict(passes=101),
    dict(revision="r-x"), dict(operation="apply"), dict(operation="shell"), dict(route="x")])
def test_job_requests_are_bounded(changes):
    with pytest.raises(ValueError):
        JobRequest.model_validate(request(**changes))


def test_saved_pipeline_cannot_silently_replace_constraints():
    with pytest.raises(ValueError, match="cannot silently"):
        JobRequest(project="demo", revision="r-one", constraints={})


def test_atomic_claim_only_runs_once(tmp_path, monkeypatch):
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    gate, entered = threading.Event(), threading.Event()
    original = queue.engine.import_project
    def import_later(*args):
        entered.set()
        gate.wait(10)
        return original(*args)
    monkeypatch.setattr(queue.engine, "import_project", import_later)
    worker = threading.Thread(target=queue.run_once)
    worker.start()
    assert entered.wait(5)
    try:
        second = queue.submit(request(project="second"))
        assert not JobQueue(tmp_path).run_once()
        assert queue.get(second["id"])["status"] == "queued"
        assert queue.get(job["id"])["status"] == "running"
    finally:
        gate.set()
        worker.join(15)
    assert not worker.is_alive() and queue.get(job["id"])["status"] == "completed"


@pytest.mark.parametrize("status", ["unknown", "infeasible", "unavailable", "ok", None])
def test_verify_requires_explicit_passed(tmp_path, monkeypatch, status):
    queue = JobQueue(tmp_path)
    revision = queue.engine.import_project("workbench", request()["board_path"])["revision"]["id"]
    monkeypatch.setattr(queue.engine, "verify_revision", lambda *a: {"status": status})
    job = queue.submit({"project": "workbench", "operation": "verify", "revision": revision})
    queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == final["stage"] == "blocked"
    assert "report" not in final["result"]["steps"]


@pytest.mark.parametrize("result", [None, [], {"status": "ok", "score": float("nan")}])
def test_malformed_stage_result_finishes_failed(tmp_path, monkeypatch, result):
    queue = JobQueue(tmp_path)
    monkeypatch.setattr(queue.engine, "plan_layout", lambda *a: result)
    job = queue.submit(request())
    queue.run_once()
    assert queue.get(job["id"])["status"] == "failed"


def test_malformed_persisted_request_does_not_strand_running_job(tmp_path):
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    with queue.store.connect() as db:
        db.execute("UPDATE jobs SET request=? WHERE id=?", ('{"operation":"invalid"}', job["id"]))
    assert queue.run_once()
    assert queue.get(job["id"])["status"] == "failed"


def test_stage_result_is_durable_before_next_operation(tmp_path, monkeypatch):
    from pcb_weaver.storage import digest
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    with queue.store.connect() as db:
        db.executescript("""
            CREATE TABLE observed_progress (result TEXT, summary TEXT);
            CREATE TRIGGER observe_progress AFTER UPDATE OF result ON jobs BEGIN
              INSERT INTO observed_progress VALUES(NEW.result,NEW.result_summary);
            END;
        """)
    original = queue.engine.plan_layout
    def inspect_progress(*args):
        running = JobQueue(tmp_path).get(job["id"])
        assert running["status"] == "running"
        assert running["result"]["revision"] == args[1]
        listed = assert_list_summary(queue, job["id"])
        assert listed["stage"] == "plan"
        assert listed["result"]["runtime"]["config_sha256"] == running["result"]["runtime"]["config_sha256"]
        event = next(e for e in running["events"] if e.get("state") == "finished")
        assert digest(Path(event["evidence"])) == event["sha256"]
        ledger = queue.store.history("workbench")["events"]
        assert any(e["kind"] == "job_stage_completed" and e["payload"]["sha256"] == event["sha256"] for e in ledger)
        return original(*args)
    monkeypatch.setattr(queue.engine, "plan_layout", inspect_progress)
    queue.run_once()
    assert queue.get(job["id"])["status"] == "completed"
    with queue.store.connect() as db:
        observations = db.execute("SELECT result,summary FROM observed_progress").fetchall()
    assert len(observations) > 3
    for full, summary in observations:
        full, summary = json.loads(full), json.loads(summary)
        assert summary.get("revision") == full.get("revision")
        assert summary["project"] == full["project"]
        assert "steps" not in summary
        if "runtime" in full:
            assert summary["runtime"]["sha256"] == full["runtime"]["sha256"]


def test_shutdown_stops_at_boundary_and_keeps_revision(tmp_path, monkeypatch):
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    original = queue.engine.plan_layout
    def shutdown(*args):
        result = original(*args)
        queue.close()
        return result
    monkeypatch.setattr(queue.engine, "plan_layout", shutdown)
    queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == "interrupted"
    assert final["result"]["revision"]
    assert "apply" not in final["result"]["steps"]
    assert_list_summary(queue, job["id"])


def test_recovery_marks_abandoned_work_without_losing_progress(tmp_path):
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    with queue.store.connect() as db:
        db.execute("UPDATE jobs SET status='running',result=? WHERE id=?", ('{"revision":"r-saved","steps":{}}', job["id"]))
    assert not JobQueue(tmp_path).run_once()
    final = queue.get(job["id"])
    assert final["status"] == final["stage"] == "interrupted"
    assert final["result"]["revision"] == "r-saved"
    assert final["events"][-1]["state"] == "worker_lost"
    assert_list_summary(queue, job["id"])


def test_report_runs_once_and_last_stage_cancellation_wins(tmp_path, monkeypatch):
    from pcb_weaver import catalog
    queue = JobQueue(tmp_path)
    revision = queue.engine.import_project("workbench", request()["board_path"])["revision"]["id"]
    job = queue.submit({"project": "workbench", "operation": "report", "revision": revision})
    original = catalog.generate_report
    def cancel_after_report(*args):
        result = original(*args)
        queue.cancel(job["id"])
        return result
    monkeypatch.setattr(catalog, "generate_report", cancel_after_report)
    queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == final["stage"] == "cancelled"
    assert len([e for e in final["events"] if e["stage"] == "report" and e["state"] == "finished"]) == 1
    assert_list_summary(queue, job["id"])


def test_large_native_result_list_never_reads_full_column(tmp_path, monkeypatch):
    queue = JobQueue(tmp_path)
    native_output = "NATIVE-DEBUG:" + "x" * 500_000
    monkeypatch.setattr(queue.engine, "plan_layout", lambda *a: {
        "status": "blocked", "reason": "Transport fixture", "operations": [{"stdout": native_output}]})
    job = queue.submit(request())
    queue.run_once()
    detail = queue.get(job["id"])
    assert detail["result"]["steps"]["plan"]["operations"][0]["stdout"] == native_output
    original_connect = queue.store.connect

    @contextmanager
    def summary_reads_only():
        with original_connect() as db:
            # Fail at SQLite column access, even if Python were to discard the payload later.
            db.set_authorizer(lambda action, table, column, *rest:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "jobs" and column == "result"
                else sqlite3.SQLITE_OK)
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(queue.store, "connect", summary_reads_only)
        for _ in range(4):
            listed = queue.list("workbench")
            assert len(json.dumps(listed)) < 15000
            assert "NATIVE-DEBUG" not in json.dumps(listed)
            assert listed[0]["result"]["revision"] == detail["result"]["revision"]
            assert listed[0]["status"] == listed[0]["stage"] == "blocked"
    assert queue.get(job["id"]) == detail


@pytest.mark.parametrize("status", ["completed", "cancelled", "interrupted", "blocked", "failed"])
def test_finish_summary_is_bounded_and_committed_with_result(tmp_path, status):
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    result = {"project": "workbench", "revision": "r-final", "error": "e" * 500_000,
              "blocking": {"status": "blocked", "reason": "b" * 500_000, "operations": ["large"]},
              "steps": {"native": {"stdout": "x" * 500_000}}}
    with queue.store.connect() as db:
        db.executescript("""
            CREATE TABLE observed_results (status TEXT, result TEXT, summary TEXT);
            CREATE TRIGGER observe_result AFTER UPDATE OF result ON jobs BEGIN
              INSERT INTO observed_results VALUES(NEW.status,NEW.result,NEW.result_summary);
            END;
        """)
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    queue._finish(job["id"], status, result)
    listed = assert_list_summary(queue, job["id"])
    assert listed["status"] == listed["stage"] == status
    assert listed["result"]["error"] == "e" * 1024
    assert listed["result"]["blocking"] == {"status": "blocked", "reason": "b" * 1024}
    assert queue.get(job["id"])["result"] == result
    with queue.store.connect() as db:
        observed = db.execute("SELECT status,result,summary FROM observed_results").fetchall()
    assert len(observed) == 1
    assert observed[0][0] == status
    assert json.loads(observed[0][1]) == result
    assert json.loads(observed[0][2]) == listed["result"]


def test_finish_failure_rolls_back_result_summary_and_status(tmp_path):
    queue = JobQueue(tmp_path)
    job = queue.submit(request())
    with queue.store.connect() as db:
        db.executescript("""
            CREATE TRIGGER reject_finished_event BEFORE INSERT ON job_events
            WHEN NEW.stage='completed' BEGIN SELECT RAISE(ABORT, 'test rollback'); END;
        """)
        db.execute("UPDATE jobs SET status='running',stage='plan' WHERE id=?", (job["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        queue._finish(job["id"], "completed", {"revision": "r-never-committed"})
    assert queue.get(job["id"])["result"] is None
    listed = assert_list_summary(queue, job["id"])
    assert listed["status"] == "running" and listed["stage"] == "plan"


def test_legacy_summary_migration_backfills_only_missing_results(tmp_path, monkeypatch):
    from pcb_weaver import jobs
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        db.execute("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, created TEXT NOT NULL,
            updated TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
            request TEXT NOT NULL, result TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0)""")
        for identifier, raw in [("old", json.dumps({"revision": "r-old", "steps": {"stdout": "x" * 500_000}})),
                                ("invalid", "{broken"), ("queued", None)]:
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                       (identifier, "workbench", "old", "old", "queued", "queued", "{}", raw, 0))
    queue = JobQueue(tmp_path)
    listed = {job["id"]: job for job in queue.list()}
    assert listed["old"]["result"] == {"revision": "r-old"}
    assert "invalid JSON" in listed["invalid"]["result"]["error"]
    assert listed["queued"]["result"] is None
    assert len(queue.get("old")["result"]["steps"]["stdout"]) == 500_000
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "_result_summary", lambda *a: pytest.fail("Already summarized result was parsed again"))
        JobQueue(tmp_path)
    # An old worker may write without the new column; repair on the next startup, not every poll.
    with queue.store.connect() as db:
        db.execute("UPDATE jobs SET result=? WHERE id='queued'", ('{"revision":"r-old-worker"}',))
    assert next(row for row in queue.list() if row["id"] == "queued")["result"] is None
    restarted = JobQueue(tmp_path)
    assert next(row for row in restarted.list() if row["id"] == "queued")["result"] == {"revision": "r-old-worker"}


def test_queued_source_change_blocks_import(tmp_path):
    import shutil
    source = tmp_path / "source"
    shutil.copytree(EXAMPLE, source)
    queue = JobQueue(tmp_path / "workspace")
    job = queue.submit(request(board_path=str(source / "two-layer.kicad_pcb")))
    with (source / "two-layer.kicad_pcb").open("a") as handle:
        handle.write("\n")
    queue.run_once()
    assert queue.get(job["id"])["status"] == "blocked"
    assert not queue.store.list_revisions("workbench")


@pytest.mark.parametrize("path", ["//remote/share/design.kicad_pcb", "\\\\remote\\share\\board.kicad_pcb", "https://remote/board.kicad_pcb"])
def test_network_input_rejected_before_filesystem_access(tmp_path, monkeypatch, path):
    queue = JobQueue(tmp_path)
    monkeypatch.setattr(Path, "is_file", lambda *a: pytest.fail("Network path was accessed"))
    with pytest.raises(ValueError, match="local filesystem"):
        queue.submit(request(board_path=path))


def test_worker_lock_project_name_cannot_deadlock_itself():
    with pytest.raises(ValueError, match="reserved"):
        JobRequest.model_validate(request(project="Platform-Worker"))


def test_worker_can_restart_after_close(tmp_path):
    queue = JobQueue(tmp_path)
    queue.start()
    queue.close()
    assert not queue.thread.is_alive()
    queue.start()
    try:
        assert queue.thread.is_alive()
        assert not queue.stop_event.is_set()
    finally:
        queue.close()
    assert not queue.thread.is_alive()
