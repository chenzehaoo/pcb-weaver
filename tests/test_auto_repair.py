"""Fault injection and control contracts, separate from native board acceptance."""
from copy import deepcopy
from pathlib import Path

import pytest

from pcb_weaver import auto_repair as module
from pcb_weaver.jobs import JobRequest, JobQueue, JobCancelled, _request_payload
from pcb_weaver.models import AutoRepairOptions
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, read_json, write_json
from pcb_weaver.repair_geometry import _read, _serialize
from test_repair import check, report
from test_repair_geometry import segment


@pytest.mark.parametrize("values", [{"max_attempts":0},{"max_attempts":True},{"time_budget_seconds":29},
    {"max_region_area_mm2":float("nan")},{"allow_neckdown":"true"},{"proposal_timeout_seconds":181}])
def test_budget_and_authorization_validation(values):
    with pytest.raises(ValueError):
        AutoRepairOptions(**values)


def test_automatic_job_has_no_coordinate_or_release_inputs():
    job = JobRequest(project="arbitrary",revision="rev-any",operation="auto_repair")
    assert job.auto_options == AutoRepairOptions()
    assert not job.auto_options.allow_neckdown and not job.auto_options.allow_local_adjustment
    for extra in ({"release":True},{"repair_nets":["N"]},{"repair_region":[0,0,10,10]}):
        with pytest.raises(ValueError):
            JobRequest(project="arbitrary",revision="rev-any",operation="auto_repair",**extra)
    assert "auto_options" not in _request_payload(JobRequest(project="x",revision="r",operation="verify"))


def test_multinet_opt_in_preserves_existing_job_payloads():
    for operation,field in (("auto_repair","auto_options"),("complete","completion_options")):
        request = JobRequest(project="x",revision="r",operation=operation)
        payload = _request_payload(request)
        options = payload[field] if operation == "auto_repair" else payload[field]["repair"]
        assert "allow_multinet" not in options
    with pytest.raises(ValueError):
        AutoRepairOptions(allow_multinet="yes")
    request = JobRequest(project="x",revision="r",operation="auto_repair",auto_options={"allow_multinet":True})
    assert _request_payload(request)["auto_options"]["allow_multinet"] is True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = EngineeringService(tmp_path / "data")
    board = Path(__file__).resolve().parents[1] / "examples/manufacturing-demo/two-layer.kicad_pcb"
    revision = engine.import_project("arbitrary",str(board))["revision"]["id"]
    original, folder = engine._verified("arbitrary",revision)
    source = folder / "design" / original["board"]
    state = {"before":1,"after":0,"new_warning":False,"calls":0}

    def verification(project, rev):
        state["calls"] += 1
        value = check(state["before"] if rev == revision else state["after"])
        value["verification_id"] = "v-"+rev
        value["persisted_track_minima"].update(global_minimum_mm=.2,per_net_minimum_mm={})
        return value

    def findings(e, project, rev, value):
        raw = report([("a","b")]*value["drc"]["unconnected"])
        raw["coordinate_units"] = "mm"
        for finding in raw["unconnected_items"]:
            for item,xy in zip(finding["items"],[(28,35),(30,35)]):
                item["pos"] = dict(zip(("x","y"),xy))
        if rev != revision and state["new_warning"]:
            raw["violations"] = [{"type":"new","severity":"warning","description":"injected","items":[]}]
        return raw

    monkeypatch.setattr(engine,"_verify",verification)
    monkeypatch.setattr(module,"_report",findings)
    monkeypatch.setattr(module,"_erc_report",lambda *a:{"$schema":"https://schemas.kicad.org/erc.v1.json","sheets":[]})
    monkeypatch.setattr(module,"_net_index",lambda p:{"a":"VIN","b":"VIN"})
    monkeypatch.setattr(engine.toolchain,"inspect_board",lambda p:{"status":"ok","nets":{"VIN":{
        "track_width":.25,"via_diameter":.8,"via_drill":.4,"clearance":.2}}})

    def proposed(work, request, timeout, checkpoint):
        ast = _read(Path(request["source"]))[0]
        ast.append(segment("new",(28,35),(30,35),.25,2))
        output = work / "routed.kicad_pcb"
        output.write_bytes(_serialize(ast))
        return {"status":"proposed","output":output.name,"output_sha256":digest(output)}

    monkeypatch.setattr(module,"proposal",proposed)
    return engine,revision,source,state


def test_adopts_only_natively_accepted_child(setup):
    engine,revision,source,state = setup
    before = digest(source)
    result = engine.auto_repair_revision("arbitrary",revision)
    assert result["status"] == "repaired", result
    assert result["revision"] != revision and result["after_unconnected"] == 0
    assert result["attempts"][0]["comparison"]["accepted"]
    assert not result["manufacturing_authorized"] and digest(source) == before


@pytest.mark.parametrize("mode", ["no_progress","warning"])
def test_bad_candidate_is_recorded_and_not_adopted(setup, mode):
    engine,revision,source,state = setup
    state["after"] = 1 if mode == "no_progress" else 0
    state["new_warning"] = mode == "warning"
    before = digest(source)
    result = engine.auto_repair_revision("arbitrary",revision,{"max_attempts":1})
    assert result["status"] == "blocked" and result["revision"] == revision
    assert result["attempts"][0]["candidate_revision"] != revision
    assert not result["attempts"][0]["comparison"]["accepted"]
    assert digest(source) == before


