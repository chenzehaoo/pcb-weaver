"""Bounded source-only multi-net proposals; the service enforces native adoption."""
from copy import deepcopy
import shutil
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Point, LineString, box
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .board import read_board
from .repair_geometry import _read, _board, _serialize, prepare_repair
from .controlled_neckdown import widen
from .routing_clearance import pad_envelope
from .storage import digest,write_json


def islands(source, net, region):
    pcb = _board(_read(source)[0],source=True)
    items = []
    for c in pcb["copper"]:
        g = c["geometry"]
        if g["net"] != net:
            continue
        if g["kind"] == "via":
            shape, layers = Point(g["at"]).buffer(g["size"]/2,quad_segs=32), pcb["layers"]
        else:
            shape = LineString([g["start"],g["end"]]).buffer(g["width"]/2,quad_segs=32)
            layers = [g["layer"]]
        items.append((c["id"],shape,set(layers)))
    for f in read_board(source)["footprints"]:
        for p in f["pads"]:
            if p["net"] == net and p["uuid"]:
                layers = pcb["layers"] if "*.Cu" in p["layers"] else p["layers"]
                items.append((p["uuid"],pad_envelope(p),set(layers)))
    if not items:
        return []
    shapes = [i[1] for i in items]
    a,b = shapely.STRtree(shapes).query(shapes,predicate="intersects")
    keep = np.array([bool(items[i][2]&items[j][2]) for i,j in zip(a,b)])
    graph = coo_matrix((np.ones(sum(keep)),(a[keep],b[keep])),shape=(len(items),len(items))).tocsr()
    _,groups = connected_components(graph,directed=False)
    roi = box(*region) if region is not None else None
    result = []
    for group in sorted(set(groups)):
        members = [item for i,item in enumerate(items) if groups[i] == group and (roi is None or item[1].intersects(roi))]
        if members:
            result.append(members)
    return result


def pair(groups):
    choices = ((x[1].distance(y[1]),x,y) for i,a in enumerate(groups)
               for b in groups[:i] for x in a for y in b)
    _,a,b = min(choices,key=lambda row:row[0])
    return {"items":[{"uuid":item[0],"pos":{"x":item[1].centroid.x,"y":item[1].centroid.y}}
                     for item in (a,b)]}


def partition_splits(baseline, current, region):
    """Detect broken original components; unrelated improvements cannot mask them."""
    roi = box(*region)
    demands = []
    for original in baseline:
        anchors = {item[0] for item in original}
        pieces = [g for g in current if any(item[0] in anchors for item in g)]
        if len(pieces) > 1:
            local = [[item for item in g if item[1].intersects(roi)] for g in pieces]
            if any(not group for group in local):
                raise ValueError("Broken original connectivity has no in-scope terminal")
            demands.append(local)
    return demands


def cut_terminals(source, removed, region):
    """Remember retained trace endpoints attached to removed copper on that layer."""
    copper = _board(_read(source)[0],source=True)["copper"]
    selected = [c["geometry"] for c in copper if c["id"] in removed]
    anchors = {}
    for c in copper:
        g = c["geometry"]
        if c["id"] in removed or g["kind"] != "segment":
            continue
        for point in (g["start"],g["end"]):
            if not box(*region).covers(Point(point)):
                continue
            for other in selected:
                if other["net"] != g["net"] or (other["kind"]=="segment" and other["layer"]!=g["layer"]):
                    continue
                core = Point(other["at"]) if other["kind"]=="via" else LineString([other["start"],other["end"]])
                if core.distance(Point(point)) <= other.get("size",other.get("width",0))/2+1e-8:
                    anchors.setdefault(c["id"],[]).append((list(point),g["layer"]))
                    break
    return anchors


