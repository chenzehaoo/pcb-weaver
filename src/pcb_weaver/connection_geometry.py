"""Read-only copper contact diagnostics, never electrical or routing authorization."""
from collections import Counter
import math

from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from .board import read_board
from .clearance import pad_shape
from .repair_geometry import _read, _board


def contacts(path, findings):
    """Describe the native report's representative item pairs, not whole islands."""
    board = read_board(path)
    if board["unsupported"]:
        return [{"status": "unsupported", "reason": "Unsupported board geometry"} for _ in findings]
    try:
        parsed = _board(_read(path)[0], source=True)
    except ValueError as error:
        return [{"status": "unsupported", "reason": str(error)} for _ in findings]
    layers = set(parsed["layers"])
    index, counts = {}, Counter()
    for item in parsed["copper"]:
        g = item["geometry"]
        counts[g["net"]] += 1
        if g["kind"] == "segment":
            shape, radius, span = LineString([g["start"], g["end"]]), g["width"] / 2, {g["layer"]}
        else:
            shape, radius, span = Point(g["at"]), g["size"] / 2, layers
        index[item["id"]] = (g["net"], shape.buffer(radius / math.cos(math.pi / 128), quad_segs=32), span, g["kind"])
    for footprint in board["footprints"]:
        for pad in footprint["pads"]:
            if not pad["uuid"]:
                continue
            if pad["uuid"] in index:
                raise ValueError("Ambiguous physical item UUID")
            span = layers if "*.Cu" in pad["layers"] else layers.intersection(pad["layers"])
            index[pad["uuid"]] = (pad["net"], pad_shape(pad), span, "pad")
    results = []
    for finding in findings:
        items = finding.get("items", [])
        if len(items) != 2 or any(i.get("uuid") not in index for i in items):
            results.append({"status": "unresolved", "reason": "Requires two uniquely resolved copper items"})
            continue
        a, b = [index[i["uuid"]] for i in items]
        if not a[0] or a[0] != b[0] or not a[2] or not b[2]:
            results.append({"status": "unresolved", "reason": "Pair does not belong to one copper net"})
            continue
        shared = sorted(a[2] & b[2])
        points = nearest_points(a[1], b[1])
        distance = a[1].distance(b[1])
        kind = ("unrouted_net" if counts[a[0]] == 0 else "layer_transition" if not shared else
                "overlapping_projection" if distance <= 1e-6 else "same_layer_gap")
        results.append({"status": "ok", "net": a[0], "classification": kind,
                        "net_copper_items": counts[a[0]], "shared_layers": shared,
                        "pair_items": [{"uuid": item["uuid"], "kind": value[3], "layers": sorted(value[2]),
                                        "bounds_mm": list(value[1].bounds)} for item, value in zip(items, (a, b))],
                        "projected_gap_lower_bound_mm": distance,
                        "projected_nearest_points_mm": [[p.x, p.y] for p in points],
                        "coverage": "Conservative 2D envelopes of native representative items, not nearest connected islands. "
                                    "No obstacle search, zone fill, drill-void model or route feasibility proof.",
                        "route_feasibility": "not_evaluated", "manufacturing_authorized": False})
    return results
