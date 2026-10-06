"""Bounded, topology-preserving via relocation with fixed incident far endpoints."""
from pathlib import Path
import math

import numpy as np
from scipy.optimize import minimize
from shapely.geometry import Point, LineString

from .board import read_board, _locked
from .repair_geometry import _read, _board, _field, _serialize
from .routing_clearance import pad_envelope
from .storage import digest


def relocate(source, output, identity, corridor, *, clearance=.15, max_move=.35, corridor_width=.2, corridor_layers=None):
    source, output = Path(source), Path(output)
    if output.exists() or source.resolve() == output.resolve():
        raise ValueError("Relocation output must be new")
    if (any(type(v) not in (int,float) or not math.isfinite(v) for v in (max_move,clearance,corridor_width))
            or not 0 < max_move <= .5 or not 0 < clearance <= 1 or not 0 < corridor_width <= 1):
        raise ValueError("Invalid bounded relocation rules")
    ast, sha = _read(source)
    board = _board(ast,source=True)
    selected = next((c for c in board["copper"] if c["id"] == identity and c["geometry"]["kind"] == "via"), None)
    if selected is None:
        raise ValueError("Via identity does not resolve")
    g = selected["geometry"]
    origin = g["at"]
    incident = [c for c in board["copper"] if c["geometry"]["kind"] == "segment"
                and origin in [c["geometry"]["start"],c["geometry"]["end"]]]
    if not incident or any(c["geometry"]["net"] != g["net"] or _locked(c["node"]) for c in [selected,*incident]):
        raise ValueError("Ambiguous or locked incident copper")
    members = {c["id"] for c in [selected,*incident]}
    if any(math.dist(c["geometry"]["start"],c["geometry"]["end"]) > 3 for c in incident):
        raise ValueError("Incident trace exceeds the local 3 mm scope")
    items = [selected,*incident]
    outlines = board["outline"]
    obstacles = []
    for c in board["copper"]:
        h = c["geometry"]
        if h["net"] == g["net"]:
            continue
        if h["kind"] == "via":
            obstacles.append((Point(h["at"]),h["size"]/2,board["layers"]))
        else:
            obstacles.append((LineString([h["start"],h["end"]]),h["width"]/2,[h["layer"]]))
    for f in read_board(source)["footprints"]:
        for p in f["pads"]:
            if p["net"] != g["net"]:
                obstacles.append((pad_envelope(p),0,board["layers"] if "*.Cu" in p["layers"] else p["layers"]))
    span = ["F.Cu"] if corridor_layers is None else corridor_layers
    if not isinstance(span, list) or not span or not set(span) <= set(board["layers"]):
        raise ValueError("Invalid reserved corridor layers")
    obstacles.append((LineString(corridor),corridor_width/2,span))

    def geometry(c, point):
        h = c["geometry"]
        if h["kind"] == "via":
            return Point(point), h["size"]/2,board["layers"]
        return LineString([point if a == origin else a for a in [h["start"],h["end"]]]),h["width"]/2,[h["layer"]]

    pairs = []
    for i,c in enumerate(items):
        shape,radius,layers = geometry(c,origin)
        for obstacle,r,span in obstacles:
            required = clearance + radius + r + .000005
            if set(layers) & set(span) and shape.distance(obstacle) <= required + max_move*math.sqrt(2):
                pairs.append((i,obstacle,required))

    def slacks(delta):
        point = np.asarray(origin) + delta
        shapes = [geometry(c,point.tolist())[0] for c in items]
        values = [shapes[i].distance(obstacle)-required for i,obstacle,required in pairs]
        values += [shapes[i].length-.001 for i in range(1,len(shapes))]
        values += [point[0]-g["size"]/2-outlines[0],outlines[2]-point[0]-g["size"]/2,
                   point[1]-g["size"]/2-outlines[1],outlines[3]-point[1]-g["size"]/2]
        return np.asarray(values)

    solutions = []
    failures = []
    for seed in ([0,0],[0,.25],[-.15,.25],[.25,0],[0,-.25]):
        trial = minimize(lambda d:float(np.dot(d,d)),seed,method="SLSQP",bounds=[(-max_move,max_move)]*2,
                         constraints={"type":"ineq","fun":slacks},options={"maxiter":120,"ftol":1e-12})
        point = np.round(np.asarray(origin)+trial.x,6)
        delta = point-np.asarray(origin)
        if np.max(np.abs(delta)) <= max_move and min(slacks(delta)) >= -1e-8:
            solutions.append((float(np.dot(delta,delta)),point,min(slacks(delta))))
        else:
            failures.append((float(min(slacks(delta))),delta))
    if not solutions:
        worst,delta = max(failures,key=lambda row:row[0])
        values = slacks(delta)
        return {"status":"blocked","reason":"No legal bounded via relocation", "uuid":identity,
                "best_minimum_slack_mm":worst,"trial_delta":delta.tolist(),
                "limiting_obstacles":[{"item":items[pairs[j][0]]["id"],"obstacle":pairs[j][1].wkt,"required_mm":pairs[j][2],"slack_mm":float(values[j])}
                    for j in sorted(range(len(pairs)),key=lambda k:values[k])[:8]]}
    _,point,slack = min(solutions,key=lambda row:row[0])
    for c in items:
        if c["geometry"]["kind"] == "via":
            _field(c["node"],"at",2)[1:] = point.tolist()
        else:
            for field in ("start","end"):
                value = _field(c["node"],field,2)
                if value[1:] == origin:
                    value[1:] = point.tolist()
    if digest(source) != sha:
        raise ValueError("Source changed during relocation")
    with output.open("xb") as stream:
        stream.write(_serialize(ast))
    return {"status":"proposed","net":g["net"],"changed_ids":sorted(members),"before":origin,"after":point.tolist(),
            "source_sha256":sha,"output_sha256":digest(output),"minimum_slack_mm":float(slack),
            "max_axis_move_mm":max_move,"widths_drills_and_far_endpoints_preserved":True,
            "reserved_corridor":{"points":corridor,"width_mm":corridor_width,"layers":span},
            "manufacturing_authorized":False,"requires_native_verification":True}