def anchored_pair(source, folder, groups, anchors, sequence, net):
    """Restrict reconnect terminals to cut endpoints before joining split components."""
    from uuid import uuid4
    from sexpdata import Symbol
    choices = []
    for group in groups:
        anchored = [(identity,Point(point),{layer},point,layer) for identity,_,_ in group
                    for point,layer in anchors.get(identity,[])]
        choices.append(anchored or [(*item,None,None) for item in group])
    _,a,b = min((x[1].distance(y[1]),x,y) for i,g in enumerate(choices)
                for other in choices[:i] for x in g for y in other)
    ast = _read(source)[0]
    pcb = _board(ast,source=True)
    code = next(code for code,name in pcb["codes"].items() if name==net)
    ids,items = [],[]
    for identity,shape,layers,point,layer in (a,b):
        if point is not None:
            identity = str(uuid4())
            ids.append(identity)
            # Temporary terminals are removed before widening/merging, never delivered as copper.
            ast.append([Symbol("segment"),[Symbol("start"),*point],
                [Symbol("end"),point[0]+.0001,point[1]],[Symbol("width"),.001],
                [Symbol("layer"),layer],[Symbol("net"),code],[Symbol("uuid"),identity]])
        items.append({"uuid":identity,"pos":{"x":shape.centroid.x,"y":shape.centroid.y}})
    seeded = folder / f"terminals-{sequence}.kicad_pcb"
    seeded.write_bytes(_serialize(ast))
    return seeded,{"items":items},ids


def _trial(request, folder, order):
    from .auto_proposal import route
    source = Path(request["source"])
    if digest(source) != request["source_sha256"]:
        raise ValueError("Rip-up source changed")
    local = request["ripup"]
    nets,region,removed = local["nets"],local["region"],local["remove_ids"]
    if not 1 <= len(nets) <= 8 or len(removed)>64:
        raise ValueError("Rip-up exceeds network/copper budget")
    if len(region) != 4 or not 0 < (region[2]-region[0])*(region[3]-region[1]) <= 2500:
        raise ValueError("Rip-up exceeds region budget")
    baseline = {n:islands(source,n,None) for n in nets}
    anchors = cut_terminals(source,removed,region)
    current = folder / "ripped.kicad_pcb"
    manifest = prepare_repair(source,current,folder / "empty.kicad_pcb",nets,region,removed)
    routes = []
    for net in order:
        rules = deepcopy(request["routing_rules"][net])
        preferred = rules.pop("preferred_width")
        for iteration in range(16):
            groups = islands(current,net,None)
            demands = partition_splits(baseline[net],groups,region)
            terminals = {item["uuid"] for item in request["finding"]["items"]}
            target_connected = any(terminals <= {item[0] for item in group} for group in groups)
            if net == request["net"] and not target_connected:
                finding = request["finding"]
                routed_source,temporary = current,[]
            elif demands:
                if request.get("preserve_cut_terminals",True):
                    routed_source,finding,temporary = anchored_pair(current,folder,demands[0],anchors,len(routes),net)
                else:
                    routed_source,finding,temporary = current,pair(demands[0]),[]
            else:
                break
            output = folder / f"route-{len(routes)}.kicad_pcb"
            result = route(routed_source,output,{"net":net,"region":region,"finding":finding,
                                          "rules":rules,"strategy":"neckdown","expand_terminals":not bool(temporary),
                                          "exact_terminals":bool(temporary)})
            routes.append({"net":net,**result})
            if result["status"] != "proposed":
                return {"status":"blocked","reason":"Unable to reconnect "+net,"failed_net":net,"routes":routes}
            if temporary:
                ast = _read(output)[0]
                board = _board(ast,source=True)
                seeds = {id(c["node"]) for c in board["copper"] if c["id"] in temporary}
                cleaned = folder / f"unseeded-{len(routes)}.kicad_pcb"
                cleaned.write_bytes(_serialize([n for n in ast if id(n) not in seeds]))
                output = cleaned
                routes[-1]["cut_terminals_restored"] = len(temporary)
            if rules["track_width"] < preferred:
                restored = folder / f"wide-{len(routes)}.kicad_pcb"
                proof = widen(current,output,restored,net,preferred,rules["track_width"],rules["clearance"],
                              escape=rules["track_width"],allow_bottlenecks=True)
                routes[-1]["width_restoration"] = proof
                if proof["status"] != "proposed":
                    return {"status":"blocked","reason":proof["reason"],"routes":routes}
                output = restored
            if len(islands(output,net,None)) >= len(groups):
                return {"status":"blocked","reason":"Geometric reconnection made no progress","routes":routes}
            current = output
        else:
            return {"status":"blocked","reason":"Reconnection iteration budget exhausted","routes":routes}
    if digest(source) != request["source_sha256"]:
        raise ValueError("Rip-up source changed during routing")
    for net in nets:
        groups = islands(current,net,None)
        if partition_splits(baseline[net],groups,region):
            return {"status":"blocked","reason":"Original connectivity partition regressed","failed_net":net,"routes":routes}
    return {"status":"proposed","output":current.name,"output_sha256":digest(current),
            "routes":routes,"ripup_manifest":manifest,
            "adjustment":{"changed_ids":removed,"nets":nets,"kind":"multi_net_ripup",
                          "requires_native_verification":True},
            "requires_native_verification":True,"manufacturing_authorized":False}


