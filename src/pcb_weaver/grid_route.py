"""Bounded additive proposals using GEOS obstacle envelopes and SciPy graph search."""
import math
from pathlib import Path
from uuid import uuid4

import numpy as np
import sexpdata
import shapely
from shapely.geometry import LineString, Point
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra, connected_components

from .board import read_board
from .clearance import pad_shape
from .repair_geometry import _read, _board, _serialize, _inside
from .storage import digest
from .routing_clearance import pad_envelope, blocked_indices


def propose(source, output, net, region, finding, rules, step=.1, *, exact_edges=False, reservations=None, refine=None, layer_costs=None, expand_terminals=False, allow_rule_boundary=False, extra_points=None, exact_terminals=False):
    source, output = Path(source), Path(output)
    if output.exists() or source.resolve() == output.resolve():
        raise ValueError("Grid output must be new")
    if (type(step) not in (float, int) or not .05 <= step <= .2 or
            not isinstance(region, list) or len(region) != 4 or
            any(type(v) not in (float, int) or not math.isfinite(v) for v in region) or
            region[0] >= region[2] or region[1] >= region[3]):
        raise ValueError("Invalid grid bounds/resolution")
    width, clearance, via_size, drill = [rules[k] for k in ("track_width", "clearance", "via_diameter", "via_drill")]
    if any(type(v) not in (float, int) or not math.isfinite(v) or v <= 0 for v in (width, clearance, via_size, drill)) or drill >= via_size:
        raise ValueError("Native positive track/clearance/via rules required")
    ast, sha = _read(source)
    pcb, model = _board(ast, source=True), read_board(source)
    if model["unsupported"] or not _inside(region, pcb["outline"]) or net not in pcb["codes"].values():
        raise ValueError("Unsupported geometry, net or region")
    layers = pcb["layers"]
    layer_costs = layer_costs or {}
    if (not isinstance(layer_costs, dict) or not set(layer_costs) <= set(layers) or
            any(type(v) not in (float, int) or not math.isfinite(v) or not 1 <= v <= 10 for v in layer_costs.values())):
        raise ValueError("Invalid routing layer costs")
    shapes, index = {layer: [] for layer in layers}, {}
    radii = {layer: [] for layer in layers}
    factor = 1 / math.cos(math.pi / 128)
    if type(exact_edges) is not bool:
        raise ValueError("exact_edges must be boolean")
    if type(exact_terminals) is not bool:
        raise ValueError("exact_terminals must be boolean")
    if type(allow_rule_boundary) is not bool or (allow_rule_boundary and not exact_edges):
        raise ValueError("Rule-boundary routing requires continuous edge checks")
    for reserved in reservations or []:
        points, diameter, layer = reserved["points"], reserved["width"], reserved["layer"]
        if (layer not in layers or type(diameter) not in (float, int) or not math.isfinite(diameter)
                or diameter <= 0 or len(points) < 2 or
                any(len(p) != 2 or any(type(v) not in (float, int) or not math.isfinite(v) for v in p) for p in points)):
            raise ValueError("Invalid reserved copper corridor")
        shapes[layer].append(LineString(points) if allow_rule_boundary else LineString(points).buffer(diameter / 2 * factor, quad_segs=32))
        radii[layer].append(diameter / 2 if allow_rule_boundary else 0)
    for item in pcb["copper"]:
        g = item["geometry"]
        if g["kind"] == "segment":
            core, radius = LineString([g["start"], g["end"]]), g["width"] / 2
            shape = core.buffer(radius * factor, quad_segs=32)
            span = [g["layer"]]
        else:
            core, radius = Point(g["at"]), g["size"] / 2
            shape = core.buffer(radius * factor, quad_segs=32)
            span = layers
        index[item["id"]] = (g["net"], shape, span)
        if g["net"] != net:
            for layer in span:
                shapes[layer].append(core if allow_rule_boundary else shape)
                radii[layer].append(radius if allow_rule_boundary else 0)
    for footprint in model["footprints"]:
        for pad in footprint["pads"]:
            shape = pad_envelope(pad) if allow_rule_boundary else pad_shape(pad)
            span = layers if "*.Cu" in pad["layers"] else [l for l in pad["layers"] if l in layers]
            if pad["uuid"]:
                index[pad["uuid"]] = (pad["net"], shape, span)
            if pad["net"] != net:
                for layer in span:
                    shapes[layer].append(shape)
                    radii[layer].append(0)
    items = finding["items"]
    if len(items) != 2 or any(not i.get("uuid") or i["uuid"] not in index for i in items):
        raise ValueError("Native pair must resolve to two physical items")
    terminals = [index[i["uuid"]] for i in items]
    if any(t[0] != net for t in terminals):
        raise ValueError("Pair belongs to another net")
    terminal_groups = [[terminal] for terminal in terminals]
    if type(expand_terminals) is not bool:
        raise ValueError("expand_terminals must be boolean")
    if expand_terminals:
        keys = [key for key, item in index.items() if item[0] == net]
        members = [index[key] for key in keys]
        tree = shapely.STRtree([item[1] for item in members])
        a, b = tree.query([item[1] for item in members], predicate="intersects")
        common = np.array([bool(set(members[i][2]) & set(members[j][2])) for i,j in zip(a,b)])
        graph = coo_matrix((np.ones(common.sum()), (a[common], b[common])), shape=(len(keys), len(keys))).tocsr()
        _, groups = connected_components(graph, directed=False)
        terminal_groups = [[member for j,member in enumerate(members) if groups[j] == groups[keys.index(item["uuid"])]] for item in items]
    pad_ids = {p["uuid"] for f in model["footprints"] for p in f["pads"] if p["uuid"]}
    anchor = next((item["pos"] for item in items if item["uuid"] in pad_ids), items[0]["pos"]) if allow_rule_boundary else items[0]["pos"]
    xs = anchor["x"] + np.arange(math.ceil((region[0] + width / 2 - anchor["x"]) / step),
                                  math.floor((region[2] - width / 2 - anchor["x"]) / step) + 1) * step
    ys = anchor["y"] + np.arange(math.ceil((region[1] + width / 2 - anchor["y"]) / step),
                                  math.floor((region[3] - width / 2 - anchor["y"]) / step) + 1) * step
    if allow_rule_boundary:
        # Exact pad centerlines can lie between uniform grid coordinates.
        xs = np.unique(np.round(np.concatenate([xs, [p["pos"]["x"] for p in items
                        if region[0]+width/2 <= p["pos"]["x"] <= region[2]-width/2]]), 6))
        ys = np.unique(np.round(np.concatenate([ys, [p["pos"]["y"] for p in items
                        if region[1]+width/2 <= p["pos"]["y"] <= region[3]-width/2]]), 6))
    if refine is not None:
        bounds, fine = refine["region"], refine["step"]
        if (not exact_edges or type(fine) not in (float, int) or not .01 <= fine < step
                or not isinstance(bounds, list) or len(bounds) != 4
                or any(type(v) not in (float, int) or not math.isfinite(v) for v in bounds)
                or bounds[0] >= bounds[2] or bounds[1] >= bounds[3] or not _inside(bounds, region)):
            raise ValueError("Refinement requires valid in-scope bounds and exact edges")
        axes = []
        for i, (axis, origin) in enumerate([(xs, anchor["x"]), (ys, anchor["y"])]):
            low = max(bounds[i], region[i] + width / 2)
            high = min(bounds[i + 2], region[i + 2] - width / 2)
            extra = origin + np.arange(math.ceil((low - origin) / fine), math.floor((high - origin) / fine) + 1) * fine
            axes.append(np.unique(np.round(np.concatenate([axis, extra]), 6)))
        xs, ys = axes
    if extra_points is not None:
        if (not exact_edges or not isinstance(extra_points,list) or len(extra_points) > 8 or
                any(not isinstance(p,list) or len(p) != 2 or
                    any(type(v) not in (int,float) or not math.isfinite(v) for v in p) or
                    not region[0]+width/2 <= p[0] <= region[2]-width/2 or
                    not region[1]+width/2 <= p[1] <= region[3]-width/2 for p in extra_points)):
            raise ValueError("Extra landing points require bounded finite coordinates and exact edges")
        xs = np.unique(np.round(np.concatenate([xs,[p[0] for p in extra_points]]),6))
        ys = np.unique(np.round(np.concatenate([ys,[p[1] for p in extra_points]]),6))
    count = len(xs) * len(ys)
    if not count or count * len(layers) > 2_500_000:
        raise ValueError("Grid exceeds bounded 2.5M-node budget")
    xx, yy = np.meshgrid(xs, ys)
    points = shapely.points(xx.ravel(), yy.ravel())
    free = np.ones((len(layers), count), dtype=bool)
    via_free = np.ones(count, dtype=bool)
    # Half-cell-diagonal inflation ensures continuous grid edges clear obstacles,
    # not merely their sampled vertices. Native DRC still evaluates actual rules.
    margin = 0 if allow_rule_boundary else .002
    inflation = margin if exact_edges else step / math.sqrt(2) + margin
    trees = []
    for n, layer in enumerate(layers):
        tree = shapely.STRtree(shapes[layer])
        trees.append(tree)
        if len(shapes[layer]):
            hits = blocked_indices(tree, radii[layer], points, clearance + width / 2 + inflation)
            free[n, hits] = False
            # A via is stationary at the sampled point, not swept along an edge.
            hits = blocked_indices(tree, radii[layer], points, clearance + via_size / 2 + margin)
            via_free[hits] = False
    via_free &= (xx.ravel() - via_size / 2 >= region[0]) & (xx.ravel() + via_size / 2 <= region[2])
    via_free &= (yy.ravel() - via_size / 2 >= region[1]) & (yy.ravel() + via_size / 2 <= region[3])
    ids = np.arange(count * len(layers), dtype=np.int32).reshape(len(layers), len(ys), len(xs))
    valid = free.reshape(ids.shape)
    rows, cols, weights = [], [], []
    directions = [(0, 1), (1, 0)] + ([(1, 1), (1, -1)] if exact_edges else [])
    coordinates = np.column_stack([xx.ravel(), yy.ravel()])
    for dy, dx in directions:
        y0, y1 = slice(0, len(ys) - dy), slice(dy, len(ys))
        x0 = slice(max(0, -dx), len(xs) - max(0, dx))
        x1 = slice(max(0, dx), len(xs) - max(0, -dx))
        for l, tree in enumerate(trees):
            mask = valid[l, y0, x0] & valid[l, y1, x1]
            a, b = ids[l, y0, x0][mask], ids[l, y1, x1][mask]
            # Chunk GEOS queries to bound memory; every continuous edge is checked.
            if exact_edges and len(shapes[layers[l]]):
                keep = np.ones(len(a), dtype=bool)
                for offset in range(0, len(a), 50000):
                    end = offset + 50000
                    lines = shapely.linestrings(np.stack([coordinates[a[offset:end] % count],
                                                          coordinates[b[offset:end] % count]], axis=1))
                    hits = blocked_indices(tree, radii[layers[l]], lines, clearance + width / 2 + margin)
                    keep[offset + hits] = False
                a, b = a[keep], b[keep]
            rows.append(a); cols.append(b)
            weights.append(np.linalg.norm(coordinates[a % count] - coordinates[b % count], axis=1) * layer_costs.get(layers[l], 1))
    landing = np.flatnonzero(via_free & free.all(axis=0)).astype(np.int32)
    for a in range(len(layers)):
        for b in range(a):
            rows.append(landing + a * count); cols.append(landing + b * count)
            weights.append(np.full(len(landing), 8.0))
    terminal_nodes = []
    for group in terminal_groups:
        nodes = []
        for l, layer in enumerate(layers):
            members = [shape for _,shape,span in group if layer in span]
            if members:
                shape = shapely.union_all(members)
                # The trace end-cap may land outside the pad centerline while
                # retaining positive copper overlap. This does not narrow the trace.
                contact_offset = 0 if exact_terminals else (width / 2 - .025 if allow_rule_boundary else -min(width / 4, .025))
                inside = shapely.covers(shape.buffer(contact_offset), points)
                nodes.append(np.flatnonzero(inside & free[l]) + l * count)
        terminal_nodes.append(np.concatenate(nodes).astype(np.int32) if nodes else np.array([], dtype=np.int32))
    result = {"status": "blocked", "net": net, "region": region, "step_mm": step,
              "source_sha256": sha, "solver": "Shapely GEOS + scipy.sparse.csgraph.dijkstra",
              "grid_nodes": int(ids.size), "rules": rules, "manufacturing_authorized": False,
              "exact_edges": exact_edges, "reservations": reservations or [], "refine": refine,
              "exact_terminals":exact_terminals,
              "layer_costs": layer_costs,
              "extra_points":extra_points,
              "allow_rule_boundary": allow_rule_boundary,
              "terminal_overlap_mm": .025 if allow_rule_boundary and not exact_terminals else None,
              "expand_terminals": expand_terminals, "terminal_component_items": [len(g) for g in terminal_groups],
              "terminal_nodes": [len(n) for n in terminal_nodes], "requires_native_verification": True}
    if any(len(n) == 0 for n in terminal_nodes):
        return {**result, "reason": "No legal grid terminal under conservative clearance envelopes"}
    start = ids.size
    rows.append(np.full(len(terminal_nodes[0]), start, dtype=np.int32)); cols.append(terminal_nodes[0])
    weights.append(np.zeros(len(terminal_nodes[0])))
    graph = coo_matrix((np.concatenate(weights), (np.concatenate(rows), np.concatenate(cols))), shape=(start + 1, start + 1)).tocsr()
    costs, prev = dijkstra(graph, directed=False, indices=start, return_predecessors=True)
    reachable = np.flatnonzero(np.isfinite(costs[:start]))
    result["reachable_nodes"] = len(reachable)
    result["reachable_layers"] = sorted({layers[i // count] for i in reachable})
    if len(reachable):
        xxr, yyr = xx.ravel()[reachable % count], yy.ravel()[reachable % count]
        result["reachable_bounds_mm"] = [float(xxr.min()), float(yyr.min()), float(xxr.max()), float(yyr.max())]
    end = int(terminal_nodes[1][np.argmin(costs[terminal_nodes[1]])])
    if not math.isfinite(costs[end]):
        return {**result, "reason": "No additive path within this grid, region and fixed obstacles"}
    path = [end]
    while prev[path[-1]] != start:
        path.append(int(prev[path[-1]]))
    path.reverse()
    coords = [(i // count, round(float(xs[(i % count) % len(xs)]), 6),
               round(float(ys[(i % count) // len(xs)]), 6)) for i in path]
    simple = []
    for p in coords:
        if len(simple) >= 2 and simple[-2][0] == simple[-1][0] == p[0]:
            a, b = simple[-2:]
            cross = (b[1] - a[1]) * (p[2] - b[2]) - (b[2] - a[2]) * (p[1] - b[1])
            dot = (b[1] - a[1]) * (p[1] - b[1]) + (b[2] - a[2]) * (p[2] - b[2])
            if abs(cross) < 1e-10 and dot > 0:
                simple.pop()
        simple.append(p)
    symbol = sexpdata.Symbol
    code = next(code for code, name in pcb["codes"].items() if name == net)
    additions, vias = [], set()
    for a, b in zip(simple, simple[1:]):
        if a[0] == b[0]:
            additions.append([symbol("segment"), [symbol("start"), *a[1:]], [symbol("end"), *b[1:]],
                [symbol("width"), width], [symbol("layer"), layers[a[0]]], [symbol("net"), code], [symbol("uuid"), str(uuid4())]])
        elif a[1:] not in vias:
            vias.add(a[1:])
            additions.append([symbol("via"), [symbol("at"), *a[1:]], [symbol("size"), via_size], [symbol("drill"), drill],
                [symbol("layers"), "F.Cu", "B.Cu"], [symbol("net"), code], [symbol("uuid"), str(uuid4())]])
    if not additions:
        return {**result, "reason": "No new copper proposed"}
    payload = _serialize(ast + additions)
    if digest(source) != sha:
        raise ValueError("Source changed during search")
    with output.open("xb") as stream:
        stream.write(payload)
    return {**result, "status": "proposed", "added_items": len(additions), "added_vias": len(vias),
            "output_sha256": digest(output), "path": simple, "weighted_cost": float(costs[end]),
            "trace_length_mm": sum(math.dist(a[1:], b[1:]) for a,b in zip(simple, simple[1:]) if a[0] == b[0])}
