"""Completion orchestration fault tests; these do not emulate native acceptance."""
from copy import deepcopy
from pathlib import Path
import pytest

from pcb_weaver import completion as module
from pcb_weaver.jobs import JobRequest, JobQueue, JobCancelled, _request_payload
from pcb_weaver.models import CompletionOptions
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import read_json, digest
from test_repair import check, report


@pytest.mark.parametrize("options", [{"candidate_count":0},{"candidate_count":True},{"candidate_count":6},
    {"route_passes":0},{"route_passes":101},{"time_budget_seconds":59},{"time_budget_seconds":7201},
    {"placement_spread_mm":-0.1},{"placement_spread_mm":2.1},{"placement_spread_mm":True},{"routing_policy":"ignore_rules"},
    {"placement_mode":"fixed"},{"placement_mode":True},
    {"repair":{"allow_local_adjustment":"yes"}}])
def test_strict_completion_limits(options):
    with pytest.raises(ValueError):
        CompletionOptions(**options)


def test_completion_request_boundaries_and_legacy_payload():
    request = JobRequest(project="p",revision="r",operation="complete")
    assert request.completion_options == CompletionOptions()
    for extra in ({"release":True},{"route":False},{"board_path":"x.kicad_pcb"},
                  {"constraints":{}},{"auto_options":{}},{"repair_nets":["N"]}):
        with pytest.raises(ValueError):
            JobRequest(project="p",revision="r",operation="complete",**extra)
    with pytest.raises(ValueError):
        JobRequest(project="p",revision="r",operation="verify",completion_options={})
    assert "completion_options" not in _request_payload(JobRequest(project="p",revision="r",operation="auto_repair"))
    legacy = _request_payload(request)
    assert "placement_mode" not in legacy["completion_options"]
    assert _request_payload(JobRequest.model_validate(legacy)) == legacy
    explicit = JobRequest(project="p",revision="r",operation="complete",completion_options={"placement_mode":"optimize"})
    assert _request_payload(explicit) == legacy
    preserved = JobRequest(project="p",revision="r",operation="complete",completion_options={"placement_mode":"preserve"})
    assert _request_payload(preserved)["completion_options"]["placement_mode"] == "preserve"


@pytest.fixture
def setup(tmp_path,monkeypatch):
    engine = EngineeringService(tmp_path / "data")
    board = Path(__file__).resolve().parents[1] / "examples/manufacturing-demo/two-layer.kicad_pcb"
    rev = engine.import_project("p",str(board))["revision"]["id"]
    state = {"routes":[],"checks":{},"outcomes":[0,0],"timeout_first":False,"duplicate":False,"repair":False}
    def child(parent,operation):
        with engine.store.lock("p"):
            rid,folder,data = engine._new("p",parent)
            if mutation := state.get("mutate_"+operation):
                mutation(folder / "design" / data["board"],folder)
            sealed = engine._seal("p",rid,folder,data["board"],parent,operation,{})
        return sealed
    def verify(p,r):
        result = deepcopy(state["checks"].get(r,check(3)))
        result["verification_id"] = "v-"+r
        return result
    def plan(p,r,count,**kwargs):
        return {"status":"ok","plan_id":"plan","candidates":[{"id":str(i),"feasible":True,
            "placements":[{"reference":"R1","x":0 if state["duplicate"] else i,"y":0,"rotation":0}],
            "metrics":{"weighted_hpwl_mm":i}} for i in range(count)]}
    def route(p,r,passes,**kwargs):
        assert kwargs == ({"normalize_widths":True} if state.get("normalize") else {"strict_widths":True})
        i = len(state["routes"])
        state["routes"].append(r)
        if i == 0 and state["timeout_first"]:
            return {"status":"blocked","timed_out":True,"failed_stage":"autoroute"}
        data = child(r,"route")
        state["checks"][data["id"]] = check(state["outcomes"][i])
        return {"status":"routed_unverified","revision":data}
    def repair(p,r,options,**callbacks):
        state["repair_options"] = options
        if not state["repair"]:
            return {"status":"blocked","revision":r}
        data = child(r,"repair")
        state["checks"][data["id"]] = check(0)
        value = {"status":"repaired","revision":data["id"]}
        callbacks["progress"](value)
        return value
    monkeypatch.setattr(engine,"verify_revision",verify)
    monkeypatch.setattr(engine,"plan_layout",plan)
    monkeypatch.setattr(engine,"apply_layout",lambda p,r,pid,cid:{"revision":child(r,"placement")})
    monkeypatch.setattr(engine,"route_revision",route)
    monkeypatch.setattr(engine,"auto_repair_revision",repair)
    monkeypatch.setattr(module,"_report",lambda e,p,r,c:state.get("reports",{}).get(r,report([("a","b")]*c["drc"]["unconnected"])))
    monkeypatch.setattr(module,"_erc_report",lambda e,p,r,c:state.get("erc_reports",{}).get(r,
        {"$schema":"https://schemas.kicad.org/erc.v1.json","sheets":[]}))
    monkeypatch.setattr(module.catalog,"verification",lambda e,p,r:verify(p,r))
    state["child"] = child
    return engine,rev,state