def run(request, folder):
    """Bounded failed-net priority negotiation, restarting from the same source."""
    folder = Path(folder)
    if digest(Path(request["source"])) != request["source_sha256"]:
        raise ValueError("Rip-up source changed")
    nets = request["ripup"]["nets"]
    if request["net"] not in nets or len(nets) != len(set(nets)):
        raise ValueError("Target must belong to a unique rip-up net set")
    order = [request["net"], *[n for n in nets if n != request["net"]]]
    trials, seen = [], set()
    for index in range(min(8, 2*len(nets))):
        if tuple(order) in seen:
            break
        seen.add(tuple(order))
        work = folder / f"priority-{index:02d}"
        work.mkdir()
        result = _trial(request,work,order)
        write_json(work / "trial.json",{"order":list(order),**result})
        trials.append({"order":list(order),**result})
        if result["status"] == "proposed":
            target = folder / "negotiated.kicad_pcb"
            if target.exists():
                raise ValueError("Negotiated output must be new")
            shutil.copyfile(work / result["output"],target)
            return {**result,"output":target.name,"output_sha256":digest(target),"negotiation_trials":trials,
                    "reference_used":False,"original_partitions_preserved":True}
        failed = result.get("failed_net")
        if not failed:
            break
        order = [failed,*[n for n in order if n != failed]]
    return {"status":"blocked","reason":"No order restored all original network partitions",
            "negotiation_trials":trials,"reference_used":False,"manufacturing_authorized":False}


if __name__ == "__main__":
    import argparse
    import os
    import sys
    from .storage import read_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request",type=Path)
    args = parser.parse_args()
    request = read_json(args.request)
    source = Path(request["source"]).resolve(strict=True)
    root = args.request.parent.resolve(strict=True)
    opened = set()
    def implementation_snapshot():
        return {path.name:digest(path) for path in sorted(Path(__file__).parent.glob("*.py"))}
    implementation = implementation_snapshot()

    def audit(event, arguments):
        if event != "open" or not isinstance(arguments[0],(str,bytes,os.PathLike)):
            return
        path = Path(os.fsdecode(arguments[0]))
        if path.suffix.lower() != ".kicad_pcb":
            return
        path = path.resolve()
        if path != source and not path.is_relative_to(root):
            raise PermissionError("Autonomous worker cannot access another board")
        if path == source and isinstance(arguments[2],int) and arguments[2] & (os.O_WRONLY|os.O_RDWR|os.O_TRUNC|os.O_APPEND):
            raise PermissionError("Autonomous worker source is read-only")
        opened.add(str(path))

    sys.addaudithook(audit)
    try:
        result = run(request,root)
        if implementation_snapshot() != implementation:
            raise ValueError("Routing implementation changed during proposal")
    except (ValueError,OSError,KeyError,TypeError,IndexError) as error:
        result = {"status":"blocked","reason":type(error).__name__+": "+str(error)}
    result.update(reference_used=False,manufacturing_authorized=False,
                  implementation_sha256=implementation,
                  board_access_audit={"source":str(source),"working_root":str(root),"opened":sorted(opened)})
    write_json(root / "proposal.json",result)
