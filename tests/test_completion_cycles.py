"""Explicit repair-cycle scheduling; native operations are stubbed."""
import asyncio

import pytest

from pcb_weaver import completion as module
from pcb_weaver.jobs import JobRequest, JobQueue, JobCancelled, _request_payload
from pcb_weaver.models import AutoRepairOptions, CompletionOptions
from pcb_weaver.storage import read_json
from test_completion import setup
from test_repair import check


def options(cycles=3, **updates):
    return {"placement_mode":"preserve","repair_cycles":cycles,"time_budget_seconds":7200,
            "repair":{"allow_multinet":True,"max_attempts":24,"time_budget_seconds":1800},**updates}


@pytest.mark.parametrize("cycles", [1,2,3])
def test_preserve_cycle_values(cycles):
    assert CompletionOptions.model_validate(options(cycles)).repair_cycles == cycles


@pytest.mark.parametrize("cycles", [0,4,-1,True,1.5,"2"])
def test_strict_cycle_limits(cycles):
    with pytest.raises(ValueError):
        CompletionOptions.model_validate(options(cycles))


@pytest.mark.parametrize("cycles", [2,3])
def test_multiple_cycles_reject_optimize(cycles):
    with pytest.raises(ValueError,match="preserve"):
        CompletionOptions.model_validate(options(cycles,placement_mode="optimize"))


def test_defaults_and_auto_repair_bounds_unchanged():
    assert CompletionOptions().repair_cycles == 1
    assert AutoRepairOptions().max_attempts == 6
    assert AutoRepairOptions().time_budget_seconds == 600
    for value in ({"max_attempts":25},{"time_budget_seconds":1801}):
        with pytest.raises(ValueError):
            AutoRepairOptions(**value)


def test_legacy_hash_payload_and_explicit_cycles():
    legacy = _request_payload(JobRequest(project="p",revision="r",operation="complete"))
    explicit = _request_payload(JobRequest(project="p",revision="r",operation="complete",
        completion_options={"repair_cycles":1}))
    assert legacy == explicit and "repair_cycles" not in legacy["completion_options"]
    assert _request_payload(JobRequest.model_validate(legacy)) == legacy
    enabled = _request_payload(JobRequest(project="p",revision="r",operation="complete",
        completion_options=options()))
    assert enabled["completion_options"]["repair_cycles"] == 3
    assert _request_payload(JobRequest.model_validate(enabled)) == enabled


def install_repair(setup,monkeypatch,specs,*,invalid_at=None,cancel_at=None):
    engine,rev,state = setup
    clock,calls = [0],[]
    monkeypatch.setattr(module.time,"monotonic",lambda:clock[0])
    def repair(p,r,opts,**callbacks):
        index = len(calls)
        calls.append((r,opts))
        if index == cancel_at:
            raise JobCancelled()
        missing,used,elapsed = specs[index]
        rid = r
        if missing is not None:
            rid = state["child"](r,"repair")["id"]
            state["checks"][rid] = check(missing)
        if index == invalid_at:
            state["checks"][rid]["drc"]["warnings"] = 1
        clock[0] += elapsed
        value = {"status":"repaired" if missing == 0 else "blocked","revision":rid,
                 "attempts":[{"status":"accepted" if missing is not None and i == used-1 else "rejected"}
                             for i in range(used)]}
        callbacks["progress"](value)
        return value
    monkeypatch.setattr(engine,"auto_repair_revision",repair)
    return clock,calls


def test_two_cycles_close_tail_with_separate_limits_and_ancestry(setup,monkeypatch):
    engine,rev,state = setup
    state["outcomes"] = [3]
    clock,calls = install_repair(setup,monkeypatch,[(None,4,300),(2,12,1400),(1,3,200),(0,5,600)])
    result = engine.complete_revision("p",rev,options())
    assert result["status"] == "completed",result
    assert [c.allow_multinet for _,c in calls] == [False,True,False,True]
    assert [c.max_attempts for _,c in calls] == [4,20,4,21]
    assert [c.time_budget_seconds for _,c in calls] == [300,1500,300,1600]
    attempt = result["attempts"][0]
    extra, = attempt["additional_repair_cycles"]
    assert extra["cycle"] == 2 and extra["status"] == "passed"
    assert extra["source_revision"] == attempt["repair"]["revision"] == calls[2][0]
    assert calls[3][0] == extra["additive_repair"]["revision"]
    assert extra["revision"] == result["revision"] == extra["repair"]["revision"]
    assert extra["max_attempts"] == 24 and extra["time_budget_seconds"] == 1800
    assert extra["effective_budget_seconds"] == 1800 and extra["elapsed_seconds"] == 800
    assert extra["attempts_used"] == 8 and extra["before_unconnected"] == 2 and extra["after_unconnected"] == 0
    assert len(attempt["repair"]["attempts"]) == 12
    assert state["routes"] == [rev] and not result["manufacturing_authorized"]


@pytest.mark.parametrize("cycles", [1,2,3])
def test_requested_cycle_limit_is_not_exceeded(setup,monkeypatch,cycles):
    engine,rev,state = setup
    state["outcomes"] = [4]
    clock,calls = install_repair(setup,monkeypatch,[(3,1,20),(2,1,20),(1,1,20)])
    result = engine.complete_revision("p",rev,options(cycles,
        repair={"allow_multinet":True,"max_attempts":1,"time_budget_seconds":1800}))
    assert result["status"] == "blocked" and len(calls) == cycles
    attempt = result["attempts"][0]
    assert [c["cycle"] for c in attempt.get("additional_repair_cycles",[])] == list(range(2,cycles+1))
    if cycles == 1:
        assert "additional_repair_cycles" not in attempt


