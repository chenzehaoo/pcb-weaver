"""Conservative KiCad board inspection and copy-only placement editing.

Coordinates are millimetres in KiCad's screen coordinate system. Both board faces
use conservative envelopes of closed straight contours, rectangles or circles.
Board outlines remain rectangular; copper declarations support 2/4/6/8 layers.
Unknown fields survive AST writes, but unknown physical geometry is reported.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
import math
from pathlib import Path
from typing import Any

import sexpdata


def _tag(node: Any) -> str:
    return str(node[0]) if isinstance(node, list) and node else ""


def _children(node: list, tag: str) -> list[list]:
    return [item for item in node[1:] if _tag(item) == tag]


def _child(node: list, tag: str, default: Any = None) -> Any:
    return next(iter(_children(node, tag)), default)


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a coordinate")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite board coordinate")
    return result


def _xy(node: list, tag: str) -> tuple[float, float]:
    item = _child(node, tag)
    if item is None or len(item) < 3:
        raise ValueError(f"Missing {tag} coordinates in {_tag(node)}")
    return _number(item[1]), _number(item[2])


def _at(node: list) -> tuple[float, float, float]:
    item = _child(node, "at", [None, 0, 0])
    return _number(item[1]), _number(item[2]), _number(item[3]) if len(item) > 3 else 0.0


def _layer(node: list) -> str:
    return str(_child(node, "layer", [None, ""])[1])


def _rotate(x: float, y: float, angle: float) -> tuple[float, float]:
    # Positive KiCad angles rotate counterclockwise on a screen with y down.
    radians = math.radians(angle)
    c, s = math.cos(radians), math.sin(radians)
    return c * x + s * y, -s * x + c * y


def _box(points: list[tuple[float, float]]) -> list[float]:
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def _corners(bounds: list[float]) -> list[tuple[float, float]]:
    a, b, c, d = bounds
    return [(a, b), (a, d), (c, b), (c, d)]


def _rectangle(nodes: list[list], prefix: str) -> list[float] | None:
    """Accept one rectangle or exactly four axis-aligned connected edges."""
    if len(nodes) == 1 and _tag(nodes[0]) == prefix + "rect":
        result = _box([_xy(nodes[0], "start"), _xy(nodes[0], "end")])
        return result if result[0] < result[2] and result[1] < result[3] else None
    if len(nodes) != 4 or any(_tag(n) != prefix + "line" for n in nodes):
        return None
    edges = [(_xy(n, "start"), _xy(n, "end")) for n in nodes]
    result = _box([point for edge in edges for point in edge])
    if result[0] >= result[2] or result[1] >= result[3]:
        return None
    a, b, c, d = result
    expected = {frozenset(edge) for edge in [((a, b), (c, b)), ((c, b), (c, d)),
                                             ((c, d), (a, d)), ((a, d), (a, b))]}
    return result if {frozenset(e) for e in edges} == expected else None


def _simple_polygon(points: list[tuple[float, float]]) -> bool:
    if len(points) < 3 or len(set(points)) != len(points):
        return False
    edges = list(zip(points, points[1:] + points[:1]))
    if abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) < 1e-12:
        return False

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    for i, (a, b) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            c, d = edges[j]
            if max(a[0], b[0]) < min(c[0], d[0]) or max(c[0], d[0]) < min(a[0], b[0]):
                continue
            if max(a[1], b[1]) < min(c[1], d[1]) or max(c[1], d[1]) < min(a[1], b[1]):
                continue
            if cross(a, b, c) * cross(a, b, d) <= 0 and cross(c, d, a) * cross(c, d, b) <= 0:
                return False
    return True


def _courtyard(nodes: list[list]) -> tuple[list[float] | None, int]:
    """Enclose closed simple contours; never interpret an open stroke as a body.

    Disjoint contours and concave polygons deliberately include their empty space
    in the envelope. This can reject valid packing but cannot hide a protrusion.
    """
    points, segments = [], []
    contours = 0
    for node in nodes:
        kind = _tag(node)
        if kind == "fp_rect":
            bounds = _rectangle([node], "fp_")
            if bounds is None:
                return None, 0
            points.extend(_corners(bounds))
            contours += 1
        elif kind == "fp_circle":
            centre = _xy(node, "center")
            radius = math.dist(centre, _xy(node, "end"))
            if radius <= 0:
                return None, 0
            points.extend(_corners([centre[0] - radius, centre[1] - radius,
                                    centre[0] + radius, centre[1] + radius]))
            contours += 1
        elif kind == "fp_poly":
            pts = _child(node, "pts", [])
            if any(_tag(p) != "xy" or len(p) != 3 for p in pts[1:]):
                return None, 0
            polygon = [(_number(p[1]), _number(p[2])) for p in pts[1:]]
            if polygon and polygon[0] == polygon[-1]:
                polygon.pop()
            if not _simple_polygon(polygon):
                return None, 0
            points.extend(polygon)
            contours += 1
        elif kind == "fp_line":
            segments.append((_xy(node, "start"), _xy(node, "end")))
        else:
            return None, 0
    adjacent: dict[tuple, list] = defaultdict(list)
    for a, b in segments:
        if a == b:
            return None, 0
        adjacent[a].append(b)
        adjacent[b].append(a)
    if any(len(neighbours) != 2 or len(set(neighbours)) != 2 for neighbours in adjacent.values()):
        return None, 0
    remaining = set(adjacent)
    while remaining:
        start = min(remaining)
        polygon, previous, current = [], None, start
        while current not in polygon:
            polygon.append(current)
            neighbours = adjacent[current]
            following = neighbours[0] if neighbours[0] != previous else neighbours[1]
            previous, current = current, following
        if current != start or not _simple_polygon(polygon):
            return None, 0
        remaining.difference_update(polygon)
        points.extend(polygon)
        contours += 1
    return (_box(points), contours) if points else (None, 0)


def copper_layer_issues(names: list[str] | None) -> list[str]:
    """Check canonical copper names without assuming version-specific layer IDs."""
    if not names:
        return ["missing copper layer table"]
    if len(names) not in {2, 4, 6, 8}:
        return ["only 2/4/6/8 declared copper layers are supported"]
    expected = {"F.Cu", "B.Cu", *(f"In{i}.Cu" for i in range(1, len(names) - 1))}
    if len(set(names)) != len(names) or set(names) != expected:
        return ["copper layer names must be unique F.Cu/B.Cu and consecutive InN.Cu"]
    return []


def collision_boxes(first: dict, second: dict):
    """Yield material envelope pairs, without colliding opposite SMD bodies.

    A through-hole pad reserves its entire copper/pad AABB on both faces,
    including NPTH holes. Space between separate pins is not filled in. This is
    conservative placement geometry, not a 3D lead or electrical clearance test.
    Legacy dictionaries without hole metadata fall back to full-body occupancy;
    auditing also marks that missing evidence unknown for two-sided boards.
    """
    if first["layer"] == second["layer"]:
        yield first["bounds"], second["bounds"]
    else:
        for bounds in first.get("through_hole_bounds", [first["bounds"]]):
            yield bounds, second["bounds"]
        for bounds in second.get("through_hole_bounds", [second["bounds"]]):
            yield first["bounds"], bounds


_AST_CACHE_CHAR_LIMIT = 2_000_000


@lru_cache(maxsize=2)
def _cached_ast(text: str) -> list:
    return sexpdata.loads(text, nil=None, true=None, false=None)


def _clone_ast(value):
    if type(value) is list:
        return [_clone_ast(item) for item in value]
    if type(value) is sexpdata.Symbol:
        return sexpdata.Symbol(value.value())
    if type(value) in (str,int,float,bool,type(None)):
        return value
    return deepcopy(value)


def _parse_ast(text: str) -> list:
    # Content-bound and copy-on-read: neither file edits nor AST mutations can
    # reuse stale geometry. Large inputs bypass the small per-process cache.
    if len(text) > _AST_CACHE_CHAR_LIMIT:
        return sexpdata.loads(text, nil=None, true=None, false=None)
    return _clone_ast(_cached_ast(text))


def _load(path: str | Path) -> list:
    ast = _parse_ast(Path(path).read_text(encoding="utf-8-sig"))
    if _tag(ast) != "kicad_pcb":
        raise ValueError("Expected a kicad_pcb S-expression")
    return ast


def _reference(node: list) -> str:
    values = [str(p[2]) for p in _children(node, "property") if str(p[1]) == "Reference"]
    values += [str(p[2]) for p in _children(node, "fp_text") if str(p[1]) == "reference"]
    if not values or not values[0] or len(set(values)) != 1:
        raise ValueError("Missing or conflicting footprint reference")
    return values[0]


def _locked(node: list) -> bool:
    item = _child(node, "locked")
    return any(not isinstance(x, list) and str(x) == "locked" for x in node[1:]) or (
        item is not None and (len(item) == 1 or str(item[1]) not in {"no", "false", "0"}))


def _inspect(ast: list) -> dict:
    unsupported: list[str] = []
    net_codes: dict[str, str] = {}
    for node in _children(ast, "net"):
        code, name = str(node[1]), str(node[2])
        if code in net_codes and net_codes[code] != name:
            raise ValueError(f"Conflicting net code {code}")
        net_codes[code] = name
    edge_nodes = [n for n in ast[1:] if isinstance(n, list) and _layer(n) == "Edge.Cuts"]
    outline = _rectangle(edge_nodes, "gr_")
    if outline is None:
        unsupported.append("outline: requires one closed, axis-aligned rectangle; cutouts/arcs unsupported")
    footprints = []
    references: set[str] = set()
    for node in _children(ast, "footprint") + _children(ast, "module"):
        reference = _reference(node)
        if reference in references:
            raise ValueError(f"Duplicate footprint reference: {reference}")
        references.add(reference)
        x, y, angle = _at(node)
        layer = _layer(node)
        if layer not in {"F.Cu", "B.Cu"}:
            unsupported.append(f"{reference}: unknown footprint layer unsupported")
        value = next((str(p[2]) for p in _children(node, "property") if str(p[1]) == "Value"), "")
        if not value:
            value = next((str(p[2]) for p in _children(node, "fp_text") if str(p[1]) == "value"), "")
        courtyard_nodes = [n for n in node[1:] if isinstance(n, list) and _layer(n).endswith("CrtYd")]
        if any(_layer(n) != layer.replace(".Cu", ".CrtYd") for n in courtyard_nodes):
            unsupported.append(f"{reference}: courtyard side disagrees with footprint side")
        courtyard, contour_count = _courtyard(courtyard_nodes)
        if courtyard is None:
            unsupported.append(f"{reference}: missing, open, self-intersecting or unsupported courtyard")
        points = []
        if courtyard is not None:
            # Include stroke thickness in the conservative component envelope.
            widths = [_number(_child(_child(n, "stroke", []), "width", [None, 0])[1])
                      if _child(n, "stroke") else _number(_child(n, "width", [None, 0])[1])
                      for n in courtyard_nodes]
            if any(width < 0 for width in widths):
                raise ValueError(f"{reference}: negative courtyard stroke width")
            half = max(widths, default=0) / 2
            courtyard = [courtyard[0] - half, courtyard[1] - half, courtyard[2] + half, courtyard[3] + half]
            for u, v in _corners(courtyard):
                u, v = _rotate(u, v, angle)
                points.append((x + u, y + v))
        pads, through_hole_bounds = [], []
        for pad in _children(node, "pad"):
            number = str(pad[1])
            pad_type = str(pad[2])
            if pad_type not in {"smd", "connect", "thru_hole", "np_thru_hole"}:
                unsupported.append(f"{reference}.{number}: unsupported pad type {pad_type}")
            px, py, pad_angle = _at(pad)
            # pcbnew SaveBoard serializes already-flipped local coordinates on B.Cu.
            # Applying another mirror here would move real back-side pads incorrectly.
            dx, dy = _rotate(px, py, angle)
            absolute_x, absolute_y = x + dx, y + dy
            net = _child(pad, "net")
            name = ""
            if net is not None:
                # Newer files may contain names directly, older files code + name.
                name = str(net[2]) if len(net) > 2 else net_codes.get(str(net[1]), "")
                if len(net) == 2 and str(net[1]) not in net_codes:
                    if isinstance(net[1], str):
                        name = net[1]
                    else:
                        unsupported.append(f"{reference}.{number}: unresolved net code {net[1]}")
                if len(net) > 2 and str(net[1]) in net_codes and net_codes[str(net[1])] != name:
                    raise ValueError(f"Net code/name mismatch at {reference}.{number}")
            sx, sy = _xy(pad, "size")
            if sx <= 0 or sy <= 0:
                raise ValueError(f"Invalid pad size at {reference}.{number}")
            if str(pad[3]) not in {"rect", "circle", "oval", "roundrect"}:
                unsupported.append(f"{reference}.{number}: unsupported pad shape {pad[3]}")
            if _child(pad, "offset") or _child(pad, "primitives") or _child(pad, "padstack"):
                unsupported.append(f"{reference}.{number}: pad offset/primitives/padstack unsupported")
            drill = _child(pad, "drill")
            if drill is not None:
                if _child(drill, "offset"):
                    unsupported.append(f"{reference}.{number}: offset drill geometry unsupported")
                dimensions = [_number(value) for value in drill[1:]
                              if isinstance(value, (int, float))]
                if not dimensions or any(value <= 0 for value in dimensions):
                    raise ValueError(f"Invalid pad drill at {reference}.{number}")
                oval = any(str(value) == "oval" for value in drill[1:] if not isinstance(value, list))
                if len(dimensions) != (2 if oval else 1):
                    raise ValueError(f"Malformed pad drill dimensions at {reference}.{number}")
                drill_size = dimensions if oval else dimensions * 2
                if drill_size[0] > sx + 1e-9 or drill_size[1] > sy + 1e-9:
                    unsupported.append(f"{reference}.{number}: drill envelope exceeds pad envelope")
                if pad_type not in {"thru_hole", "np_thru_hole"}:
                    unsupported.append(f"{reference}.{number}: drill on non-through-hole pad unsupported")
            elif pad_type in {"thru_hole", "np_thru_hole"}:
                unsupported.append(f"{reference}.{number}: through-hole pad missing drill")
            # Pad angles in board files are absolute; positions are footprint-local.
            pad_points = []
            for u, v in _corners([-sx / 2, -sy / 2, sx / 2, sy / 2]):
                u, v = _rotate(u, v, pad_angle)
                pad_points.append((absolute_x + u, absolute_y + v))
            points.extend(pad_points)
            if drill is not None:
                through_hole_bounds.append(_box(pad_points))
            metadata = {"number": number, "net": name, "x": absolute_x, "y": absolute_y,
                        "size": [sx, sy], "shape": str(pad[3]), "rotation": pad_angle,
                        "layers": [str(v) for v in _child(pad, "layers", [])[1:]], "type": pad_type}
            metadata.update({
                "uuid": str(_child(pad, "uuid", _child(pad, "tstamp", [None, ""]))[1]) or None,
                "pin_function": str(_child(pad, "pinfunction", [None, ""])[1]) or None,
                "pin_type": str(_child(pad, "pintype", [None, ""])[1]) or None,
            })
            copper = [v for v in metadata["layers"] if v.endswith(".Cu")]
            if pad_type in {"smd", "connect"} and copper != [layer]:
                unsupported.append(f"{reference}.{number}: surface pad copper side disagrees with footprint")
            if pad_type in {"thru_hole", "np_thru_hole"} and "*.Cu" not in copper:
                unsupported.append(f"{reference}.{number}: nonstandard through-hole layer span unsupported")
            if drill is not None:
                metadata["drill_mm"] = min(dimensions)
                if len(dimensions) == 2:
                    metadata["drill_size"] = dimensions
            ratio = _child(pad, "roundrect_rratio")
            if ratio is not None:
                metadata["roundrect_rratio"] = _number(ratio[1])
            pads.append(metadata)
        identities: dict[str, set[str]] = defaultdict(set)
        for pad in pads:
            if pad["number"]:
                identities[pad["number"]].add(pad["net"])
            elif pad["net"]:
                unsupported.append(f"{reference}: connected pad has no electrical number")
        for number, names in identities.items():
            if len(names) > 1:
                unsupported.append(f"{reference}.{number}: ambiguous duplicate pad net identity")
        physical = {"zone", "rule_area", "fp_arc", "fp_curve"}
        for child in node[1:]:
            if _tag(child) in physical and not _layer(child).endswith(("SilkS", "Fab")):
                unsupported.append(f"{reference}: unsupported geometry {_tag(child)}")
            if _tag(child).startswith("fp_") and _layer(child).endswith(".Cu"):
                unsupported.append(f"{reference}: footprint copper graphics unsupported")
            if isinstance(child, list) and _layer(child) == "Edge.Cuts":
                unsupported.append(f"{reference}: footprint-local board cutout unsupported")
        footprints.append({"reference": reference, "value": value, "footprint": str(node[1]),
                           "properties": {str(p[1]): str(p[2]) for p in _children(node, "property")},
                           "property_items": [{"name": str(p[1]), "value": str(p[2])}
                                              for p in _children(node, "property")],
                           "uuid": str(_child(node, "uuid", _child(node, "tstamp", [None, ""]))[1]) or None,
                           "dnp": (any(str(a) == "dnp" for a in _child(node, "attr", [])[1:])
                                   or str(_child(node, "dnp", [None, "no"])[1]) == "yes"),
                           "x": x, "y": y, "rotation": angle, "layer": layer, "locked": _locked(node),
                           "pads": pads, "bounds": _box(points) if points else [x, y, x, y],
                           "through_hole_bounds": through_hole_bounds,
                           "courtyard": {"supported": courtyard is not None,
                                         "model": "conservative_aabb", "contours": contour_count}})
    nets: dict[str, list] = defaultdict(list)
    for fp in footprints:
        for pad in fp["pads"]:
            if pad["net"]:
                nets[pad["net"]].append({"reference": fp["reference"], "number": pad["number"],
                                         "x": pad["x"], "y": pad["y"]})
    segments, arcs, vias = _children(ast, "segment"), _children(ast, "arc"), _children(ast, "via")
    for n in ast[1:]:
        if _tag(n) in {"zone", "rule_area", "group", "generated"}:
            unsupported.append(f"board: {_tag(n)} geometry/placement semantics unsupported")
        if _tag(n).startswith("gr_") and _layer(n).endswith(".Cu"):
            unsupported.append(f"board: copper graphics {_tag(n)} unsupported")
    if arcs:
        unsupported.append("board: routed arc geometry unsupported")
    def route_net(node: list) -> str:
        token = _child(node, "net", [None, 0])[1]
        if str(token) in net_codes:
            return net_codes[str(token)]
        if isinstance(token, str):
            return token
        if token != 0:
            unsupported.append(f"board: unresolved routed net code {token}")
        return ""

    tracks_data = [{"net": route_net(s),
                    "width_mm": _number(_child(s, "width")[1]),
                    "start": list(_xy(s, "start")), "end": list(_xy(s, "end")), "layer": _layer(s),
                    "length_mm": math.dist(_xy(s, "start"), _xy(s, "end"))} for s in segments]
    via_items = [{"net": route_net(v), "drill_mm": _number(_child(v, "drill")[1]),
                  "x": _at(v)[0], "y": _at(v)[1],
                  "diameter_mm": _number(_child(v, "size")[1]),
                  "layers": [str(layer) for layer in _child(v, "layers", [])[1:]]} for v in vias]
    layers = _child(ast, "layers", [])
    copper_layers = [str(n[1]) for n in layers[1:] if isinstance(n, list) and len(n) > 1 and str(n[1]).endswith(".Cu")]
    unsupported.extend("board: " + issue for issue in copper_layer_issues(copper_layers))
    layer_rows = [n for n in layers[1:] if isinstance(n, list)]
    identifiers = [n[0] for n in layer_rows if n]
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in identifiers) or len(set(identifiers)) != len(identifiers):
        unsupported.append("board: malformed or duplicate layer ordinals")
    for row in layer_rows:
        if len(row) > 1 and str(row[1]).endswith(".Cu") and (len(row) < 3 or str(row[2]) not in {"signal", "power", "mixed", "jumper"}):
            unsupported.append("board: invalid copper layer type")
    stackup = _child(_child(ast, "setup", []), "stackup")
    stackup_copper = None if stackup is None else [str(n[1]) for n in _children(stackup, "layer") if str(n[1]).endswith(".Cu")]
    if stackup_copper is not None and (len(stackup_copper) != len(copper_layers) or set(stackup_copper) != set(copper_layers)):
        unsupported.append("board: stackup copper layers disagree with declared layer table")
    if stackup_copper and stackup_copper != ["F.Cu", *(f"In{i}.Cu" for i in range(1, len(copper_layers) - 1)), "B.Cu"]:
        unsupported.append("board: unsupported stackup copper layer ordering")
    for fp in footprints:
        for pad in fp["pads"]:
            if any(layer.endswith(".Cu") and layer not in {"*.Cu", "F&B.Cu", *copper_layers} for layer in pad["layers"]):
                unsupported.append(f"{fp['reference']}.{pad['number']}: pad uses undeclared copper layer")
    for track in tracks_data:
        if track["layer"] not in copper_layers:
            unsupported.append("board: track uses undeclared copper layer")
    for via in via_items:
        if len(via["layers"]) != 2 or any(layer not in copper_layers for layer in via["layers"]):
            unsupported.append("board: via uses undeclared or invalid copper layer span")
    return {"version": _child(ast, "version", [None, None])[1], "footprints": footprints,
            "nets": [{"name": name, "pads": pads} for name, pads in sorted(nets.items())],
            "outline": {"bounds": outline or [0.0, 0.0, 0.0, 0.0], "supported": outline is not None},
            "tracks": len(segments) + len(arcs), "vias": len(vias), "unsupported": sorted(set(unsupported)),
            "copper_layers": copper_layers, "track_items": tracks_data,
            "stackup_copper_layers": stackup_copper,
            "via_items": via_items,
            "routed_length_mm": sum(t["length_mm"] for t in tracks_data) if not arcs else None}


def read_board(path: str | Path) -> dict:
    """Read a KiCad AST, retaining explicit uncertainty about physical geometry."""
    return _inspect(_load(path))


def write_placements(source: str | Path, destination: str | Path, placements: list[dict]) -> dict:
    """Write a new board copy; never edit the source or an existing destination.

    Translation only: rotations/side changes need KiCad's native transformation
    engine to update all nested orientation fields and are deliberately rejected.
    """
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError("Destination must be a new copy, distinct from source")
    ast = _load(source)
    board = _inspect(ast)
    by_ref = {fp["reference"]: fp for fp in board["footprints"]}
    updates = {}
    for placement in placements:
        reference = placement["reference"]
        if reference not in by_ref or reference in updates:
            raise ValueError(f"Unknown or repeated placement reference: {reference}")
        if set(placement) - {"reference", "x", "y", "rotation"}:
            raise ValueError(f"Unsupported placement fields for {reference}")
        fp = by_ref[reference]
        x, y = _number(placement["x"]), _number(placement["y"])
        angle = _number(placement.get("rotation", fp["rotation"]))
        if not math.isclose((angle - fp["rotation"]) % 360, 0, abs_tol=1e-9):
            raise ValueError("Rotation editing unsupported; retain original rotation")
        if (x, y) != (fp["x"], fp["y"]):
            if fp["locked"]:
                raise ValueError(f"Locked footprint cannot move: {reference}")
            # KiCad persists integer nanometres. Quantize before the caller's
            # post-write constraint audit, not during later routing import.
            updates[reference] = (round(x, 6), round(y, 6))
        else:
            updates[reference] = None
    if any(value is not None for value in updates.values()):
        if board["tracks"] or board["vias"]:
            raise ValueError("Routed-board placement changes are rejected")
        if board["unsupported"]:
            raise ValueError("Unsupported geometry: " + "; ".join(board["unsupported"]))
    for node in _children(ast, "footprint") + _children(ast, "module"):
        update = updates.get(_reference(node))
        if update is None:
            continue
        at = _child(node, "at")
        if at is None:
            node.append([sexpdata.Symbol("at"), *update])
        else:
            at[1:3] = update
    result = _inspect(ast)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(sexpdata.dumps(ast) + "\n")
    return result


def compare_boards(before: str | Path | dict, after: str | Path | dict) -> dict:
    """Distinguish pose changes from connectivity changes by (reference, pad).

    Repeated physical pads with the same number are one electrical identity;
    conflicting assignments are reported as ambiguous, never silently collapsed.
    """
    before = read_board(before) if not isinstance(before, dict) else before
    after = read_board(after) if not isinstance(after, dict) else after

    def index(board: dict) -> tuple[dict, dict]:
        fps, connections = {}, defaultdict(set)
        for fp in board["footprints"]:
            ref = fp["reference"]
            if ref in fps:
                raise ValueError(f"Duplicate footprint reference: {ref}")
            fps[ref] = fp
            for pad in fp["pads"]:
                if pad["number"]:
                    connections[(ref, pad["number"])].add(pad["net"])
        return fps, connections

    old, old_nets = index(before)
    new, new_nets = index(after)
    moved, modified = [], []
    for ref in sorted(old.keys() & new.keys()):
        a, b = old[ref], new[ref]
        pose = ("x", "y", "rotation", "layer")
        if any(a[k] != b[k] for k in pose):
            moved.append({"reference": ref, "before": {k: a[k] for k in pose},
                          "after": {k: b[k] for k in pose},
                          "distance_mm": math.hypot(b["x"] - a["x"], b["y"] - a["y"])})
        fields = [k for k in ("value", "footprint", "locked") if a[k] != b[k]]
        if fields:
            modified.append({"reference": ref, "fields": fields})
    changes = []
    for key in sorted(old_nets.keys() | new_nets.keys()):
        if old_nets.get(key) != new_nets.get(key):
            changes.append({"reference": key[0], "number": key[1],
                            "before": sorted(old_nets[key]) if key in old_nets else None,
                            "after": sorted(new_nets[key]) if key in new_nets else None})
    ambiguous = [f"{ref}.{number}" for ref, number in sorted(old_nets.keys() | new_nets.keys())
                 if len(old_nets.get((ref, number), set())) > 1 or len(new_nets.get((ref, number), set())) > 1]
    return {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
            "moved": moved, "modified": modified, "connection_changes": changes,
            "connectivity_changed": bool(changes), "ambiguous_pad_identities": ambiguous,
            "unsupported": sorted(set(before.get("unsupported", []) + after.get("unsupported", [])))}
