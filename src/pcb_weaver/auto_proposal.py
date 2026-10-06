"""Isolated, bounded geometric proposals, never native acceptance or release."""
import argparse
import math
from copy import deepcopy
from pathlib import Path

from .board import read_board
from .controlled_neckdown import widen
from .grid_route import propose
from .joint_escape import relocate
from .repair_geometry import _read, _board
from .storage import read_json, write_json, digest


def route(source, target, request, adjustment=None):
    """Retry coarse failures near physical pads, within the same graph/time limits."""
    from .routing_clearance import pad_envelope
    kwargs = dict(step=.1, exact_edges=True, expand_terminals=request.get("expand_terminals",True),
                  exact_terminals=request.get("exact_terminals",False),
                  reservations=request.get("reservations"),
                  allow_rule_boundary=True,
                  extra_points=[adjustment["reserved_via"]["at"]] if adjustment else None)
    args = (source, target, request["net"], request["region"],
            request["finding"], request["rules"])
    result = propose(*args, **kwargs)
    attempts = []
    if result["status"] != "proposed" and request["strategy"] in {"expanded", "neckdown"}:
        wanted = {item["uuid"] for item in request["finding"]["items"]}
        region = request["region"]
        pads = [p for f in read_board(source)["footprints"] for p in f["pads"]
                if p["uuid"] in wanted and "*.Cu" not in p["layers"]]
        for pad in pads[:2]:
            bounds = pad_envelope(pad).bounds
            refined = [max(region[0], bounds[0]-1.5), max(region[1], bounds[1]-1.5),
                       min(region[2], bounds[2]+1.5), min(region[3], bounds[3]+1.5)]
            if refined[0] >= refined[2] or refined[1] >= refined[3]:
                continue
            attempts.append(result)
            fine = .01 if (region[2]-region[0])*(region[3]-region[1]) <= 500 else .025
            result = propose(*args, **kwargs, refine={"region": refined, "step": fine})
            if result["status"] == "proposed":
                break
    return {**result, "coarse_and_refinement_trials": attempts}


def landing_points(pad, rules, region, pads):
    """Deterministic pad-relative candidates, excluding immutable pad collisions."""
    from shapely.geometry import Point
    from .routing_clearance import pad_envelope
    bounds = pad_envelope(pad).bounds
    cx, cy = pad["x"], pad["y"]
    radius = rules["via_diameter"]/2
    obstacles = [pad_envelope(p) for p in pads if p["net"] != pad["net"]]
    points = []
    for gap in (.05, .1, .2, .3, .5, .8, 1.1):
        offset = radius + gap
        for shift in (0, -.1, .1, -.2, .2):
            for x, y in [(bounds[0]-offset,cy+shift),(bounds[2]+offset,cy+shift),
                         (cx+shift,bounds[1]-offset),(cx+shift,bounds[3]+offset)]:
                if not (region[0]+radius <= x <= region[2]-radius and
                        region[1]+radius <= y <= region[3]-radius):
                    continue
                point = Point(x, y)
                if any(shape.distance(point) < radius+rules["clearance"] for shape in obstacles):
                    continue
                candidate = [round(x, 6), round(y, 6)]
                if candidate not in points:
                    points.append(candidate)
    return points


