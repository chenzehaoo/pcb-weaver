from copy import deepcopy
import pytest
from pcb_weaver import completion_cleanup as module


@pytest.fixture
def geometry(monkeypatch):
    parsed = {"outline":[0,0,50,50],"copper":[
        {"id":"a","geometry":{"kind":"segment","layer":"F.Cu","bounds":[5,5,6,6]}},
        {"id":"b","geometry":{"kind":"segment","layer":"F.Cu","bounds":[6,5,7,6]}},
        {"id":"c","geometry":{"kind":"segment","layer":"F.Cu","bounds":[7,5,8,6]}}]}
    index = {"a":"A","b":"B","c":"C","pad":"P"}
    monkeypatch.setattr(module,"_read",lambda p:([],"hash"))
    monkeypatch.setattr(module,"_board",lambda *a,**k:parsed)
    monkeypatch.setattr(module,"_net_index",lambda p:index)
    def issue(a,b):
        return {"type":"clearance","severity":"error","items":[
            {"uuid":a,"pos":{"x":5,"y":5}},{"uuid":b,"pos":{"x":7,"y":5}}]}
    return parsed,index,issue


def test_scopes_merge_related_native_pairs_and_include_segment_extents(geometry):
    parsed,index,issue = geometry
    report = {"violations":[issue("a","b"),issue("b","c")]}
    before = deepcopy(report)
    scopes = module.clearance_scopes("unused",report)
    assert len(scopes) == 1
    assert scopes[0]["nets"] == ["A","B","C"]
    assert scopes[0]["region"] == [4.2,4.2,8.8,6.8]
    assert report == before


def test_unknown_object_is_not_used_to_infer_scope(geometry):
    _,_,issue = geometry
    with pytest.raises(ValueError,match="unknown"):
        module.clearance_scopes("unused",{"violations":[issue("a","unknown")]})


@pytest.mark.parametrize("change",["layer","via","oversized"])
def test_unsupported_or_oversized_scope_not_selected(geometry,change):
    parsed,_,issue = geometry
    g = parsed["copper"][0]["geometry"]
    if change == "layer": g["layer"] = "B.Cu"
    if change == "via": g["kind"] = "via"
    if change == "oversized": g["bounds"] = [1,1,40,40]
    assert module.clearance_scopes("unused",{"violations":[issue("a","b")]}) == []


@pytest.mark.parametrize("margin",[True,0,-1,2.1,float("inf")])
def test_margin_limits(geometry,margin):
    with pytest.raises(ValueError):
        module.clearance_scopes("unused",{"violations":[]},margin)


def test_warnings_not_used_as_repair_targets(geometry):
    _,_,issue = geometry
    warning = issue("a","b")
    warning["severity"] = "warning"
    assert module.clearance_scopes("unused",{"violations":[warning]}) == []


def test_width_normalization_uses_declared_floor_not_preferred_width(tmp_path,monkeypatch):
    monkeypatch.setattr(module,"read_json",lambda path:{"board":{"design_settings":{"rules":{"min_track_width":.2}}},
        "pcb_weaver_net_rules":{"P":{"declared_min_width_mm":.3,"minimums":{"track_width":.8}}}})
    monkeypatch.setattr(module,"read_board",lambda path:{"nets":[{"name":"P"},{"name":"GND"}]})
    seen = []
    monkeypatch.setattr(module,"raise_minima",lambda *args:seen.append(args) or {"ok":True})
    assert module.normalize_route_widths(tmp_path / "board.kicad_pcb","input","output") == {"ok":True}
    assert seen == [("input","output",.2,{"P":.3},{"P","GND"})]
