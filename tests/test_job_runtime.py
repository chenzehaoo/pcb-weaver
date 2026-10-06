"""Submission-bound execution profiles; every queue here uses a temporary store."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from pcb_weaver.jobs import JobQueue, _checksum
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import canonical, digest
from test_jobs import EXAMPLE


def target(queue):
    revision = queue.engine.import_project("runtime-test", str(EXAMPLE / "two-layer.kicad_pcb"))["revision"]["id"]
    return {"project": "runtime-test", "operation": "route", "revision": revision}


def runtime_row(queue, job):
    with queue.store.connect() as db:
        raw, checksum = db.execute("SELECT payload,sha256 FROM job_runtime WHERE job=?", (job,)).fetchone()
    return json.loads(raw), checksum


def observe_routes(monkeypatch):
    calls = []
    def route(engine, *args):
        calls.append({"engine": engine, "config": deepcopy(engine.toolchain.config),
                      "fanout": engine.toolchain.fanout, "neckdown": engine.toolchain.controlled_neckdown,
                      "timeout": engine.toolchain.route_timeout})
        return {"status": "blocked", "reason": "Profile selection unit fixture, not EDA acceptance"}
    monkeypatch.setattr(EngineeringService, "route_revision", route)
    return calls


@pytest.mark.parametrize("reverse", [False, True])
def test_other_worker_uses_submitted_profile_without_silent_downgrade(tmp_path, monkeypatch, reverse):
    old, new = tmp_path / "old.jar", tmp_path / "new.jar"
    old.write_bytes(b"old profile fixture")
    new.write_bytes(b"new profile fixture")
    profiles = [{"freerouting_jar": str(old), "fanout": True, "controlled_neckdown": False,
                 "timeout_seconds": 300, "route_timeout_seconds": 900},
                {"freerouting_jar": str(new), "fanout": False, "controlled_neckdown": True,
                 "timeout_seconds": 180, "route_timeout_seconds": 240}]
    submitted, other = profiles[:: -1] if reverse else profiles
    sender = JobQueue(tmp_path / "managed", submitted)
    worker = JobQueue(tmp_path / "managed", other)
    calls = observe_routes(monkeypatch)
    job = sender.submit(target(sender))
    snapshot, checksum = runtime_row(sender, job["id"])
    assert snapshot["config"] == sender.engine.toolchain.config
    assert snapshot["submitted_config_sha256"] == _checksum(snapshot["config"])
    assert snapshot["config_sha256"] == _checksum(snapshot["execution_config"])
    assert snapshot["jar"]["sha256"] == digest(Path(submitted["freerouting_jar"]))
    assert worker.run_once()
    assert len(calls) == 1 and calls[0]["engine"] is not worker.engine
    assert calls[0]["config"] == snapshot["execution_config"]
    assert calls[0]["fanout"] is submitted["fanout"]
    assert calls[0]["neckdown"] is submitted["controlled_neckdown"]
    assert calls[0]["timeout"] == submitted["route_timeout_seconds"]
    assert all(worker.engine.toolchain.config[key] == value for key, value in other.items())
    final = sender.get(job["id"])
    assert final["result"]["runtime"] == {**snapshot, "sha256": checksum}
    history = sender.store.history("runtime-test")["events"]
    assert any(e["kind"] == "job_runtime_selected" and e["payload"]["runtime_sha256"] == checksum
               and e["payload"]["config_sha256"] == snapshot["config_sha256"] for e in history)
    assert any(e["kind"] == "job_stage_completed" and e["payload"]["config_sha256"] == snapshot["config_sha256"] for e in history)
    assert runtime_row(sender, job["id"]) == (snapshot, checksum)


def test_matching_config_preserves_injected_engine_and_methods(tmp_path, monkeypatch):
    sender = JobQueue(tmp_path)
    injected = EngineeringService(tmp_path, sender.engine.toolchain.config)
    calls = []
    monkeypatch.setattr(injected, "route_revision", lambda *a: calls.append(a) or {"status": "blocked"})
    worker = JobQueue(tmp_path, engine=injected)
    job = sender.submit(target(sender))
    worker.run_once()
    assert len(calls) == 1 and sender.get(job["id"])["status"] == "blocked"
    selected = [e["payload"] for e in worker.store.history("runtime-test")["events"] if e["kind"] == "job_runtime_selected"]
    assert selected[-1]["reused_worker_engine"] is True


def test_mutating_sender_config_after_submit_does_not_change_job(tmp_path, monkeypatch):
    config = {"fanout": True, "controlled_neckdown": False}
    sender = JobQueue(tmp_path, config)
    calls = observe_routes(monkeypatch)
    job = sender.submit(target(sender))
    config["fanout"] = False
    sender.engine.toolchain.config["fanout"] = False
    sender.run_once()
    assert calls[0]["config"]["fanout"] is True
    assert sender.get(job["id"])["result"]["runtime"]["config"]["fanout"] is True


@pytest.mark.parametrize("sql", ["UPDATE job_runtime SET sha256='changed' WHERE job=?", "DELETE FROM job_runtime WHERE job=?"])
def test_runtime_rows_are_immutable(tmp_path, sql):
    queue = JobQueue(tmp_path)
    job = queue.submit(target(queue))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with queue.store.connect() as db:
            db.execute(sql, (job["id"],))


@pytest.mark.parametrize("corruption", ["missing", "hash", "json", "canonical", "event"])
def test_missing_or_corrupt_runtime_blocks_before_any_operation(tmp_path, monkeypatch, corruption):
    queue = JobQueue(tmp_path)
    calls = observe_routes(monkeypatch)
    job = queue.submit(target(queue))
    with queue.store.connect() as db:
        # Deliberately bypass SQL immutability to model damaged/legacy databases.
        db.execute("DROP TRIGGER job_runtime_no_update")
        db.execute("DROP TRIGGER job_runtime_no_delete")
        if corruption == "missing":
            db.execute("DELETE FROM job_runtime WHERE job=?", (job["id"],))
        elif corruption == "hash":
            db.execute("UPDATE job_runtime SET sha256='bad' WHERE job=?", (job["id"],))
        elif corruption == "json":
            db.execute("UPDATE job_runtime SET payload='not json' WHERE job=?", (job["id"],))
        elif corruption == "canonical":
            db.execute("UPDATE job_runtime SET payload=payload || ' ' WHERE job=?", (job["id"],))
        else:
            db.execute("DELETE FROM job_events WHERE job=?", (job["id"],))
    assert queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == "blocked"
    assert "resubmit" in final["result"]["blocking"]["reason"]
    assert not final["result"]["steps"] and not calls


@pytest.mark.parametrize("change", [{"controlled_neckdown": "false"}, {"fanout": 1}, {"route_timeout_seconds": 0},
                                    {"unsupported_backend": "auto"}])
def test_even_rehashed_runtime_requires_current_validated_config(tmp_path, monkeypatch, change):
    queue = JobQueue(tmp_path)
    calls = observe_routes(monkeypatch)
    job = queue.submit(target(queue))
    runtime, _ = runtime_row(queue, job["id"])
    runtime["config"].update(change)
    runtime["submitted_config_sha256"] = _checksum(runtime["config"])
    runtime["execution_config"].update(change)
    runtime["config_sha256"] = _checksum(runtime["execution_config"])
    with queue.store.connect() as db:
        db.execute("DROP TRIGGER job_runtime_no_update")
        db.execute("UPDATE job_runtime SET payload=?,sha256=? WHERE job=?", (canonical(runtime), _checksum(runtime), job["id"]))
    queue.run_once()
    assert queue.get(job["id"])["status"] == "blocked" and not calls


def test_runtime_cannot_be_replayed_into_another_job(tmp_path, monkeypatch):
    queue = JobQueue(tmp_path)
    request = target(queue)
    first, second = queue.submit(request), queue.submit(request)
    runtime, checksum = runtime_row(queue, first["id"])
    queue.cancel(first["id"])
    calls = observe_routes(monkeypatch)
    with queue.store.connect() as db:
        db.execute("DROP TRIGGER job_runtime_no_update")
        db.execute("UPDATE job_runtime SET payload=?,sha256=? WHERE job=?", (canonical(runtime), checksum, second["id"]))
    queue.run_once()
    final = queue.get(second["id"])
    assert final["status"] == "blocked" and "different job" in final["result"]["blocking"]["reason"]
    assert not calls


@pytest.mark.parametrize("mutation", ["replace", "remove"])
def test_queued_jar_change_blocks_before_any_operation(tmp_path, monkeypatch, mutation):
    jar = tmp_path / "router.jar"
    jar.write_bytes(b"submitted jar bytes")
    queue = JobQueue(tmp_path / "managed", {"freerouting_jar": str(jar)})
    job = queue.submit(target(queue))
    if mutation == "replace":
        jar.write_bytes(b"different jar")
    else:
        jar.unlink()
    calls = observe_routes(monkeypatch)
    queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == "blocked" and "JAR bytes" in final["result"]["blocking"]["reason"]
    assert final["result"]["runtime"]["jar"]["status"] == "host_file_hashed"
    assert not calls and not final["result"]["steps"]


def test_relative_jar_remains_bound_to_submission_directory(tmp_path, monkeypatch):
    sender_dir, worker_dir = tmp_path / "sender", tmp_path / "worker"
    sender_dir.mkdir()
    worker_dir.mkdir()
    (sender_dir / "router.jar").write_bytes(b"submitted")
    (worker_dir / "router.jar").write_bytes(b"other")
    monkeypatch.chdir(sender_dir)
    queue = JobQueue(tmp_path / "managed", {"freerouting_jar": "router.jar"})
    job = queue.submit(target(queue))
    monkeypatch.chdir(worker_dir)
    calls = observe_routes(monkeypatch)
    queue.run_once()
    assert calls[0]["config"]["freerouting_jar"] == str(sender_dir / "router.jar")
    assert calls[0]["engine"] is not queue.engine
    runtime = queue.get(job["id"])["result"]["runtime"]
    assert runtime["config"]["freerouting_jar"] == "router.jar"


def test_equivalent_explicit_defaults_reuse_existing_monkeypatched_engine(tmp_path, monkeypatch):
    from pcb_weaver.models import ToolchainConfig
    implicit = JobQueue(tmp_path, {"timeout_seconds": 180})
    explicit = JobQueue(tmp_path, ToolchainConfig().model_dump(exclude_none=True))
    calls = []
    monkeypatch.setattr(implicit.engine, "route_revision", lambda *a: calls.append(a) or {"status": "blocked"})
    job = explicit.submit(target(explicit))
    implicit.run_once()
    assert len(calls) == 1 and implicit.get(job["id"])["status"] == "blocked"
    assert implicit.engine.toolchain.timeout == explicit.engine.toolchain.timeout


def test_different_actual_defaults_do_not_reuse_wrong_engine(tmp_path, monkeypatch):
    from pcb_weaver.models import ToolchainConfig
    worker = JobQueue(tmp_path, {})
    sender = JobQueue(tmp_path, ToolchainConfig().model_dump(exclude_none=True))
    calls = observe_routes(monkeypatch)
    monkeypatch.setattr(worker.engine, "route_revision", lambda *a: pytest.fail("Reused 300s worker for 180s submitted runtime"))
    job = sender.submit(target(sender))
    worker.run_once()
    assert len(calls) == 1 and calls[0]["engine"] is not worker.engine
    assert calls[0]["config"]["timeout_seconds"] == 180
    assert worker.engine.toolchain.timeout == 300
    assert sender.get(job["id"])["result"]["runtime"]["config"]["timeout_seconds"] == 180


def test_raw_configs_differ_but_effective_values_match(tmp_path, monkeypatch):
    # Direct injected services retain Toolchain defaults; an explicit equivalent must reuse them.
    injected = EngineeringService(tmp_path)
    worker = JobQueue(tmp_path, engine=injected)
    sender = JobQueue(tmp_path, {"timeout_seconds": 300, "route_timeout_seconds": 1800})
    calls = []
    monkeypatch.setattr(injected, "route_revision", lambda *a: calls.append(a) or {"status": "blocked"})
    job = sender.submit(target(sender))
    worker.run_once()
    assert injected.toolchain.config == {}
    assert len(calls) == 1 and sender.get(job["id"])["status"] == "blocked"


def test_wsl_jar_is_explicitly_unhashed_without_host_probe(tmp_path, monkeypatch):
    queue = JobQueue(tmp_path, {"wsl_distro": "Ubuntu", "freerouting_jar": "/opt/router.jar"})
    request = target(queue)
    real_is_file = Path.is_file
    def local_only(path):
        assert "/opt/router.jar" not in path.as_posix()
        return real_is_file(path)
    monkeypatch.setattr(Path, "is_file", local_only)
    job = queue.submit(request)
    runtime, _ = runtime_row(queue, job["id"])
    assert runtime["jar"] == {"status": "not_host_hashed", "path": "/opt/router.jar"}