def test_native_pass_retained_and_source_immutable(setup):
    engine,rev,state = setup
    data,folder = engine._verified("p",rev)
    sha = digest(folder / "design" / data["board"])
    result = engine.complete_revision("p",rev,{"candidate_count":2})
    assert result["status"] == "completed",result
    assert result["revision"] != rev and result["best"]["unconnected"] == 0
    assert len(state["routes"]) == 1 and not result["manufacturing_authorized"]
    assert digest(folder / "design" / data["board"]) == sha


def test_timeout_tries_different_recorded_layout(setup):
    engine,rev,state = setup
    state["timeout_first"] = True
    result = engine.complete_revision("p",rev,{"candidate_count":2})
    assert result["status"] == "completed"
    assert len(state["routes"]) == 2
    assert result["attempts"][0]["diagnosis"]["category"] == "route_timeout"


def test_duplicate_layout_never_retried(setup):
    engine,rev,state = setup
    state.update(duplicate=True,timeout_first=True)
    result = engine.complete_revision("p",rev,{"candidate_count":2})
    assert result["status"] == "blocked" and result["revision"] == rev
    assert len(state["routes"]) == 1


def test_best_partial_survives_worse_second_attempt(setup):
    engine,rev,state = setup
    state["outcomes"] = [1,2]
    result = engine.complete_revision("p",rev,{"candidate_count":2})
    assert result["status"] == "blocked"
    assert result["best"]["unconnected"] == 1
    assert result["revision"] == result["attempts"][0]["routed_revision"]
    assert result["best"]["status"] == "blocked"


def test_automatic_repair_integrated_and_retained(setup):
    engine,rev,state = setup
    state.update(outcomes=[1,2],repair=True)
    result = engine.complete_revision("p",rev,{"candidate_count":2})
    assert result["status"] == "completed" and result["best"]["unconnected"] == 0
    assert result["revision"] == result["attempts"][0]["repair"]["revision"]


def test_normalized_width_policy_still_requires_native_pass(setup):
    engine,rev,state = setup
    state.update(normalize=True,outcomes=[1,2],repair=True)
    result = engine.complete_revision("p",rev,{"routing_policy":"normalize_widths"})
    assert result["status"] == "completed" and result["best"]["unconnected"] == 0


def test_normalized_clearance_cleanup_uses_verified_child_before_adoption(setup,monkeypatch):
    from pcb_weaver import clearance_repair, completion_cleanup
    engine,rev,state = setup
    state.update(normalize=True,outcomes=[1,2])
    monkeypatch.setattr(clearance_repair,"eligible",lambda c,r:[])
    monkeypatch.setattr(clearance_repair,"clearance_keys",lambda r:{"pair":1})
    monkeypatch.setattr(completion_cleanup,"clearance_scopes",lambda *a:[
        {"nets":["GND"],"region":[1,1,2,2],"source_items":["a","b"],"margin_mm":a[-1]}])
    def clean(p,r,nets,region):
        child = state["child"](r,"clearance")
        state["checks"][child["id"]] = check(0)
        return {"status":"improved","revision":child["id"]}
    monkeypatch.setattr(engine,"repair_clearance",clean)
    result = engine.complete_revision("p",rev,{"candidate_count":1,"routing_policy":"normalize_widths"})
    assert result["status"] == "completed" and result["best"]["unconnected"] == 0
    assert result["revision"] == result["attempts"][0]["cleaned_revision"]
    assert len(result["attempts"][0]["clearance_cleanup"]) <= 6


def test_erc_failure_stops_before_placement(setup):
    engine,rev,state = setup
    state["checks"][rev] = check(3)
    state["checks"][rev]["erc"]["errors"] = 1
    result = engine.complete_revision("p",rev)
    assert result["status"] == "blocked" and state["routes"] == []
    assert result["diagnosis"]["category"] == "electrical_input"


def test_cancel_persists_partial_result(setup):
    engine,rev,state = setup
    state["outcomes"] = [1,2]
    saved = []
    def progress(value):
        saved.append(value)
        if value["stage"] == "candidate_rejected":
            raise JobCancelled()
    with pytest.raises(JobCancelled):
        engine.complete_revision("p",rev,{"candidate_count":2},progress=progress)
    result = read_json(next(engine._verified("p",rev)[1].glob("completion/*/result.json")))
    assert result["status"] == "interrupted" and result["revision"] != rev
    assert result["best"]["unconnected"] == 1


