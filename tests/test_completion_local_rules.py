"""Preservation and repair scheduling tests; no simulated native acceptance."""
from copy import deepcopy

import pytest
from sexpdata import Symbol, loads

from pcb_weaver import completion as module
from pcb_weaver.board import _load, _child, _children
from pcb_weaver.repair_geometry import _serialize
from pcb_weaver.jobs import JobCancelled
from test_completion import setup
from test_repair import check


def owner(ast, scope):
    if scope == "board":
        return ast
    fp = _children(ast,"footprint")[0]
    return fp if scope == "footprint" else _children(fp,"pad")[0]


@pytest.mark.parametrize("scope", ["board","footprint","pad"])
@pytest.mark.parametrize("tag", ["clearance","solder_paste_margin","future_rule"])
@pytest.mark.parametrize("change", ["add","modify","delete"])
def test_ast_keeps_local_and_unknown_rules(scope,tag,change):
    before = loads('(kicad_pcb (footprint "F" (pad "1" smd rect)))')
    if change != "add":
        owner(before,scope).append([Symbol(tag),0.3])
    after = deepcopy(before)
    target = owner(after,scope)
    if change == "add":
        target.append([Symbol(tag),0.1])
    elif change == "modify":
        _child(target,tag)[1] = 0.1
    else:
        target.remove(_child(target,tag))
    assert module._preserved_board_ast(before) != module._preserved_board_ast(after)


def test_ast_allows_only_root_routing_and_generator_metadata():
    before = loads('(kicad_pcb (version 20240108) (generator "old") (generator_version "8")'
                   ' (footprint "F" (pad "1" smd rect (clearance 0.3))))')
    after = deepcopy(before)
    _child(after,"generator")[1] = "pcbnew"
    _child(after,"generator_version")[1] = "9.0.9"
    after.extend(loads('((segment (width 0.25)) (via (size 0.6)))'))
    assert module._preserved_board_ast(before) == module._preserved_board_ast(after)
    _child(after,"version")[1] += 1
    assert module._preserved_board_ast(before) != module._preserved_board_ast(after)
    _child(after,"version")[1] -= 1
    owner(after,"pad").append([Symbol("generator"),"nested-unknown"])
    assert module._preserved_board_ast(before) != module._preserved_board_ast(after)


@pytest.mark.parametrize("stage", ["route","repair"])
@pytest.mark.parametrize("scope", ["board","footprint","pad"])
def test_local_rule_change_rejected_before_adoption(setup,stage,scope):
    engine,rev,state = setup
    state.update(outcomes=[1 if stage == "repair" else 0],repair=stage == "repair")
    def mutate(path,folder):
        ast = _load(path)
        owner(ast,scope).append([Symbol("clearance"),0.001])
        path.write_bytes(_serialize(ast))
    state["mutate_"+stage] = mutate
    result = engine.complete_revision("p",rev,{"placement_mode":"preserve"})
    assert result["status"] == "blocked"
    attempt = result["attempts"][0]
    if stage == "route":
        assert result["revision"] == rev and "repair_options" not in state
        reasons = attempt["preservation_issues"]
    else:
        assert result["revision"] == attempt["routed_revision"]
        reasons = result["candidate_assessments"][attempt["repair"]["revision"]]["reasons"]
    assert "Preserve mode changed nonrouting_ast" in reasons


