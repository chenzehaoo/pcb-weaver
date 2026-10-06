"""Bounded joint fanout adjustment; native verification is always required."""
from copy import deepcopy
import math
from pathlib import Path
from uuid import uuid4

import numpy as np
from scipy.optimize import minimize
from shapely.geometry import Point, LineString

from .board import read_board, _locked
from .repair_geometry import _read, _board, _field, _serialize
from .routing_clearance import pad_envelope
from .storage import digest


def relocate(source, output, vertices, reserved, *, net="+3.3V", diameter=.5, clearance=.15, max_move=.35):
    source, output = Path(source), Path(output)
    if output.exists() or output.resolve() == source.resolve():
        raise ValueError("Joint output must be new")
    if any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in (diameter,clearance,max_move)) or max_move > .5:
        raise ValueError("Invalid bounded physical constraints")
    if not 1 <= len(vertices) <= 8 or len(set(map(tuple,vertices))) != len(vertices):
        raise ValueError("One to eight unique fanout vertices required")
    if any(len(p) != 2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in p) for p in [*vertices,reserved]):
        raise ValueError("Finite XY coordinates required")
    ast, sha = _read(source)
    model = _board(ast,source=True)
    points = np.asarray(vertices,dtype=float)
    removed, splits = [], []
    for c in model["copper"]:
        g = c["geometry"]
        ends = [g["at"]] if g["kind"] == "via" else [g["start"],g["end"]]
        touched = [p for p in ends if p in vertices]
        if not touched:
            continue
        if _locked(c["node"]):
            raise ValueError("Locked copper at requested vertex")
        removed.append(c["id"])
        if g["kind"] == "segment" and len(touched) == 1 and math.dist(*ends) > 3:
            field = "start" if _field(c["node"],"start",2)[1:] in vertices else "end"
            far = "end" if field == "start" else "start"
            factor = 2 / math.dist(*ends)
            joint = [round(a+(b-a)*factor,6) for a,b in zip(_field(c["node"],field,2)[1:],_field(c["node"],far,2)[1:])]
            node = deepcopy(c["node"])
            identity = str(uuid4())
            _field(node,"uuid",1)[1] = identity
            _field(node,far,2)[1:] = joint
            _field(c["node"],field,2)[1:] = joint
            ast.append(node)
            splits.append(identity)
    model = _board(ast,source=True)
    selected = []
    for c in model["copper"]:
        g = c["geometry"]
        ends = [g["at"]] if g["kind"] == "via" else [g["start"],g["end"]]
        if any(p in vertices for p in ends):
            selected.append(c)
    if any(not any(p in ([c["geometry"]["at"]] if c["geometry"]["kind"] == "via" else [c["geometry"]["start"],c["geometry"]["end"]]) for c in selected) for p in vertices):
        raise ValueError("Vertex does not resolve to copper")
    selected_ids = {c["id"] for c in selected}
    layers = model["layers"]

    def shape(g, locations=None):
        def xy(p):
            return locations[vertices.index(p)] if locations is not None and p in vertices else p
        if g["kind"] == "via":
            return Point(xy(g["at"])),g["size"]/2,layers
        return LineString([xy(g["start"]),xy(g["end"])]),g["width"]/2,[g["layer"]]

    fixed = [(c["geometry"]["net"],*shape(c["geometry"])) for c in model["copper"] if c["id"] not in selected_ids]
    for f in read_board(source)["footprints"]:
        for p in f["pads"]:
            fixed.append((p["net"],pad_envelope(p),0,layers if "*.Cu" in p["layers"] else p["layers"]))
    reservation = Point(reserved)
    if any(n != net and set(span)&set(layers) and s.distance(reservation) < r+diameter/2+clearance-1e-9 for n,s,r,span in fixed):
        return {"status":"blocked","reason":"Reserved via conflicts with fixed copper"}
    fixed.append((None,reservation,diameter/2,layers))
    pairs, moving = [], []
    for i,c in enumerate(selected):
        g = c["geometry"]
        s,r,span = shape(g)
        for n,o,radius,other_span in fixed:
            required = r+radius+clearance+.000005
            if n != g["net"] and set(span)&set(other_span) and s.distance(o) <= required+max_move*math.sqrt(2):
                pairs.append((i,o,required))
        for j,d in enumerate(selected[:i]):
            _,rr,other_span = shape(d["geometry"])
            if g["net"] != d["geometry"]["net"] and set(span)&set(other_span):
                moving.append((i,j,r+rr+clearance+.000005))

    def slacks(delta):
        locations = points + delta.reshape(points.shape)
        shapes = [shape(c["geometry"],locations)[0] for c in selected]
        values = [shapes[i].distance(o)-r for i,o,r in pairs]
        values += [shapes[i].distance(shapes[j])-r for i,j,r in moving]
        values += [s.length-.001 for s,c in zip(shapes,selected) if c["geometry"]["kind"] == "segment"]
        for s,c in zip(shapes,selected):
            radius = shape(c["geometry"])[1]
            left,bottom,right,top = s.bounds
            values.extend([left-radius-model["outline"][0],bottom-radius-model["outline"][1],
                           model["outline"][2]-right-radius,model["outline"][3]-top-radius])
        return np.asarray(values)

    solutions, failures = [], []
    for seed in (np.zeros(points.size),np.tile([.1,.05],len(points)),np.tile([-.1,-.15],len(points))):
        trial = minimize(lambda d:float(d@d),seed,method="SLSQP",bounds=[(-max_move,max_move)]*points.size,
                         constraints={"type":"ineq","fun":slacks},options={"maxiter":250,"ftol":1e-11})
        locations = np.round(points+trial.x.reshape(points.shape),6)
        delta = (locations-points).ravel()
        values = slacks(delta)
        slack = float(min(values))
        # Six-decimal millimetre output rounds by up to 1 nm in distance;
        # the pair constraints already include an extra 5 nm safety margin.
        geometry_count = len(pairs)+len(moving)
        if (min(values[:geometry_count]) >= -.000001 and min(values[geometry_count:]) >= 0
                and max(abs(delta)) <= max_move):
            solutions.append((float(delta@delta),locations,slack))
        else:
            failures.append((slack,delta))
    if not solutions:
        slack,delta = max(failures,key=lambda row:row[0])
        values = slacks(delta)
        return {"status":"blocked","reason":"No legal bounded joint adjustment","best_minimum_slack_mm":slack,
                "trial_vertices":(points+delta.reshape(points.shape)).tolist(),
                "limiting_fixed":[{"item":selected[pairs[j][0]]["id"],"obstacle":pairs[j][1].wkt,"slack":float(values[j])}
                    for j in sorted(range(len(pairs)),key=lambda k:values[k])[:6]]}
    _,locations,slack = min(solutions,key=lambda row:row[0])
    for c in selected:
        g = c["geometry"]
        for field in (["at"] if g["kind"] == "via" else ["start","end"]):
            value = _field(c["node"],field,2)
            if value[1:] in vertices:
                value[1:] = locations[vertices.index(value[1:])].tolist()
    if digest(source) != sha:
        raise ValueError("Joint source changed")
    with output.open("xb") as stream:
        stream.write(_serialize(ast))
    return {"status":"proposed","changed_ids":sorted(removed),"split_near_ids":splits,
            "nets":sorted({c["geometry"]["net"] for c in selected}),"vertices_before":vertices,"vertices_after":locations.tolist(),
            "source_sha256":sha,"output_sha256":digest(output),"minimum_slack_mm":slack,
            "clearance_margin_mm":.000005,"quantization_tolerance_mm":.000001,
            "reserved_via":{"at":reserved,"diameter_mm":diameter},"max_axis_move_mm":max_move,
            "manufacturing_authorized":False,"requires_native_verification":True}
