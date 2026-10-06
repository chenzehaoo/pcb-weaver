from pathlib import Path
from copy import deepcopy
import pytest

from pcb_weaver.negotiated_reroute import run,islands
from pcb_weaver.repair_geometry import _read,_board,_serialize
from pcb_weaver.storage import digest
from test_grid_route import fixture,FINDING,RULES
from test_repair_geometry import segment


def case(tmp_path):
    source = fixture(tmp_path)
    ast = _read(source)[0]
    ast.extend([segment("lower",(37,25),(37,27),.2,1),
                segment("obstacle",(37,27),(37,33),.2,1),
                segment("upper",(37,33),(37,35),.2,1)])
    source.write_bytes(_serialize(ast))
    net = next(c["geometry"]["net"] for c in _board(_read(source)[0],source=True)["copper"] if c["id"] == "obstacle")
    region = [30,24,44,36]
    request = {"source":str(source),"source_sha256":digest(source),"net":"VIN", "finding":deepcopy(FINDING),
               "ripup":{"nets":["VIN",net],"remove_ids":["obstacle"],"region":region},
               "routing_rules":{n:{**RULES,"preferred_width":.2} for n in ("VIN",net)}}
    return source,net,request


def test_target_and_ripped_neighbor_both_reconnected(tmp_path):
    source,net,request = case(tmp_path)
    original = source.read_bytes()
    work = tmp_path / "work"
    work.mkdir()
    result = run(request,work)
    assert result["status"] == "proposed", result
    assert result["requires_native_verification"] and not result["manufacturing_authorized"]
    output = work / result["output"]
    assert len(islands(output,"VIN",request["ripup"]["region"])) == 1
    assert len(islands(output,net,request["ripup"]["region"])) == 1
    assert source.read_bytes() == original
    assert any(r.get("cut_terminals_restored")==2 for r in result["routes"])
    old = {c["id"]:c["node"] for c in _board(_read(source)[0],source=True)["copper"]}
    new = {c["id"]:c["node"] for c in _board(_read(output)[0],source=True)["copper"]}
    assert all(new[k] == v for k,v in old.items() if k != "obstacle")
    assert all(c["geometry"].get("width",.2) >= .2 for c in _board(_read(output)[0],source=True)["copper"])
    new_ends = {tuple(point) for c in _board(_read(output)[0],source=True)["copper"]
                if c["id"] not in old and c["geometry"]["kind"] == "segment"
                for point in (c["geometry"]["start"],c["geometry"]["end"])}
    assert {(37,27),(37,33)} <= new_ends


def test_stale_source_and_out_of_scope_removal_fail(tmp_path):
    source,net,request = case(tmp_path)
    request["source_sha256"] = "stale"
    with pytest.raises(ValueError,match="source changed"):
        run(request,tmp_path)
    request["source_sha256"] = digest(source)
    request["ripup"]["region"] = [30,29,44,31]
    with pytest.raises(ValueError,match="crosses region"):
        run(request,tmp_path)


def test_equal_island_count_cannot_hide_a_split():
    from shapely.geometry import Point
    from pcb_weaver.negotiated_reroute import partition_splits
    a,b,c,d = [(name,Point(x,1),{"F.Cu"}) for x,name in enumerate("abcd",1)]
    baseline = [[a,b],[c,d]]
    current = [[a],[b,c,d]]
    assert len(baseline) == len(current)
    assert len(partition_splits(baseline,current,[0,0,5,5])) == 1


def test_failed_neighbor_is_prioritized_from_unchanged_source(tmp_path,monkeypatch):
    from pcb_weaver import negotiated_reroute as module
    source,net,request = case(tmp_path)
    orders = []
    def trial(req,folder,order):
        orders.append(list(order))
        assert digest(source) == req["source_sha256"]
        return {"status":"blocked","failed_net":net if len(orders)==1 else "VIN"}
    monkeypatch.setattr(module,"_trial",trial)
    result = run(request,tmp_path)
    assert orders == [["VIN",net],[net,"VIN"]]
    assert result["status"] == "blocked" and not result["reference_used"]


def test_missing_local_terminal_rejects_partial_restore():
    from shapely.geometry import Point
    from pcb_weaver.negotiated_reroute import partition_splits
    a = ("a",Point(1,1),{"F.Cu"})
    b = ("b",Point(20,20),{"F.Cu"})
    with pytest.raises(ValueError,match="no in-scope terminal"):
        partition_splits([[a,b]],[[a],[b]],[0,0,5,5])


@pytest.mark.parametrize("target_layer",["F.Cu","B.Cu"])
def test_congestion_matrix_and_source_preservation(tmp_path,target_layer):
    from test_repair_geometry import tag
    source,net,request = case(tmp_path)
    ast = _read(source)[0]
    endpoint = next(c["node"] for c in _board(ast,source=True)["copper"] if c["id"] == "b")
    next(n for n in endpoint if tag(n)=="layer")[1] = target_layer
    source.write_bytes(_serialize(ast))
    request["source_sha256"] = digest(source)
    result = run(request,tmp_path)
    assert result["status"] == "proposed",result
    assert result["original_partitions_preserved"] and not result["reference_used"]
    assert digest(source) == request["source_sha256"]


def test_two_layer_immutable_wall_is_not_bypassed(tmp_path):
    from test_repair_geometry import tag
    source,net,request = case(tmp_path)
    ast = _read(source)[0]
    for layer in ("F.Cu","B.Cu"):
        wall = segment("wall-"+layer,(38,24),(38,36),1,1)
        next(n for n in wall if tag(n)=="layer")[1] = layer
        ast.append(wall)
    source.write_bytes(_serialize(ast))
    request["source_sha256"] = digest(source)
    result = run(request,tmp_path)
    assert result["status"] == "blocked" and not (tmp_path / "negotiated.kicad_pcb").exists()
    assert digest(source) == request["source_sha256"] and not result["manufacturing_authorized"]
