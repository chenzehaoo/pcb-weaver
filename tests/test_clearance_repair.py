from copy import deepcopy

import pytest

from pcb_weaver.clearance_repair import compare, eligible
from pcb_weaver.jobs import JobQueue, JobRequest
from pcb_weaver import server, catalog
from test_repair import check, report


def fixture():
    old = report([("g1", "g2")])
    old["violations"] = [{"type": "clearance", "severity": "error", "description": "too close",
                          "items": [{"uuid": "a"}, {"uuid": "b"}]}]
    before, after = check(1), check(1)
    before["drc"]["errors"] = 2
    return before, after, old, report([("g1", "g2")]), {"g1": "GND", "g2": "GND"}


def test_clearance_reduction_does_not_authorize_manufacturing():
    before, after, old, new, index = fixture()
    result = compare(before, after, old, new, index, index, ["VIN"])
    assert result["accepted"] and not result["manufacturing_authorized"]
    assert result["after_clearance"] == 0 and result["after_connections"] == {"GND": 1}


@pytest.mark.parametrize("change", ["warning", "short", "ignored", "parity", "minima", "erc", "summary", "severity", "regression", "scope", "no_progress", "pair"])
def test_regressions_are_rejected(change):
    before, after, old, new, index = fixture()
    if change in {"warning", "short"}:
        new["violations"].append({"type": "shorting_items", "severity": "warning" if change == "warning" else "error",
                                  "description": "new", "items": []})
        after["drc"]["errors"] += change == "short"
    elif change == "ignored":
        after["drc"]["ignored_checks"] = ["clearance"]
    elif change == "parity":
        after["connectivity"]["status"] = "blocked"
    elif change == "minima":
        after["persisted_track_minima"]["passed"] = False
    elif change == "erc":
        after["erc"]["errors"] = 1
    elif change == "summary":
        after["drc"]["errors"] = 0
    elif change == "severity":
        new["unconnected_items"][0]["severity"] = "warning"
    elif change == "regression":
        new["unconnected_items"].append(deepcopy(new["unconnected_items"][0]))
        after["drc"].update(errors=2, unconnected=2)
    elif change == "scope":
        new["unconnected_items"] = []
        after["drc"].update(errors=0, unconnected=0)
    elif change == "no_progress":
        new, after = deepcopy(old), deepcopy(before)
    elif change == "pair":
        old["violations"].append(deepcopy(old["violations"][0]))
        before["drc"]["errors"] = 3
        new["violations"] = deepcopy(old["violations"][:1])
        new["violations"][0]["items"] = [{"uuid": "other"}]
        after["drc"]["errors"] = 2
    assert not compare(before, after, old, new, index, index, ["VIN"])["accepted"]


def test_baseline_other_failures_not_hidden():
    before, _, old, _, _ = fixture()
    before["reasons"] = ["Engine changed the verification design snapshot"]
    assert eligible(before, old)


@pytest.mark.parametrize("status", ["improved", "blocked"])
def test_persistent_clearance_job_preserves_acceptance_boundary(tmp_path, monkeypatch, status):
    queue = JobQueue(tmp_path)
    calls = []
    monkeypatch.setattr(queue.engine, "inspect_revision", lambda *args: {})
    monkeypatch.setattr(queue.engine, "repair_clearance", lambda *args: {
        "status": status, "revision": "r-child" if status == "improved" else "r-parent",
        "candidate_revision": "r-child", "manufacturing_authorized": False})
    monkeypatch.setattr(catalog, "generate_report", lambda e, p, r: calls.append(r) or {"status": "generated"})
    monkeypatch.setattr(server, "job_queue", lambda: queue)
    job = server.submit_pcb_clearance_repair("clearance-test", "r-parent", ["VIN"], [30, 25, 45, 35])
    assert job["status"] == "queued"
    assert queue.run_once()
    final = queue.get(job["id"])
    assert final["status"] == ("completed" if status == "improved" else "blocked")
    assert calls == (["r-child"] if status == "improved" else [])
    assert final["result"]["revision"] == ("r-child" if status == "improved" else "r-parent")


def test_clearance_never_accepts_deletion():
    with pytest.raises(ValueError, match="never removes"):
        JobRequest(project="p", revision="r-parent", operation="clearance", repair_nets=["A"],
                   repair_region=[0, 0, 1, 1], repair_remove_ids=["track"])


@pytest.mark.asyncio
async def test_clearance_mcp_annotation():
    tool = next(t for t in await server.mcp.list_tools() if t.name == "submit_pcb_clearance_repair")
    assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is False
    assert tool.inputSchema["required"] == ["project", "revision", "nets", "region"]