def test_budget_does_not_adopt_unverified_routed_child(setup,monkeypatch):
    engine,rev,state = setup
    clock = [0]
    monkeypatch.setattr(module.time,"monotonic",lambda:clock[0])
    route = engine.route_revision
    def slow(*args,**kwargs):
        value = route(*args,**kwargs)
        clock[0] = 61
        return value
    monkeypatch.setattr(engine,"route_revision",slow)
    result = engine.complete_revision("p",rev,{"time_budget_seconds":60})
    assert result["status"] == "blocked" and result["revision"] == rev
    assert "unverified" in result["reason"]


def test_queue_persists_completion_progress(setup,monkeypatch):
    engine,rev,state = setup
    state["outcomes"] = [1,2]
    queue = JobQueue(engine.store.root)
    queue.engine = engine
    monkeypatch.setattr(module.catalog,"generate_report",lambda *args:{"status":"generated"})
    job = queue.submit(JobRequest(project="p",revision=rev,operation="complete",completion_options={"candidate_count":2}))
    assert queue.run_once()
    result = queue.get(job["id"])
    assert result["status"] == "blocked"
    assert result["result"]["revision"] != rev
    assert result["result"]["steps"]["complete"]["best"]["unconnected"] == 1


def test_unavailable_native_drc_stops_before_routing(setup):
    engine,rev,state = setup
    state["checks"][rev] = check(3)
    state["checks"][rev]["drc"]["status"] = "failed"
    result = engine.complete_revision("p",rev)
    assert result["status"] == "blocked" and state["routes"] == []
    assert result["diagnosis"]["category"] == "native_unavailable"


def test_strict_route_uses_isolated_toolchain_without_changing_shared_profile(tmp_path,monkeypatch):
    import pcb_weaver.service as service_module
    engine = EngineeringService(tmp_path / "data",{"controlled_neckdown":True})
    board = Path(__file__).resolve().parents[1] / "examples/manufacturing-demo/two-layer.kicad_pcb"
    rev = engine.import_project("p",str(board))["revision"]["id"]
    seen = []
    class Tools:
        def __init__(self,config):
            seen.append(config)
        def export_dsn(self,*args):
            return {"status":"blocked","reason":"Test boundary reached"}
    monkeypatch.setattr(service_module,"Toolchain",Tools)
    result = engine.route_revision("p",rev,strict_widths=True)
    assert result["failed_stage"] == "export_dsn"
    assert seen == [{"controlled_neckdown":False}]
    assert engine.toolchain.controlled_neckdown is True
    assert engine.toolchain.config["controlled_neckdown"] is True


@pytest.mark.parametrize("repair", [False,True])
def test_preserve_bypasses_placement_and_routes_immutable_child(setup,monkeypatch,repair):
    engine,rev,state = setup
    original = deepcopy(engine._verified("p",rev)[0])
    state.update(outcomes=[1 if repair else 0],repair=repair)
    def forbidden(*args,**kwargs):
        pytest.fail("Preserve mode must not plan or apply placement")
    monkeypatch.setattr(engine,"plan_layout",forbidden)
    monkeypatch.setattr(engine,"apply_layout",forbidden)
    result = engine.complete_revision("p",rev,{"placement_mode":"preserve","candidate_count":5,
        "repair":{"allow_multinet":True}})
    assert result["status"] == "completed",result
    assert "plan" not in result and len(result["attempts"]) == 1
    attempt = result["attempts"][0]
    assert attempt["placement_revision"] == rev and state["routes"] == [rev]
    assert engine._verified("p",attempt["routed_revision"])[0]["parent"] == rev
    assert engine._verified("p",rev)[0] == original
    assert result["best"]["moved_components"] == 0 and not result["manufacturing_authorized"]
    if repair:
        assert state["repair_options"].allow_multinet is False
        assert result["revision"] == attempt["additive_repair"]["revision"]
        assert "repair" not in attempt
        assert engine._verified("p",result["revision"])[0]["parent"] == attempt["routed_revision"]


