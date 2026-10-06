from copy import deepcopy
import json
import pytest
from shapely.geometry import box, Point

from pcb_weaver import auto_proposal as proposal
from pcb_weaver.auto_repair import compact_via_policy, adjustment_scope
from pcb_weaver.storage import write_json, digest


def pad(identity="pad", net="A", x=5, y=5):
    return dict(uuid=identity, net=net, x=x, y=y, layers=["F.Cu"])


@pytest.mark.parametrize("strategy,expected", [("additive", 1), ("expanded", 2),
                                              ("neckdown", 2), ("fanout", 1)])
def test_refinement_only_retries_same_physical_scope(monkeypatch, tmp_path, strategy, expected):
    calls = []
    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "blocked", "reason": "No path"}
    monkeypatch.setattr(proposal, "propose", fake)
    monkeypatch.setattr(proposal, "read_board", lambda _: {"footprints": [{"pads": [pad()]}]})
    monkeypatch.setattr("pcb_weaver.routing_clearance.pad_envelope", lambda _: box(4.8, 4, 5.2, 6))
    request = dict(strategy=strategy, net="A", region=[4.5, 3.5, 5.5, 6.5],
                   finding={"items": [{"uuid": "pad"}]}, rules={"track_width": .2})
    before = deepcopy(request)
    result = proposal.route(tmp_path / "source", tmp_path / "target", request)
    assert len(calls) == expected and request == before
    assert all(args == calls[0][0] for args, _ in calls)
    assert all(k["exact_edges"] and k["allow_rule_boundary"] for _, k in calls)
    if expected == 2:
        assert calls[-1][1]["refine"] == {"region": request["region"], "step": .01}
        assert len(result["coarse_and_refinement_trials"]) == 1


def test_successful_coarse_route_is_not_repeated(monkeypatch, tmp_path):
    monkeypatch.setattr(proposal, "propose", lambda *a, **k: {"status": "proposed"})
    monkeypatch.setattr(proposal, "read_board", lambda _: pytest.fail("Unneeded board read"))
    result = proposal.route(tmp_path / "a", tmp_path / "b", dict(
        strategy="expanded", net="A", region=[0, 0, 10, 10], finding={}, rules={}))
    assert result["status"] == "proposed" and result["coarse_and_refinement_trials"] == []


def test_landing_candidates_clear_fixed_pads_and_remain_bounded(monkeypatch):
    monkeypatch.setattr("pcb_weaver.routing_clearance.pad_envelope",
                        lambda p: box(p["x"]-.15, p["y"]-.8, p["x"]+.15, p["y"]+.8))
    target, obstacle = pad(), pad("other", "B", 4.5, 5)
    rules = {"via_diameter": .6, "clearance": .15}
    region = [3, 3, 7, 7]
    points = proposal.landing_points(target, rules, region, [target, obstacle])
    assert points and len(points) <= 140 and len(points) == len(set(map(tuple, points)))
    assert points == proposal.landing_points(target, rules, region, [target, obstacle])
    for x, y in points:
        assert 3.3 <= x <= 6.7 and 3.3 <= y <= 6.7
        assert box(4.35,4.2,4.65,5.8).distance(Point(x,y)) >= .45-1e-9


def test_compact_vias_require_authenticated_explicit_limits(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    project = source.with_suffix(".kicad_pro")
    settings = {"board": {"design_settings": {"rules": {
        "min_via_diameter": .5, "min_via_annular_width": .05,
        "min_through_hole_diameter": .4}}}}
    write_json(project, settings)
    rules = {"via_diameter": .8, "via_drill": .4}
    assert compact_via_policy(source, {}, "A", rules) is None
    native = {"project_sha256": digest(project)}
    result = compact_via_policy(source, native, "A", rules)
    assert result["diameter_mm"] == .5 and result["drill_mm"] == .4
    assert rules == {"via_diameter": .8, "via_drill": .4}
    settings["pcb_weaver_net_rules"] = {"A": {"minimums": {"via_diameter": .8}}}
    write_json(project, settings)
    assert compact_via_policy(source, {"project_sha256": digest(project)}, "A", rules) is None
    source.with_suffix(".kicad_dru").write_text("(version 1)")
    assert compact_via_policy(source, {"project_sha256": digest(project)}, "B", rules) is None


@pytest.mark.parametrize("value", [None, 0, -1, True, float("nan")])
def test_compact_vias_reject_missing_or_invalid_annulus(tmp_path, value):
    source = tmp_path / "board.kicad_pcb"
    project = source.with_suffix(".kicad_pro")
    project.write_text(json.dumps({"board": {"design_settings": {"rules": {
        "min_via_diameter": .5, "min_via_annular_width": value,
        "min_through_hole_diameter": .4}}}}))
    assert compact_via_policy(source, {"project_sha256": digest(project)}, "A",
                              {"via_diameter": .8, "via_drill": .4}) is None


def test_fanout_continues_when_adjustment_cannot_connect(monkeypatch, tmp_path):
    monkeypatch.setattr(proposal, "_read", lambda _: ([], "source-hash"))
    monkeypatch.setattr(proposal, "_board", lambda *a, **k: {"copper": [{"geometry": {
        "kind": "segment", "net": "B", "start": [5,4.9], "end": [6,4.9], "width": .2}}]})
    monkeypatch.setattr(proposal, "read_board", lambda _: {"footprints": [{"pads": [pad()]}]})
    monkeypatch.setattr(proposal, "landing_points", lambda *a: [[5,5.4],[5.1,5.4]])
    monkeypatch.setattr(proposal, "relocate", lambda *a, **k: {"status": "proposed"})
    routes = []
    def evaluate(output, proof):
        routes.append(output)
        return {"status": "blocked" if len(routes) == 1 else "proposed"}
    request = dict(net="A", finding={"items": [{"uuid": "pad"}]},
                   region=[3,3,7,7], rules={"via_diameter": .6, "clearance": .15})
    output, proof, trials = proposal.fanout(tmp_path / "board", tmp_path, request, evaluate)
    assert len(routes) == len(trials) == 2 and output == routes[-1]
    assert trials[0]["routing_trial"]["status"] == "blocked"
    assert proof["routing_trial"]["status"] == "proposed"


def test_split_fragment_audit_scope_is_bounded(tmp_path):
    from test_grid_route import fixture
    source = fixture(tmp_path)
    region = [32,29,33,31]
    scope = adjustment_scope(source, region, ["a"], 100)
    assert scope[0] < 31.9 and scope[2] > 34.1 and region == [32,29,33,31]
    with pytest.raises(ValueError, match="authorized bounds"):
        adjustment_scope(source, region, ["a"], 1)
    with pytest.raises(ValueError, match="unknown or duplicate"):
        adjustment_scope(source, region, ["a", "a"], 100)
