"""Transactional submission tests; all databases and inputs are temporary."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from pcb_weaver import jobs
from pcb_weaver.jobs import IdempotencyConflict, JobQueue, JobRequest
from pcb_weaver.storage import canonical


CLIENT = hashlib.sha256(b"test-only-client-token").hexdigest()
KEY = "integration:request-001"
TABLES = ("jobs", "job_inputs", "job_runtime", "job_events", "job_idempotency")


@pytest.fixture
def submission(tmp_path):
    source = tmp_path / "input" / "board.kicad_pcb"
    source.parent.mkdir()
    source.write_text('(kicad_pcb (version 20240108))', encoding="utf-8")
    return {"project": "enterprise", "board_path": str(source)}


def submit(queue, request, **overrides):
    return queue.submit(request, **{"idempotency_key": KEY, "client_id": CLIENT, **overrides})


def counts(queue):
    with queue.store.connect() as db:
        return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}


def assert_one_submission(queue, identifier):
    assert counts(queue) == dict.fromkeys(TABLES, 1)
    with queue.store.connect() as db:
        raw, = db.execute("SELECT payload FROM job_runtime WHERE job=?", (identifier,)).fetchone()
        request, = db.execute("SELECT request FROM jobs WHERE id=?", (identifier,)).fetchone()
        row = db.execute("SELECT client_id,key,request_sha256,job FROM job_idempotency").fetchone()
    runtime = json.loads(raw)
    expected = hashlib.sha256(canonical({"request": json.loads(request),
                                        "config_sha256": runtime["config_sha256"]}).encode()).hexdigest()
    assert row == (CLIENT, KEY, expected, identifier)
    assert "test-only-client-token" not in canonical(row)


def test_same_payload_normalizes_defaults_order_and_model_across_queues(tmp_path, submission):
    queue = JobQueue(tmp_path / "store")
    first = submit(queue, submission)
    other = JobQueue(tmp_path / "store")
    explicit = JobRequest.model_validate(submission).model_dump(mode="json")
    assert submit(other, dict(reversed(list(explicit.items())))) == first
    assert submit(other, JobRequest.model_validate(submission)) == first
    assert_one_submission(other, first["id"])
    assert queue.thread is None and other.thread is None


def test_equivalent_board_path_normalizes_before_digest(tmp_path, submission, monkeypatch):
    queue = JobQueue(tmp_path / "store")
    first = submit(queue, submission)
    monkeypatch.chdir(tmp_path)
    assert submit(queue, {**submission, "board_path": "input/./board.kicad_pcb"})["id"] == first["id"]
    assert_one_submission(queue, first["id"])


@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed", "blocked", "cancelled", "interrupted"])
def test_replays_keep_original_status_result_and_events(tmp_path, submission, status):
    queue = JobQueue(tmp_path / "store")
    first = submit(queue, submission)
    with queue.store.connect() as db:
        db.execute("UPDATE jobs SET status=?,stage=?,result=? WHERE id=?",
                   (status, status, canonical({"test_status": status}), first["id"]))
    before = queue.get(first["id"])
    assert submit(queue, submission) == before
    assert_one_submission(queue, first["id"])


@pytest.mark.parametrize("change", [{"passes": 11}, {"route": False}, {"project": "other"}, {"release": True}])
def test_different_payload_conflicts_without_mutation(tmp_path, submission, change):
    queue = JobQueue(tmp_path / "store")
    first = submit(queue, submission)
    with pytest.raises(IdempotencyConflict, match="different request"):
        submit(queue, {**submission, **change})
    assert queue.get(first["id"]) == first
    assert_one_submission(queue, first["id"])
    assert issubclass(IdempotencyConflict, ValueError)


@pytest.mark.parametrize("config", [{"fanout": True}, {"timeout_seconds": 180},
                                    {"route_timeout_seconds": 900}, {"java": "another-java"}])
def test_changed_effective_toolchain_conflicts(tmp_path, submission, config):
    first_queue = JobQueue(tmp_path / "store", {})
    first = submit(first_queue, submission)
    changed = JobQueue(tmp_path / "store", config)
    with pytest.raises(IdempotencyConflict, match="effective toolchain"):
        submit(changed, submission)
    assert_one_submission(first_queue, first["id"])


def test_equivalent_effective_config_reuses_job(tmp_path, submission):
    queue = JobQueue(tmp_path / "store", {})
    first = submit(queue, submission)
    explicit = JobQueue(tmp_path / "store", {"timeout_seconds": 300, "route_timeout_seconds": 1800})
    assert submit(explicit, submission)["id"] == first["id"]
    assert_one_submission(queue, first["id"])


def test_keys_are_scoped_by_client_and_case_sensitive(tmp_path, submission):
    queue = JobQueue(tmp_path / "store")
    identifiers = {submit(queue, submission)["id"],
                   submit(queue, submission, client_id="other-client")["id"],
                   submit(queue, submission, idempotency_key=KEY.upper())["id"]}
    assert len(identifiers) == 3
    assert counts(queue) == dict.fromkeys(TABLES, 3)


@pytest.mark.parametrize("field", ["idempotency_key", "client_id"])
@pytest.mark.parametrize("value", ["", "a" * 129, "has space", "a\n", "\u00e9", "\uff21", "x/y", "x\\y", "x\x00", 1, True, b"abc", None])
def test_invalid_identifiers_rejected_before_input_validation(tmp_path, submission, monkeypatch, field, value):
    queue = JobQueue(tmp_path / "store")
    monkeypatch.setattr(jobs, "design_files", lambda *_: pytest.fail("Input validation must follow key validation"))
    with pytest.raises(ValueError):
        submit(queue, submission, **{field: value})
    assert counts(queue) == dict.fromkeys(TABLES, 0)


@pytest.mark.parametrize("value", ["a", ".", "AZaz09._:-", "x" * 128])
def test_valid_identifier_boundaries(tmp_path, submission, value):
    queue = JobQueue(tmp_path / "store")
    assert submit(queue, submission, client_id=value, idempotency_key=value)["status"] == "queued"


def test_without_idempotency_retains_duplicate_submission_behavior(tmp_path, submission):
    queue = JobQueue(tmp_path / "store")
    first = queue.submit(submission)
    second = queue.submit(submission, idempotency_key=None, client_id=None)
    assert first["id"] != second["id"]
    assert counts(queue) == {**dict.fromkeys(TABLES, 2), "job_idempotency": 0}


def test_cross_queue_thread_concurrency_creates_exactly_one_job(tmp_path, submission):
    queues = [JobQueue(tmp_path / "store") for _ in range(8)]
    barrier = threading.Barrier(len(queues))

    def send(queue):
        barrier.wait(timeout=20)
        return submit(queue, submission)["id"]

    with ThreadPoolExecutor(max_workers=len(queues)) as pool:
        identifiers = list(pool.map(send, queues))
    assert len(set(identifiers)) == 1
    assert_one_submission(queues[0], identifiers[0])


def _process_submit(root, request, barrier):
    queue = JobQueue(Path(root))
    barrier.wait(timeout=30)
    return submit(queue, request)["id"]


def test_spawned_processes_share_transactional_idempotency(tmp_path, submission):
    queue = JobQueue(tmp_path / "store")
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        barrier = manager.Barrier(4)
        with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
            futures = [pool.submit(_process_submit, str(queue.store.root), submission, barrier) for _ in range(4)]
            identifiers = [future.result(timeout=60) for future in futures]
    assert len(set(identifiers)) == 1
    assert_one_submission(queue, identifiers[0])


def test_concurrent_different_payload_has_one_winner_one_conflict(tmp_path, submission):
    queues = [JobQueue(tmp_path / "store") for _ in range(2)]
    barrier = threading.Barrier(2)

    def send(index):
        barrier.wait(timeout=20)
        try:
            return submit(queues[index], {**submission, "passes": index + 1})["id"]
        except IdempotencyConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert results.count("conflict") == 1
    assert_one_submission(queues[0], next(value for value in results if value != "conflict"))


@pytest.mark.parametrize("table", TABLES)
def test_sql_abort_at_each_insert_rolls_back_every_record(tmp_path, submission, table):
    queue = JobQueue(tmp_path / "store")
    with queue.store.connect() as db:
        db.execute(f"CREATE TRIGGER fail_insert AFTER INSERT ON {table} "
                   "BEGIN SELECT RAISE(ABORT, 'injected submission failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected submission failure"):
        submit(queue, submission)
    assert counts(queue) == dict.fromkeys(TABLES, 0)
    assert not queue.wake.is_set()
    with queue.store.connect() as db:
        db.execute("DROP TRIGGER fail_insert")
    first = submit(queue, submission)
    assert_one_submission(queue, first["id"])


class _AbortAfterFinalInsert:
    def __init__(self, db, abort):
        self.db, self.abort = db, abort

    def execute(self, sql, parameters=()):
        result = self.db.execute(sql, parameters)
        if sql.startswith("INSERT INTO job_idempotency"):
            self.abort()
        return result


def _interrupt():
    raise KeyboardInterrupt("injected cancellation")


def test_base_exception_after_final_insert_rolls_back(tmp_path, submission, monkeypatch):
    queue = JobQueue(tmp_path / "store")
    connect = queue.store.connect

    @contextmanager
    def interrupted_connect():
        with connect() as db:
            yield _AbortAfterFinalInsert(db, _interrupt)

    with monkeypatch.context() as patch:
        patch.setattr(queue.store, "connect", interrupted_connect)
        with pytest.raises(KeyboardInterrupt, match="injected cancellation"):
            submit(queue, submission)
    assert counts(queue) == dict.fromkeys(TABLES, 0)
    assert_one_submission(queue, submit(queue, submission)["id"])


def _crash_during_submission(root, request):
    queue = JobQueue(Path(root))
    connect = queue.store.connect

    @contextmanager
    def crash_connect():
        with connect() as db:
            yield _AbortAfterFinalInsert(db, lambda: os._exit(23))

    queue.store.connect = crash_connect
    submit(queue, request)


def test_process_death_before_commit_leaves_no_partial_submission(tmp_path, submission):
    queue = JobQueue(tmp_path / "store")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_during_submission, args=(str(queue.store.root), submission))
    process.start()
    try:
        process.join(timeout=40)
        assert not process.is_alive(), "Crash fixture did not reach the final insert"
        assert process.exitcode == 23
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        process.close()
    assert counts(queue) == dict.fromkeys(TABLES, 0)
    assert_one_submission(queue, submit(queue, submission)["id"])


def test_lost_response_after_commit_replays_original_job(tmp_path, submission, monkeypatch):
    queue = JobQueue(tmp_path / "store")

    def fail_response(_):
        raise ConnectionError("injected lost response")

    with monkeypatch.context() as patch:
        patch.setattr(queue, "get", fail_response)
        with pytest.raises(ConnectionError, match="lost response"):
            submit(queue, submission)
    original_id = queue.list()[0]["id"]
    assert submit(queue, submission)["id"] == original_id
    assert_one_submission(queue, original_id)


def test_input_and_runtime_capture_do_not_hold_write_lock(tmp_path, submission, monkeypatch):
    queue = JobQueue(tmp_path / "store")
    inspected = []

    def unlocked(function, name):
        def check(*args):
            db = sqlite3.connect(queue.store.root / "ledger.sqlite3", timeout=0)
            try:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
            finally:
                db.close()
            inspected.append(name)
            return function(*args)
        return check

    for name in ("design_files", "_execution_config", "_jar_snapshot"):
        monkeypatch.setattr(jobs, name, unlocked(getattr(jobs, name), name))
    assert_one_submission(queue, submit(queue, submission)["id"])
    assert inspected == ["design_files", "_execution_config", "_jar_snapshot"]