def mutate_preserved(path,folder,field):
    from sexpdata import Symbol
    from pcb_weaver.board import _load, _child, _children
    from pcb_weaver.repair_geometry import _serialize
    from pcb_weaver.storage import write_json
    if field == "companion_files":
        project = path.with_suffix(".kicad_pro")
        settings = read_json(project)
        settings["board"]["design_settings"]["rules"]["min_clearance"] = 0.001
        write_json(project,settings)
        return
    if field == "constraints":
        settings = read_json(folder / "constraints.json")
        settings["fabrication"]["min_track_mm"] = 0.001
        write_json(folder / "constraints.json",settings)
        return
    ast = _load(path)
    fp = _children(ast,"footprint")[0]
    if field == "position":
        _child(fp,"at")[1] += 0.000001
    elif field == "rotation":
        at = _child(fp,"at")
        if len(at) == 3:
            at.append(0)
        at[3] += 90
    elif field == "side":
        _child(fp,"layer")[1] = "B.Cu"
    elif field == "electrical_identity":
        pad = _children(fp,"pad")[0]
        pad[1] = "changed"
    elif field == "pad_size":
        _child(_children(fp,"pad")[0],"size")[1] += 0.1
    elif field == "outline":
        edge = next(n for n in ast[1:] if _child(n,"layer",[None,""])[1] == "Edge.Cuts")
        _child(edge,"end")[1] += 1
    elif field == "board_rules":
        _child(ast,"setup").append([Symbol("pad_to_mask_clearance"),0.123])
    path.write_bytes(_serialize(ast))


@pytest.mark.parametrize("stage", ["route","repair"])
@pytest.mark.parametrize("field", ["position","rotation","side","electrical_identity",
    "pad_size","outline","board_rules","companion_files","constraints"])
def test_preserve_rejects_changed_input_in_route_and_repair(setup,stage,field):
    engine,rev,state = setup
    state.update(outcomes=[1 if stage == "repair" else 0],repair=stage == "repair")
    state["mutate_"+stage] = lambda path,folder:mutate_preserved(path,folder,field)
    result = engine.complete_revision("p",rev,{"placement_mode":"preserve"})
    assert result["status"] == "blocked",result
    if stage == "route":
        assert result["revision"] == rev
        assert result["attempts"][0]["preservation_issues"]
        assert "repair_options" not in state
    else:
        assert result["revision"] == result["attempts"][0]["routed_revision"]
        rejected = result["attempts"][0]["repair"]["revision"]
        assert not result["candidate_assessments"][rejected]["accepted"]


@pytest.mark.parametrize("kind", ["drc","erc"])
@pytest.mark.parametrize("stage", ["route","repair"])
@pytest.mark.parametrize("change", ["same","replacement","duplicate"])
def test_preserve_findings_compared_to_original_baseline(setup,monkeypatch,kind,stage,change):
    engine,rev,state = setup
    state.update(outcomes=[1 if stage == "repair" else 0],repair=stage == "repair")
    finding = {"type":"test_warning","severity":"warning","description":"original","items":[{"uuid":"a"}]}
    def native_report(e,p,r,c):
        findings = [deepcopy(finding)]
        operation = engine._verified(p,r)[0]["operation"]
        if operation == stage and change == "replacement":
            findings[0]["description"] = "new"
        elif operation == stage and change == "duplicate":
            findings.append(deepcopy(finding))
        if kind == "drc":
            return {**report([("a","b")]*c["drc"]["unconnected"]),"violations":findings}
        return {"$schema":"https://schemas.kicad.org/erc.v1.json",
                "sheets":[{"uuid_path":"/root","violations":findings}]}
    monkeypatch.setattr(module,"_report" if kind == "drc" else "_erc_report",native_report)
    result = engine.complete_revision("p",rev,{"placement_mode":"preserve"})
    assert result["status"] == ("completed" if change == "same" else "blocked"),result
    if change != "same":
        assert result["revision"] == (rev if stage == "route" else result["attempts"][0]["routed_revision"])
        assert any(any("native" in reason for reason in value["reasons"])
                   for value in result["candidate_assessments"].values())


def test_preserve_missing_authenticated_erc_blocks_before_routing(setup,monkeypatch):
    engine,rev,state = setup
    def unavailable(*args):
        raise ValueError("Native ERC evidence unavailable")
    monkeypatch.setattr(module,"_erc_report",unavailable)
    result = engine.complete_revision("p",rev,{"placement_mode":"preserve"})
    assert result["status"] == "blocked" and not state["routes"]
    assert "Native ERC evidence unavailable" in result["reason"]


def test_preserve_budget_does_not_adopt_unverified_child(setup,monkeypatch):
    engine,rev,state = setup
    clock = [0]
    monkeypatch.setattr(module.time,"monotonic",lambda:clock[0])
    route = engine.route_revision
    def slow(*args,**kwargs):
        result = route(*args,**kwargs)
        clock[0] = 61
        return result
    monkeypatch.setattr(engine,"route_revision",slow)
    result = engine.complete_revision("p",rev,{"placement_mode":"preserve","time_budget_seconds":60})
    assert result["status"] == "blocked" and result["revision"] == rev
    assert "unverified" in result["reason"]
