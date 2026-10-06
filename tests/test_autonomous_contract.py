"""Source-only worker and native-gate contracts, not complex-board acceptance."""
from pathlib import Path
from copy import deepcopy
import pytest
from pcb_weaver import auto_repair as module
from pcb_weaver.repair_geometry import _read,_board
from test_auto_repair import setup
from test_negotiated_reroute import case


def test_new_source_prioritizes_untried_scopes_without_reusing_results():
    old = {"net":"N","region":[1,1,5,5],"finding":{"items":[{"uuid":"p"}]},
           "routing_rules":{"N":{"clearance":.2}},"ripup":{"nets":["N","M"],"remove_ids":["old"]},
           "source":"original.kicad_pcb","source_sha256":"old"}
    current = {**deepcopy(old),"source":"current.kicad_pcb","source_sha256":"new"}
    fresh = deepcopy(current)
    fresh["ripup"]["remove_ids"] = ["different"]
    changed_rules = deepcopy(current)
    changed_rules["routing_rules"]["N"]["clearance"] = .25
    attempted = {module._hypothesis_key(old)}
    candidates = [current,fresh,changed_rules]
    assert list(module._untried_first(candidates,attempted)) == [fresh,changed_rules,current]
    assert list(module._untried_first([current],attempted)) == [current]
    assert all(r["source_sha256"] == "new" for r in candidates)


def test_worker_audits_only_source_and_own_candidates(tmp_path):
    source,net,request = case(tmp_path)
    folder = tmp_path / "worker"
    folder.mkdir()
    result = module.proposal(folder,request,30,lambda:None,worker_module="pcb_weaver.negotiated_reroute")
    assert result["status"] == "proposed",result
    assert result["reference_used"] is False and result["original_partitions_preserved"]
    audit = result["board_access_audit"]
    assert audit["source"] == str(source.resolve())
    assert str(source.resolve()) in audit["opened"]
    assert all(Path(p) == source.resolve() or Path(p).is_relative_to(folder.resolve()) for p in audit["opened"])
    assert {"negotiated_reroute.py","board.py","repair_geometry.py","controlled_neckdown.py"} <= result["implementation_sha256"].keys()
    assert all(len(sha)==64 for sha in result["implementation_sha256"].values())


@pytest.mark.parametrize("fault",[None,"foreign_board","scope","native_warning","native_dangling"])
def test_autonomous_adoption_requires_scope_audit_and_native_pass(setup,monkeypatch,fault):
    engine,revision,source,state = setup
    original_targets,original_proposal = module.targets,module.proposal
    original_report = module._report
    def report(*args):
        value = original_report(*args)
        if fault == "native_dangling":
            for item in value["violations"]:
                if item["type"] == "new":
                    item["type"] = "track_dangling"
        return value
    monkeypatch.setattr(module,"_report",report)

    def targets(source,report,native,check,options,edge):
        plain = options.model_copy(update={"allow_multinet":False})
        request = next(original_targets(source,report,native,check,plain,edge))
        request.update(strategy="multinet",ripup={"nets":["VIN"],"remove_ids":[]},routing_rules={"VIN":request["rules"]})
        yield request

    def proposal(work,request,timeout,checkpoint,**kwargs):
        assert kwargs["worker_module"] == "pcb_weaver.negotiated_reroute"
        result = original_proposal(work,request,timeout,checkpoint)
        if request.get("preserve_cut_terminals"):
            state["new_warning"] = False
        result.update(reference_used=False,original_partitions_preserved=True,
            adjustment={"nets":["VIN"],"changed_ids":[]},
            board_access_audit={"source":str(source.resolve()),"working_root":str(work.resolve()),"opened":[str(source.resolve())]})
        if fault == "scope":
            result["adjustment"]["nets"] = ["OTHER"]
        if fault == "foreign_board":
            result["board_access_audit"]["opened"].append(str(source.parent / "foreign.kicad_pcb"))
        return result

    monkeypatch.setattr(module,"targets",targets)
    monkeypatch.setattr(module,"proposal",proposal)
    state["new_warning"] = fault in {"native_warning","native_dangling"}
    result = engine.auto_repair_revision("arbitrary",revision,{"allow_multinet":True})
    success = fault in {None,"native_dangling"}
    assert result["status"] == ("repaired" if success else "blocked")
    assert not result["manufacturing_authorized"]
    if not success:
        assert result["revision"] == revision
    if fault == "native_dangling":
        assert len(result["attempts"]) == 2
        assert result["attempts"][0]["followup"] and result["attempts"][1]["endpoint_retry"]


@pytest.mark.parametrize("expanded",[False,True])
def test_planner_ignores_other_layer_copper(tmp_path,monkeypatch,expanded):
    from pcb_weaver import ripup_planning as planner
    from shapely.geometry import box
    source = tmp_path / "board.kicad_pcb"
    source.write_text("fixture",encoding="ascii")
    pad = {"uuid":"p","net":"TARGET","layers":["F.Cu"]}
    model = {"footprints":[{"pads":[pad]}],"outline":{"bounds":[0,0,30,30]},"copper_layers":["F.Cu","B.Cu"]}
    copper = [{"id":net,"node":[],"geometry":{"kind":"segment","net":net,"layer":layer,"width":.2,
              "start":[10,y],"end":[11,y],"bounds":[9.9,y-.1,11.1,y+.1]}}
              for net,layer,y in (("BACK","B.Cu",10),("FRONT","F.Cu",10.6))]
    if expanded:
        from sexpdata import Symbol
        for identity,end,locked in (("long",14.5,False),("outside",20,False),("locked",14,True)):
            copper.append({"id":identity,"node":[Symbol("segment"),Symbol("locked")] if locked else [],
                "geometry":{"kind":"segment","net":"FRONT","layer":"F.Cu","width":.2,
                            "start":[11,10.6],"end":[end,10.6],"bounds":[10.9,10.5,end+.1,10.7]}})
    monkeypatch.setattr(planner,"read_board",lambda path:model)
    monkeypatch.setattr(planner,"_read",lambda path:([],"sha"))
    monkeypatch.setattr(planner,"_board",lambda *args,**kwargs:{"copper":copper})
    monkeypatch.setattr(planner,"pad_envelope",lambda pad:box(10,9.9,10.2,10.1))
    findings = [{"items":[{"uuid":"p","pos":{"x":10,"y":10}},{"uuid":"q","pos":{"x":12,"y":10}}]}]
    candidates = list(planner.plans(source,findings))
    assert candidates and all(p["nets"] == ["TARGET","FRONT"] for p in candidates)
    if expanded:
        whole = [p for p in candidates if p["ripup_extent"] == "routing_region"]
        assert whole and all(set(p["remove_ids"]) == {"FRONT","long"} for p in whole)
        assert all(not {"outside","locked","BACK"}.intersection(p["remove_ids"]) for p in candidates)
        assert all((p["region"][2]-p["region"][0])*(p["region"][3]-p["region"][1]) <= 2500 for p in candidates)
        assert not list(planner.plans(source,findings,maximum_area=1))
    else:
        assert all(p["remove_ids"] == ["FRONT"] for p in candidates)