@pytest.mark.parametrize("last_missing", [None,3,4])
def test_new_revision_without_strict_improvement_does_not_start_cycle(setup,monkeypatch,last_missing):
    engine,rev,state = setup
    state["outcomes"] = [3]
    clock,calls = install_repair(setup,monkeypatch,[(None,4,100),(last_missing,1,100)])
    result = engine.complete_revision("p",rev,options())
    assert result["status"] == "blocked" and len(calls) == 2
    assert "additional_repair_cycles" not in result["attempts"][0]


def test_no_progress_second_cycle_stops_before_third(setup,monkeypatch):
    engine,rev,state = setup
    state["outcomes"] = [3]
    clock,calls = install_repair(setup,monkeypatch,[(None,4,30),(2,1,100),(None,4,30),(None,0,20)])
    result = engine.complete_revision("p",rev,options())
    assert result["status"] == "blocked" and len(calls) == 4
    extra, = result["attempts"][0]["additional_repair_cycles"]
    assert extra["status"] == "no_progress" and extra["attempts_used"] == 4


def test_additive_improvement_with_blocked_multinet_allows_next_cycle(setup,monkeypatch):
    engine,rev,state = setup
    state["outcomes"] = [3]
    clock,calls = install_repair(setup,monkeypatch,[(2,1,50),(None,0,50),(0,1,50)])
    result = engine.complete_revision("p",rev,options())
    assert result["status"] == "completed" and len(calls) == 3
    attempt = result["attempts"][0]
    assert attempt["repair"]["status"] == "blocked"
    assert calls[2][0] == attempt["additive_repair"]["revision"]
    extra, = attempt["additional_repair_cycles"]
    assert extra["source_revision"] == calls[2][0] and extra["before_unconnected"] == 2
    assert extra["after_unconnected"] == 0 and "repair" not in extra


@pytest.mark.parametrize("elapsed,call_count", [(30,2),(31,1),(60,1),(61,1)])
def test_outer_budget_controls_cycle_restart_including_overrun(setup,monkeypatch,elapsed,call_count):
    engine,rev,state = setup
    state["outcomes"] = [3]
    clock,calls = install_repair(setup,monkeypatch,[(2,1,elapsed),(0,1,10)])
    result = engine.complete_revision("p",rev,options(time_budget_seconds=60,
        repair={"allow_multinet":True,"max_attempts":1,"time_budget_seconds":1800}))
    assert len(calls) == call_count,result
    assert calls[0][1].time_budget_seconds == 60
    if call_count == 2:
        assert calls[1][1].time_budget_seconds == 30
        assert result["attempts"][0]["additional_repair_cycles"][0]["effective_budget_seconds"] == 30
    else:
        assert "additional_repair_cycles" not in result["attempts"][0]


@pytest.mark.parametrize("stop", ["head_pass","first_pass","second_head_pass","invalid","cancel"])
def test_pass_invalid_candidate_and_cancellation_stop_remaining_work(setup,monkeypatch,stop):
    engine,rev,state = setup
    state["outcomes"] = [3]
    specs = [(None,4,100),(2,1,100),(None,4,100),(0,1,100)]
    if stop == "head_pass":
        specs[0] = (0,1,100)
    if stop == "first_pass":
        specs[1] = (0,1,100)
    if stop in {"second_head_pass","invalid"}:
        specs[2] = (0,1,100)
    clock,calls = install_repair(setup,monkeypatch,specs,
        invalid_at=2 if stop == "invalid" else None,cancel_at=2 if stop == "cancel" else None)
    if stop == "cancel":
        with pytest.raises(JobCancelled):
            engine.complete_revision("p",rev,options())
        result = read_json(next(engine._verified("p",rev)[1].glob("completion/*/result.json")))
        assert result["status"] == "interrupted"
        assert result["attempts"][0]["additional_repair_cycles"][0]["attempts_used"] == 0
    else:
        result = engine.complete_revision("p",rev,options())
        assert result["status"] == ("blocked" if stop == "invalid" else "completed")
        if stop == "invalid":
            attempt = result["attempts"][0]
            assert result["revision"] == attempt["repair"]["revision"]
            assert attempt["additional_repair_cycles"][0]["status"] == "rejected"
    assert len(calls) == {"head_pass":1,"first_pass":2,"second_head_pass":3,"invalid":3,"cancel":3}[stop]


def test_mcp_schema_and_queue_round_trip_without_native_worker(setup,monkeypatch):
    from pcb_weaver import server
    engine,rev,state = setup
    queue = JobQueue(engine.store.root,engine=engine)
    monkeypatch.setattr(server,"job_queue",lambda:queue)
    tools = asyncio.run(server.mcp.list_tools())
    schema = next(tool.inputSchema for tool in tools if tool.name == "submit_pcb_completion")
    definitions = schema.get("$defs",{})
    field = definitions["CompletionOptions"]["properties"]["repair_cycles"]
    assert field["default"] == field["minimum"] == 1 and field["maximum"] == 3
    job = server.submit_pcb_completion("p",rev,CompletionOptions.model_validate(options()))
    stored = queue.get(job["id"])
    assert stored["status"] == "queued" and not state["routes"]
    assert stored["request"]["completion_options"]["repair_cycles"] == 3