def test_no_proposal_stops_at_budget_without_new_revision(setup, monkeypatch):
    engine,revision,source,state = setup
    monkeypatch.setattr(module,"proposal",lambda *a:{"status":"blocked","reason":"Timeout","timed_out":True})
    result = engine.auto_repair_revision("arbitrary",revision,{"max_attempts":1})
    assert result["status"] == "blocked" and len(result["attempts"]) == 1
    assert len(engine.store.list_revisions("arbitrary")) == 1


def test_already_passed_is_noop(setup):
    engine,revision,source,state = setup
    state["before"] = 0
    result = engine.auto_repair_revision("arbitrary",revision)
    assert result["status"] == "repaired" and result["revision"] == revision
    assert result["already_passed"] and result["attempts"] == []


def test_cancellation_retains_accepted_checkpoint(setup):
    engine,revision,source,state = setup
    state.update(before=2,after=1)
    snapshots = []
    def checkpoint():
        if snapshots and any(a["status"] == "accepted" for a in snapshots[-1]["attempts"]):
            raise JobCancelled()
    with pytest.raises(JobCancelled):
        engine.auto_repair_revision("arbitrary",revision,checkpoint=checkpoint,progress=snapshots.append)
    assert snapshots[-1]["status"] == "interrupted"
    assert snapshots[-1]["revision"] != revision and snapshots[-1]["after_unconnected"] == 1
    engine._verified("arbitrary",snapshots[-1]["revision"])


def test_queue_persists_partial_best_revision_when_blocked(setup, monkeypatch):
    engine,revision,source,state = setup
    queue = JobQueue(engine.store.root,engine=engine)
    state.update(before=2,after=1)
    job = queue.submit(JobRequest(project="arbitrary",revision=revision,operation="auto_repair",
                                 auto_options=AutoRepairOptions(max_attempts=1)))
    assert queue.run_once()
    result = queue.get(job["id"])
    assert result["status"] == "blocked", result
    assert result["result"]["revision"] != revision
    assert result["result"]["steps"]["auto_repair"]["after_unconnected"] == 1


def test_area_limit_does_not_expand_to_whole_board(setup):
    engine,revision,source,state = setup
    result = engine.auto_repair_revision("arbitrary",revision,{"max_region_area_mm2":.01})
    assert result["status"] == "blocked" and result["attempts"] == []


def test_report_units_are_normalized_before_scope_selection(setup):
    engine,revision,source,state = setup
    check = engine._verify("arbitrary",revision)
    native = engine.toolchain.inspect_board(source)
    raw = module._report(engine,"arbitrary",revision,check)
    mm = list(module.targets(source,raw,native,check,AutoRepairOptions(),.5))
    inches = deepcopy(raw)
    inches["coordinate_units"] = "in"
    for item in inches["unconnected_items"][0]["items"]:
        item["pos"] = {k:v/25.4 for k,v in item["pos"].items()}
    normalized = list(module.targets(source,inches,native,check,AutoRepairOptions(),.5))
    assert normalized[0]["region"] == pytest.approx(mm[0]["region"])


@pytest.mark.parametrize("cancel", [False,True])
def test_proposal_process_is_reaped_on_timeout_or_cancel(tmp_path, monkeypatch, cancel):
    processes = []
    class Process:
        returncode = None
        def __init__(self,*args,**kwargs):
            self.killed = self.waited = False
            processes.append(self)
        def poll(self):
            return self.returncode
        def kill(self):
            self.killed = True
            self.returncode = -1
        def wait(self):
            self.waited = True
    monkeypatch.setattr(module.subprocess,"Popen",Process)
    def checkpoint():
        if cancel:
            raise JobCancelled()
    if cancel:
        with pytest.raises(JobCancelled):
            module.proposal(tmp_path,{"fixture":True},0,checkpoint)
    else:
        result = module.proposal(tmp_path,{"fixture":True},0,checkpoint)
        assert result["status"] == "blocked" and result["timed_out"]
    assert processes[0].killed and processes[0].waited


def test_proposal_cannot_return_an_external_file(tmp_path, monkeypatch):
    outside = tmp_path / "outside.kicad_pcb"
    outside.write_text("not-a-real-board")
    folder = tmp_path / "attempt"
    folder.mkdir()
    class Process:
        returncode = 0
        def __init__(self,*args,**kwargs):
            write_json(folder / "proposal.json",{"status":"proposed","output":str(outside),"output_sha256":digest(outside)})
        def poll(self): return 0
        def wait(self): pass
    monkeypatch.setattr(module.subprocess,"Popen",Process)
    with pytest.raises(ValueError):
        module.proposal(folder,{"fixture":True},1,lambda:None)


def test_time_budget_stops_before_baseline_native_command(setup,monkeypatch):
    engine,revision,source,state = setup
    counter = iter([0,31])
    monkeypatch.setattr(module.time,"monotonic",lambda:next(counter,31))
    result = engine.auto_repair_revision("arbitrary",revision,{"time_budget_seconds":30})
    assert result["status"] == "blocked" and state["calls"] == 0


def test_erc_failure_prevents_proposals(setup,monkeypatch):
    engine,revision,source,state = setup
    real = engine._verify
    def broken(*args):
        value = real(*args)
        value["erc"]["errors"] = 1
        return value
    monkeypatch.setattr(engine,"_verify",broken)
    result = engine.auto_repair_revision("arbitrary",revision)
    assert result["status"] == "blocked" and result["attempts"] == []


def test_neckdown_requires_project_profile_authorization(setup):
    engine,revision,source,state = setup
    result = engine.auto_repair_revision("arbitrary",revision,{"allow_neckdown":True})
    assert result["status"] == "blocked" and state["calls"] == 0
