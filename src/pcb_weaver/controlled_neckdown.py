"""Widen new power traces; retain bounded short pad escapes only when necessary."""
from pathlib import Path
from copy import deepcopy
import math
from uuid import uuid4

import shapely
from shapely.geometry import LineString, Point

from .board import read_board, _tag
from .repair_geometry import _read, _board, _field, _serialize
from .routing_clearance import pad_envelope, blocked_indices
from .storage import digest


def widen(source, routed, output, net, preferred, minimum, clearance, *, escape=.3, max_length=4, contacts=None, allow_bottlenecks=False):
    source, routed, output = Path(source), Path(routed), Path(output)
    if output.exists() or output.resolve() in {source.resolve(), routed.resolve()}:
        raise ValueError("Widening output must be new")
    if not 0 < minimum <= escape < preferred or clearance <= 0 or not 0 < max_length <= 4:
        raise ValueError("Invalid native width rules or neck budget")
    old_ast, source_sha = _read(source)
    ast, routed_sha = _read(routed)
    old, board = _board(old_ast, source=True), _board(ast, source=True)
    old_ids = {c["id"] for c in old["copper"]}
    old_nodes = {c["id"]:c["node"] for c in old["copper"]}
    if {c["id"]:c["node"] for c in board["copper"] if c["id"] in old_ids} != old_nodes:
        raise ValueError("Routing changed existing copper")
    if any(c["geometry"]["net"] != net for c in board["copper"] if c["id"] not in old_ids):
        raise ValueError("Unselected net was added")
    if [n for n in ast if _tag(n) not in {"segment","via"}] != [n for n in old_ast if _tag(n) not in {"segment","via"}]:
        raise ValueError("Routing changed noncopper geometry")
    model = read_board(routed)
    pads = [p for f in model["footprints"] for p in f["pads"]]
    landing_shapes = {layer:[pad_envelope(p).buffer(1.5) for p in pads if p["net"] == net
                    and (layer in p["layers"] or "*.Cu" in p["layers"])] for layer in board["layers"]}
    # Whole-island routing may connect to existing copper away from the native
    # report's representative pair. Only original copper grants landing regions.
    for c in old["copper"]:
        h = c["geometry"]
        if h["net"] != net:
            continue
        if h["kind"] == "via":
            shape,span = Point(h["at"]).buffer(h["size"]/2+1.5),board["layers"]
        else:
            shape,span = LineString([h["start"],h["end"]]).buffer(h["width"]/2+1.5),[h["layer"]]
        for layer in span:
            landing_shapes[layer].append(shape)
    for contact in contacts or []:
        for layer in contact["layers"]:
            landing_shapes[layer].append(Point(contact["at"]).buffer(1.5))
    zones = {layer:shapely.union_all(shapes) for layer,shapes in landing_shapes.items()}
    trees = {}
    for layer in board["layers"]:
        shapes, radii = [], []
        for c in board["copper"]:
            g = c["geometry"]
            if g["net"] == net:
                continue
            if g["kind"] == "via":
                shapes.append(Point(g["at"])); radii.append(g["size"]/2)
            elif g["layer"] == layer:
                shapes.append(LineString([g["start"],g["end"]])); radii.append(g["width"]/2)
        for p in pads:
            if p["net"] != net and (layer in p["layers"] or "*.Cu" in p["layers"]):
                shapes.append(pad_envelope(p)); radii.append(0)
        trees[layer] = (shapely.STRtree(shapes), radii)
    short, widened, replacements = [], [], {}
    for c in board["copper"]:
        if c["id"] in old_ids:
            continue
        g = c["geometry"]
        if g["kind"] != "segment":
            continue
        if g["width"] != escape:
            raise ValueError("Unexpected proposed escape width")
        line = LineString([g["start"],g["end"]])
        tree, radii = trees[g["layer"]]
        steps = max(1, math.ceil(line.length/.1))
        points = [tuple(round(a + (b-a)*i/steps,6) for a,b in zip(g["start"],g["end"])) for i in range(steps+1)]
        runs = []
        for a,b in zip(points,points[1:]):
            if a == b:
                continue
            piece = LineString([a,b])
            width = preferred
            if len(blocked_indices(tree,radii,[piece],clearance + preferred/2)):
                if (not allow_bottlenecks and not zones[g["layer"]].covers(piece)) or len(blocked_indices(tree,radii,[piece],clearance + escape/2)):
                    return {"status":"blocked","reason":"Narrow trace outside a pad escape or below required clearance",
                            "segment":c["id"],"piece":[a,b],"inside_escape":bool(zones[g["layer"]].covers(piece)),
                            "clearance_passed":not bool(len(blocked_indices(tree,radii,[piece],clearance + escape/2)))}
                width = escape
            if runs and runs[-1][2] == width:
                runs[-1] = (runs[-1][0],b,width)
            else:
                runs.append((a,b,width))
        nodes = []
        for i,(a,b,width) in enumerate(runs):
            node = deepcopy(c["node"])
            identity = c["id"] if i == 0 else str(uuid4())
            _field(node,"uuid",1)[1] = identity
            _field(node,"start",2)[1:] = a
            _field(node,"end",2)[1:] = b
            _field(node,"width",1)[1] = width
            nodes.append(node)
            if width == escape:
                short.append({"uuid":identity,"length_mm":math.dist(a,b),"layer":g["layer"],"width_mm":escape})
            else:
                widened.append(identity)
        replacements[id(c["node"])] = nodes
    length = sum(c["length_mm"] for c in short)
    if length > max_length:
        return {"status":"blocked","reason":"Total narrow escape length exceeds 4 mm budget", "escape_length_mm":length}
    if digest(source) != source_sha or digest(routed) != routed_sha:
        raise ValueError("Inputs changed during widening")
    ast = [child for node in ast for child in replacements.get(id(node),[node])]
    with output.open("xb") as stream:
        stream.write(_serialize(ast))
    return {"status":"proposed","escape_segments":short,"escape_length_mm":length,"widened_segments":len(widened),
            "preferred_width_mm":preferred,"minimum_rule_mm":minimum,"source_sha256":source_sha,
            "allow_bottlenecks":allow_bottlenecks,"maximum_escape_length_mm":max_length,
            "output_sha256":digest(output),"manufacturing_authorized":False,"requires_native_verification":True}
