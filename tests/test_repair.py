from copy import deepcopy
from collections import Counter
from pathlib import Path

import pytest

from pcb_weaver.repair import validate_scope, compare_checks, connection_counts, quality_issues, diagnose
from pcb_weaver.service import EngineeringService


def check(missing):
    return {"status": "blocked" if missing else "passed", "reasons": [],
            "drc": {"status": "ok", "report_valid": True, "errors": missing, "unconnected": missing,
                    "excluded": 0, "ignored_checks": []},
            "erc": {"status": "ok", "errors": 0, "excluded": 0, "ignored_checks": []},
            "connectivity": {"status": "passed"}, "constraints": {"passed": True},
            "persisted_track_minima": {"passed": True}}


def report(pairs):
    return {"violations": [], "unconnected_items": [{"type":"unconnected_items", "severity":"error",
             "items": [{"uuid": a}, {"uuid": b}]} for a,b in pairs]}


@pytest.mark.parametrize("nets,region,ids,passes", [
    ([], [0,0,10,10], [], 3), (["A", "A"], [0,0,10,10], [], 3),
    ([""], [0,0,10,10], [], 3), (["A"] * 9, [0,0,10,10], [], 3),
    (["A"], [0,0,0,10], [], 3), (["A"], [0,0,10], [], 3),
    (["A"], [0,0,float("inf"),10], [], 3), (["A"], [False,0,10,10], [], 3),
    (["A"], [0,0,10,10], ["id", "id"], 3), (["A"], [0,0,10,10], [None], 3),
    (["A"], [0,0,10,10], [], 11), (["A"], [0,0,10,10], [], True),
])
def test_invalid_scope(nets, region, ids, passes):
    with pytest.raises(ValueError):
        validate_scope(nets, region, ids, passes)


def test_scope_valid():
    validate_scope(["+3.3V", "/GPT2"], [10,20,30,40], ["actual-uuid"], 3)


def test_improvement_still_not_manufacturing():
    index = {"a": "A", "b": "A", "c": "B", "d": "B"}
    result = compare_checks(check(2), check(1), report([("a","b"), ("c","d")]),
                            report([("c","d")]), index, index, ["A"])
    assert result["accepted"] and result["after_unconnected"] == 1
    assert result["manufacturing_authorized"] is False


def test_no_progress_rejected():
    r, idx = report([("a","b")]), {"a":"A", "b":"A"}
    assert not compare_checks(check(1), check(1), r, r, idx, idx, ["A"])["accepted"]


def test_net_regression_rejected_even_when_total_falls():
    index = {"a":"A", "b":"A", "c":"B", "d":"B"}
    assert not compare_checks(check(2), check(1), report([("a","b"), ("a","b")]),
                              report([("c","d")]), index, index, ["A", "B"])["accepted"]


def test_out_of_scope_improvement_rejected():
    idx = {"a":"A", "b":"A"}
    assert not compare_checks(check(1), check(0), report([("a","b")]), report([]), idx, idx, ["B"])["accepted"]


@pytest.mark.parametrize("items", [[], [{"uuid":"a"}], [{"uuid":"a"},{"uuid":"missing"}],
                                   [{"uuid":"a"},{"uuid":"c"}]])
def test_unknown_or_cross_net_drc_endpoints_refused(items):
    with pytest.raises(ValueError):
        connection_counts({"unconnected_items":[{"items": items}]}, {"a":"A", "c":"C"})


@pytest.mark.parametrize("field,value", [("constraints", {"passed":False}),
    ("persisted_track_minima", {"passed":False}), ("connectivity", {"status":"blocked"}),
    ("erc", {"status":"ok", "errors":1}), ("reasons", ["Engine changed the verification design snapshot"])])
def test_non_drc_failures_not_hidden(field,value):
    c = check(1)
    c[field] = value
    assert quality_issues(c)


def test_new_warning_rejects_candidate():
    old, new = report([("a","b")]), report([])
    new["violations"] = [{"type":"clearance", "severity":"warning", "description":"new", "items":[]}]
    idx = {"a":"A", "b":"A"}
    assert not compare_checks(check(1), check(0), old, new, idx, idx, ["A"])["accepted"]


def test_existing_warning_can_remain():
    old, new = report([("a","b")]), report([])
    old["violations"] = [{"type":"lib_footprint_mismatch", "severity":"warning", "description":"old", "items":[]}]
    new["violations"] = deepcopy(old["violations"])
    idx = {"a":"A", "b":"A"}
    assert compare_checks(check(1), check(0), old, new, idx, idx, ["A"])["accepted"]


def test_report_count_disagreement_refused():
    idx = {"a":"A", "b":"A"}
    assert not compare_checks(check(2), check(0), report([("a","b")]), report([]), idx, idx, ["A"])["accepted"]


def test_frozen_complex_board_diagnosis():
    root = Path(__file__).resolve().parents[1]
    data = root / "data"
    if not (data / "projects/system-mcp-acceptance/revisions/r-9b5f5075e65e4931/revision.json").exists():
        pytest.skip("Frozen native diagnostic revision is not installed")
    result = diagnose(EngineeringService(data), "system-mcp-acceptance", "r-9b5f5075e65e4931")
    assert result["status"] == "ok"
    assert result["unconnected_by_net"] == {"/GPT3":1, "/GPT2":1, "/PST1":1, "GND":1, "+3.3V":3}
    assert len(result["proposals"]) == 7
    assert all(p["remove_ids"] == [] and p["feasibility"] == "not_evaluated" for p in result["proposals"])