def fanout(source, folder, request, evaluate=None):
    """Discover via landing/incident fanout candidates from actual target pads."""
    from shapely.geometry import Point, LineString
    copper = _board(_read(source)[0],source=True)["copper"]
    wanted = {item["uuid"] for item in request["finding"]["items"]}
    model = read_board(source)
    pads = [p for f in model["footprints"] for p in f["pads"]]
    targets = [p for p in pads if p["uuid"] in wanted and "*.Cu" not in p["layers"]]
    rules, region = request["rules"], request["region"]
    trials = []
    obstacles = []
    for c in copper:
        g = c["geometry"]
        if g["net"] != request["net"]:
            shape = Point(g["at"]) if g["kind"] == "via" else LineString([g["start"],g["end"]])
            obstacles.append((g, shape, g.get("size",g.get("width",0))/2))
    candidates = []
    for pad in targets:
        for x,y in landing_points(pad, rules, region, pads):
            point, vertices, deficits = Point(x,y), [], []
            for g, shape, radius in obstacles:
                deficit = radius+rules["via_diameter"]/2+rules["clearance"]-shape.distance(point)
                if deficit < -.15:
                    continue
                deficits.append(max(0, deficit))
                for vertex in ([g["at"]] if g["kind"] == "via" else [g["start"],g["end"]]):
                    if math.dist(vertex,[x,y]) <= 2 and vertex not in vertices:
                        vertices.append(vertex)
            if not 1 <= len(vertices) <= 8:
                continue
            if max(deficits, default=0) > .35*math.sqrt(2):
                continue
            score = sum(d*d for d in deficits)+.001*math.dist([x,y],[pad["x"],pad["y"]])
            candidates.append((score,x,y,vertices))
    for score,x,y,vertices in sorted(candidates, key=lambda c:c[:3])[:8]:
        output = folder / f"fanout-{len(trials)}.kicad_pcb"
        try:
            proof = relocate(source, output, vertices, [x,y], net=request["net"],
                             diameter=rules["via_diameter"],clearance=rules["clearance"])
        except ValueError as error:
            proof = {"status":"blocked","reason":str(error)}
        proof.update(landing_point=[x,y], candidate_score=score)
        trials.append(proof)
        if proof["status"] == "proposed":
            if evaluate is not None:
                proof["routing_trial"] = evaluate(output, proof)
                if proof["routing_trial"]["status"] != "proposed":
                    continue
            return output, proof, trials
    return source, None, trials


def run(request, folder):
    request = deepcopy(request)
    source = Path(request["source"])
    if digest(source) != request["source_sha256"]:
        raise ValueError("Proposal source hash changed")
    current, adjustment, trials = source, None, []
    compact = request.get("compact_via_policy") if request["strategy"] == "fanout" else None
    target = folder / "routed.kicad_pcb"
    result = None
    if compact:
        if digest(source.with_suffix(".kicad_pro")) != compact["project_sha256"]:
            raise ValueError("Compact via project rules changed")
        request["rules"]["via_diameter"] = compact["diameter_mm"]
        result = route(source, target, request)
    if request["strategy"] == "fanout":
        if result is None or result["status"] != "proposed":
            current, adjustment, trials = fanout(source, folder, request,
                evaluate=lambda board, proof: route(board, target, request, proof))
            if adjustment is None:
                return {"status":"blocked","reason":"No bounded fanout proposal","fanout_trials":trials,
                        "compact_via_policy":compact,"compact_route_trial":result}
            result = adjustment.pop("routing_trial")
    if result is None:
        result = route(current, target, request, adjustment)
    result.update(adjustment=adjustment,fanout_trials=trials,compact_via_policy=compact)
    if result["status"] != "proposed":
        return result
    if request["rules"]["track_width"] < request["preferred_width"]:
        restored = folder / "restored.kicad_pcb"
        proof = widen(current,target,restored,request["net"],request["preferred_width"],request["minimum_width"],
                      request["rules"]["clearance"],escape=request["rules"]["track_width"],allow_bottlenecks=True)
        result["width_restoration"] = proof
        if proof["status"] != "proposed":
            return {**result,"status":"blocked","reason":proof["reason"]}
        target = restored
    if digest(source) != request["source_sha256"]:
        raise ValueError("Source changed during proposal")
    return {**result,"output":target.name,"output_sha256":digest(target)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("request",type=Path)
    args = parser.parse_args()
    try:
        result = run(read_json(args.request),args.request.parent)
    except (ValueError,OSError,KeyError,TypeError,IndexError) as error:
        result = {"status":"blocked","reason":type(error).__name__+": "+str(error)}
    write_json(args.request.parent / "proposal.json", result)
