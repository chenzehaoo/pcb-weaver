"""Bounded, topology-preserving trace-joint optimization; native DRC is mandatory."""
from copy import deepcopy
import math
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from shapely import affinity
from shapely.geometry import LineString, Point, box

from .board import _locked, read_board
from .repair_geometry import _read, _board, _field, _point, _inside, _bounds, _serialize
from .storage import digest


def pad_shape(pad):
    sx, sy = pad["size"]
    shape = box(-sx / 2, -sy / 2, sx / 2, sy / 2)
    radius = 0
    if pad["shape"] in {"circle", "oval"}:
        radius = min(sx, sy) / 2
    elif pad["shape"] == "roundrect":
        radius = min(sx, sy) * pad.get("roundrect_rratio", 0)
    if radius:
        dx, dy = max(0, sx / 2 - radius), max(0, sy / 2 - radius)
        core = box(-dx, -dy, dx, dy) if dx and dy else LineString([(-dx, -dy), (dx, dy)]) if dx or dy else Point(0, 0)
        shape = core.buffer(radius / math.cos(math.pi / 128), quad_segs=32)
    return affinity.translate(affinity.rotate(shape, -pad["rotation"], origin=(0, 0)), pad["x"], pad["y"])


def propose(source, output, nets, region, layer="F.Cu", clearance_mm=.15, max_move_mm=.08):
    """Move shared joints, never widths/vias/pads; return a non-authorizing proof."""
    if (type(max_move_mm) not in (int, float) or not math.isfinite(max_move_mm) or not 0 < max_move_mm <= .25 or
            type(clearance_mm) not in (int, float) or not math.isfinite(clearance_mm) or not 0 < clearance_mm <= 2):
        raise ValueError("Invalid clearance or bounded movement")
    if (not isinstance(region, list) or len(region) != 4 or
            any(type(x) not in (int, float) or not math.isfinite(x) for x in region) or
            region[0] >= region[2] or region[1] >= region[3]):
        raise ValueError("Invalid repair region")
    source, output = Path(source), Path(output)
    if output.exists() or source.resolve() == output.resolve():
        raise ValueError("Output must be a new candidate file")
    ast, sha = _read(source)
    baseline = deepcopy(ast)
    parsed = _board(ast, source=True)
    if (not isinstance(nets, list) or not 1 <= len(nets) <= 8 or len(set(nets)) != len(nets) or
            not set(nets).issubset(set(parsed["codes"].values()) - {""}) or layer not in parsed["layers"] or
            not _inside(region, parsed["outline"])):
        raise ValueError("Invalid nets, layer or board bounds")
    board = read_board(source)
    if board["unsupported"]:
        raise ValueError("Unsupported board geometry")
    selected = [c for c in parsed["copper"] if c["geometry"]["kind"] == "segment" and
                c["geometry"]["net"] in nets and c["geometry"]["layer"] == layer and
                _inside(c["geometry"]["bounds"], region) and not _locked(c["node"])]
    if not selected or len(selected) > 80:
        raise ValueError("Repair requires 1-80 in-scope unlocked segments")
    selected_ids = {c["id"] for c in selected}
    pads = [(p, pad_shape(p)) for f in board["footprints"] for p in f["pads"]
            if layer in p["layers"] or "*.Cu" in p["layers"]]
    vertices, endpoints = {}, []
    for c in selected:
        indices = []
        for end in ("start", "end"):
            p = _point(c["node"], end)
            key = (c["geometry"]["net"], *p)
            vertices.setdefault(key, len(vertices))
            indices.append(vertices[key])
        endpoints.append(indices)
    keys = list(vertices)
    base = np.array([k[1:] for k in keys], dtype=float)
    fixed = set()
    obstacles = []
    for c in parsed["copper"]:
        g = c["geometry"]
        if c["id"] in selected_ids:
            continue
        if g["kind"] == "segment" and g["layer"] != layer:
            continue
        shape = LineString([g["start"], g["end"]]) if g["kind"] == "segment" else Point(g["at"])
        radius = g["width"] / 2 if g["kind"] == "segment" else g["size"] / 2
        obstacles.append((g["net"], shape, radius))
        for i, key in enumerate(keys):
            if key[0] == g["net"] and shape.distance(Point(key[1:])) <= 1e-7:
                fixed.add(i)
    for i, key in enumerate(keys):
        degree = sum(i in ends for ends in endpoints)
        if any(p["net"] == key[0] and shape.distance(Point(key[1:])) <= 1e-6 and
               (degree == 1 or math.dist(key[1:], (p["x"], p["y"])) <= 1e-6) for p, shape in pads):
            fixed.add(i)
    obstacles.extend((p["net"], shape, 0) for p, shape in pads)
    free = [i for i in range(len(keys)) if i not in fixed]
    if not free:
        raise ValueError("No movable trace joints")
    base_lines = [LineString(base[ends]) for ends in endpoints]
    radii = [c["geometry"]["width"] / 2 for c in selected]
    variable_lines = {i for i, ends in enumerate(endpoints) if any(end in free for end in ends)}
    pairs, walls = [], []
    safety = .002
    for i, line in enumerate(base_lines):
        for j in range(i):
            if i not in variable_lines and j not in variable_lines:
                continue
            if selected[i]["geometry"]["net"] != selected[j]["geometry"]["net"]:
                if line.distance(base_lines[j]) < radii[i] + radii[j] + clearance_mm + max_move_mm * 3:
                    pairs.append((i, j))
        # Constant geometry cannot be optimized; native DRC still checks it in full.
        if i not in variable_lines:
            continue
        for net, shape, radius in obstacles:
            if net != selected[i]["geometry"]["net"] and line.distance(shape) < radii[i] + radius + clearance_mm + max_move_mm * 2:
                walls.append((i, shape, radii[i] + radius + clearance_mm + safety))

    def positions(x):
        points = base.copy()
        points[free] += np.asarray(x).reshape(-1, 2)
        return points

    def constraints(x):
        points = positions(x)
        lines = [LineString(points[ends]) for ends in endpoints]
        values = [lines[i].distance(lines[j]) - radii[i] - radii[j] - clearance_mm - safety for i, j in pairs]
        values.extend(lines[i].distance(shape) - required for i, shape, required in walls)
        values.extend(line.length - .001 for line in lines)
        for i, ends in enumerate(endpoints):
            for point in points[ends]:
                values.extend((point[0] - radii[i] - region[0], region[2] - point[0] - radii[i],
                               point[1] - radii[i] - region[1], region[3] - point[1] - radii[i]))
        return np.asarray(values)

    initial = np.zeros(2 * len(free))
    result = minimize(lambda x: np.dot(x, x), initial, method="SLSQP",
                      bounds=[(-max_move_mm, max_move_mm)] * len(initial),
                      constraints={"type": "ineq", "fun": constraints},
                      options={"maxiter": 150, "ftol": 1e-11})
    points = positions(result.x)
    points[free] = np.round(points[free], 6)
    snapped = (points[free] - base[free]).ravel()
    proof = {"status": "blocked", "source_sha256": sha, "nets": nets, "region": region, "layer": layer,
             "solver": "scipy-SLSQP / Shapely-GEOS", "solver_success": bool(result.success),
             "solver_message": str(result.message), "minimum_slack_mm": float(min(constraints(snapped))),
             "movable_joints": len(free), "selected_segments": len(selected), "clearance_mm": clearance_mm,
             "constant_segments": len(selected) - len(variable_lines),
             "max_axis_move_mm": max_move_mm, "requires_native_verification": True, "manufacturing_authorized": False}
    trial_lines = [LineString(points[ends]) for ends in endpoints]
    diagnostics = [{"uuid": selected[i]["id"], "obstacle_bounds": list(shape.bounds),
                    "slack_mm": trial_lines[i].distance(shape) - required,
                    "initial_slack_mm": base_lines[i].distance(shape) - required} for i, shape, required in walls]
    diagnostics += [{"uuid": selected[i]["id"], "other_uuid": selected[j]["id"],
                     "slack_mm": trial_lines[i].distance(trial_lines[j]) - radii[i] - radii[j] - clearance_mm - safety}
                    for i, j in pairs]
    proof["tightest_constraints"] = sorted(diagnostics, key=lambda row: row["slack_mm"])[:12]
    proof["fixed_joints"] = [keys[i] for i in sorted(fixed)]
    proof["free_joints"] = [keys[i] for i in free]
    if not np.all(np.isfinite(snapped)) or np.max(np.abs(snapped)) > max_move_mm + 1e-8 or min(constraints(snapped)) < -1e-6:
        return proof
    changes = []
    for c, ends in zip(selected, endpoints):
        before = deepcopy(c["node"])
        for name, index in zip(("start", "end"), ends):
            _field(c["node"], name, 2)[1:] = points[index].tolist()
        if c["node"] != before:
            if not _inside(_bounds(points[ends].tolist(), c["geometry"]["width"]), region):
                raise ValueError("Changed copper escaped region")
            changes.append({"uuid": c["id"], "before": {name: list(_point(before, name)) for name in ("start", "end")},
                            "after": {name: list(_point(c["node"], name)) for name in ("start", "end")}})
    payload = _serialize(ast)
    for c, old in zip(ast, baseline):
        if c == old:
            continue
        restored = deepcopy(c)
        for name in ("start", "end"):
            _field(restored, name, 2)[1:] = _field(old, name, 2)[1:]
        if restored != old:
            raise ValueError("Non-endpoint AST content changed")
    if digest(source) != sha:
        raise ValueError("Source changed during solve")
    with output.open("xb") as stream:
        stream.write(payload)
    proof.update(status="proposed", output_sha256=digest(output), changes=changes,
                 non_endpoint_ast_preserved=True, outside_scope_copper_preserved=True,
                 widths_and_vias_preserved=True)
    return proof