@pytest.mark.parametrize("used,elapsed,attempt_limit,time_limit,second", [
    (4,200,24,1800,True),
    (0,100,24,1800,True),
    (4,20,5,1800,True),
    (1,30,2,60,True),
    (1,600,24,600,False),
    (1,1780,24,1800,False),
])
def test_additive_then_multinet_share_actual_budget(setup,monkeypatch,used,elapsed,attempt_limit,time_limit,second):
    engine,rev,state = setup
    state["outcomes"] = [2]
    clock, calls = [0], []
    monkeypatch.setattr(module.time,"monotonic",lambda:clock[0])
    def repair(p,r,options,**callbacks):
        calls.append((r,options))
        first = len(calls) == 1
        rid = r
        if not first or used:
            rid = state["child"](r,"repair")["id"]
            state["checks"][rid] = check(1 if first else 0)
        clock[0] += elapsed if first else 1
        result = {"status":"blocked" if first else "repaired","revision":rid,
                  "attempts":[{}]*(used if first else 1)}
        callbacks["progress"](result)
        return result
    monkeypatch.setattr(engine,"auto_repair_revision",repair)
    result = engine.complete_revision("p",rev,{"placement_mode":"preserve","time_budget_seconds":5400,
        "repair":{"allow_multinet":True,"allow_neckdown":True,"allow_local_adjustment":True,
                  "max_attempts":attempt_limit,"time_budget_seconds":time_limit}})
    assert len(calls) == (2 if second else 1),result
    first = calls[0][1]
    assert not first.allow_multinet and first.max_attempts == min(4,attempt_limit-1)
    assert first.time_budget_seconds == min(300,time_limit-30)
    assert first.allow_neckdown and first.allow_local_adjustment
    attempt = result["attempts"][0]
    assert "additive_repair" in attempt
    if second:
        source, options = calls[1]
        assert source == attempt["additive_repair"]["revision"]
        assert options.allow_multinet and options.max_attempts == attempt_limit-used
        assert options.time_budget_seconds == time_limit-elapsed
        assert result["revision"] == attempt["repair"]["revision"]
        assert result["status"] == "completed"
    else:
        assert "repair" not in attempt and result["status"] == "blocked"


@pytest.mark.parametrize("cancel", [False,True])
def test_invalid_or_cancelled_additive_never_starts_multinet(setup,monkeypatch,cancel):
    engine,rev,state = setup
    state["outcomes"] = [1]
    calls = []
    def repair(p,r,options,**callbacks):
        calls.append(r)
        if cancel:
            raise JobCancelled()
        def mutate(path,folder):
            ast = _load(path)
            owner(ast,"pad").append([Symbol("clearance"),0.001])
            path.write_bytes(_serialize(ast))
        state["mutate_repair"] = mutate
        rid = state["child"](r,"repair")["id"]
        state["checks"][rid] = check(0)
        result = {"status":"repaired","revision":rid,"attempts":[{}]}
        callbacks["progress"](result)
        return result
    monkeypatch.setattr(engine,"auto_repair_revision",repair)
    options = {"placement_mode":"preserve","repair":{"allow_multinet":True}}
    if cancel:
        with pytest.raises(JobCancelled):
            engine.complete_revision("p",rev,options)
    else:
        result = engine.complete_revision("p",rev,options)
        assert result["status"] == "blocked"
        assert result["revision"] == result["attempts"][0]["routed_revision"]
    assert len(calls) == 1


@pytest.mark.parametrize("attempt_limit,time_limit", [(1,1800),(24,30)])
def test_multinet_reservation_skips_unaffordable_additive(setup,monkeypatch,attempt_limit,time_limit):
    engine,rev,state = setup
    state["outcomes"] = [1]
    calls = []
    monkeypatch.setattr(module.time,"monotonic",lambda:0)
    def repair(p,r,options,**callbacks):
        calls.append(options)
        return {"status":"blocked","revision":r,"attempts":[]}
    monkeypatch.setattr(engine,"auto_repair_revision",repair)
    result = engine.complete_revision("p",rev,{"placement_mode":"preserve",
        "repair":{"allow_multinet":True,"max_attempts":attempt_limit,"time_budget_seconds":time_limit}})
    assert len(calls) == 1 and calls[0].allow_multinet
    assert calls[0].max_attempts == attempt_limit and calls[0].time_budget_seconds == time_limit
    assert "additive_repair" not in result["attempts"][0]
    assert "repair" in result["attempts"][0]
