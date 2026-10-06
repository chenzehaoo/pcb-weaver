"""Copy-only local copper repair, without native APIs or sidecar handling.

Only straight segments and ordinary through vias are supported. Outlines use
board.py's exact rectangle/four-edge contract, including rejection of cutouts.
Manifests are JSON values with a consistency checksum, not authorization tokens;
callers must keep their approved manifest trusted across the routing operation.
"""

from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
from uuid import UUID, uuid4

import sexpdata

from .board import _children, _locked, _parse_ast, _rectangle, _tag, copper_layer_issues


_COPPER = {"segment", "via", "arc"}
_IDENTITY = {"uuid", "tstamp"}


def _field(node: list, name: str, size: int | None = None) -> list:
    values = _children(node, name)
    if len(values) != 1 or (size is not None and len(values[0]) != size + 1):
        raise ValueError(f"Missing, duplicate or malformed {name} in {_tag(node)}")
    return values[0]


def _number(value) -> float:
    if type(value) not in {int, float}:
        raise ValueError("Non-finite or invalid numeric geometry")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("Non-finite or invalid numeric geometry") from exc
    if not math.isfinite(number):
        raise ValueError("Non-finite or invalid numeric geometry")
    return number if number else 0.0


def _point(node: list, name: str) -> tuple[float, float]:
    return tuple(_number(v) for v in _field(node, name, 2)[1:])


def _bounds(points: list[tuple[float, float]], diameter: float) -> list[float]:
    # Use exact decimal rationals, then round outward, so float cancellation
    # cannot make a tiny positive copper protrusion appear inside the region.
    points = [(Fraction(str(x)), Fraction(str(y))) for x, y in points]
    radius = Fraction(str(diameter)) / 2
    exact = [min(p[0] for p in points) - radius, min(p[1] for p in points) - radius,
             max(p[0] for p in points) + radius, max(p[1] for p in points) + radius]
    result = []
    for index, value in enumerate(exact):
        try:
            rounded = float(value)
        except OverflowError as exc:
            raise ValueError("Non-finite copper envelope") from exc
        if not math.isfinite(rounded):
            raise ValueError("Non-finite copper envelope")
        approximation = Fraction.from_float(rounded)
        if index < 2 and approximation > value:
            rounded = math.nextafter(rounded, -math.inf)
        elif index >= 2 and approximation < value:
            rounded = math.nextafter(rounded, math.inf)
        result.append(rounded)
    return result


def _walk(node):
    if isinstance(node, list):
        yield node
        for child in node[1:]:
            yield from _walk(child)


def _identity(node: list, required: bool = True) -> str | None:
    fields = [c for c in node[1:] if _tag(c) in _IDENTITY]
    if not fields and not required:
        return None
    if len(fields) != 1 or len(fields[0]) != 2:
        raise ValueError("Missing or ambiguous copper UUID")
    value = str(fields[0][1])
    if not value:
        raise ValueError("Empty UUID")
    return _id_key(value)


def _id_key(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        # Older boards use arbitrary tstamp identifiers.
        return value


def _read(path: Path) -> tuple[list, str]:
    raw = Path(path).read_bytes()
    try:
        ast = _parse_ast(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError("Invalid board S-expression") from exc
    if _tag(ast) != "kicad_pcb":
        raise ValueError("Expected a kicad_pcb S-expression")
    for node in _walk(ast):
        if any(type(v) is float and not math.isfinite(v) for v in node):
            raise ValueError("Non-finite board value")
    return ast, hashlib.sha256(raw).hexdigest()


def _net_table(ast: list) -> dict[int, str]:
    result = {}
    for node in _children(ast, "net"):
        if (len(node) != 3 or type(node[1]) is not int or node[1] < 0
                or type(node[2]) is not str):
            raise ValueError("Malformed net declaration")
        code, name = node[1:]
        if code in result or name in result.values():
            raise ValueError("Duplicate net code or name")
        if (code == 0) != (name == ""):
            raise ValueError("Invalid no-net declaration")
        result[code] = name
    if not result:
        raise ValueError("Missing net declarations")
    return result


def _net(node: list, codes: dict[int, str]) -> str:
    value = _field(node, "net", 1)[1]
    if type(value) is int:
        name = codes.get(value)
    elif type(value) is str and value in codes.values():
        name = value
    else:
        name = None
    if not name:
        raise ValueError("Unknown or no-net copper")
    return name


def _geometry(node: list, codes: dict[int, str], layers: list[str]) -> dict:
    kind = _tag(node)
    common = {"net", "uuid", "tstamp", "locked"}
    allowed = common | ({"start", "end", "width", "layer"} if kind == "segment"
                        else {"at", "size", "drill", "layers", "tenting"})
    seen = set()
    for item in node[1:]:
        if isinstance(item, list):
            name = _tag(item)
            if name not in allowed or name in seen:
                raise ValueError(f"Unsupported or duplicate {kind} field: {name}")
            seen.add(name)
        elif str(item) != "locked" or "locked" in seen:
            raise ValueError(f"Unsupported {kind} primitive or flag")
        else:
            seen.add("locked")
    for item in _children(node, "locked"):
        if len(item) > 2 or (len(item) == 2 and str(item[1]) not in {"yes", "no", "true", "false", "0", "1"}):
            raise ValueError("Malformed locked flag")
    name = _net(node, codes)
    if kind == "segment":
        a, b = _point(node, "start"), _point(node, "end")
        width = _number(_field(node, "width", 1)[1])
        layer = str(_field(node, "layer", 1)[1])
        if width <= 0 or a == b or layer not in layers:
            raise ValueError("Invalid segment width, length or copper layer")
        bounds = _bounds([a, b], width)
        start, end = sorted((a, b))
        geometry = {"kind": kind, "net": name, "start": list(start), "end": list(end),
                    "width": width, "layer": layer}
    else:
        at = _point(node, "at")
        size = _number(_field(node, "size", 1)[1])
        drill = _number(_field(node, "drill", 1)[1])
        span = [str(v) for v in _field(node, "layers", 2)[1:]]
        if not 0 < drill < size or set(span) != {"F.Cu", "B.Cu"}:
            raise ValueError("Unsupported via drill, diameter or layer span")
        for tenting in _children(node, "tenting"):
            if len(set(map(str, tenting[1:]))) != len(tenting) - 1 or any(str(v) not in {"front", "back"} for v in tenting[1:]):
                raise ValueError("Malformed via tenting")
        bounds = _bounds([at], size)
        geometry = {"kind": kind, "net": name, "at": list(at), "size": size,
                    "drill": drill, "layers": sorted(span)}
    if any(not math.isfinite(v) for v in bounds):
        raise ValueError("Non-finite copper envelope")
    return {**geometry, "bounds": bounds}


def _board(ast: list, *, source: bool) -> dict:
    codes = _net_table(ast)
    table = _field(ast, "layers")
    rows = table[1:]
    if any(not isinstance(r, list) or len(r) < 3 or type(r[0]) is not int for r in rows):
        raise ValueError("Malformed layer table")
    if len({r[0] for r in rows}) != len(rows) or len({str(r[1]) for r in rows}) != len(rows):
        raise ValueError("Duplicate layer declaration")
    layers = [str(r[1]) for r in rows if str(r[1]).endswith(".Cu")]
    if copper_layer_issues(layers):
        raise ValueError("Unsupported copper layer table")
    if any(str(r[2]) not in {"signal", "power", "mixed", "jumper"} for r in rows if str(r[1]) in layers):
        raise ValueError("Invalid copper layer type")

    edges, ids = [], set()
    top = {id(n) for n in ast[1:] if isinstance(n, list)}
    for node in _walk(ast):
        kind = _tag(node)
        if kind in {"arc", "zone", "rule_area", "generated", "group", "primitives", "padstack"}:
            raise ValueError(f"Unsupported copper/geometry: {kind}")
        if kind in {"segment", "via"} and id(node) not in top:
            raise ValueError("Unsupported nested copper")
        if _children(node, "net") and kind not in {"kicad_pcb", "pad", "segment", "via"}:
            raise ValueError(f"Unsupported net-bearing primitive: {kind}")
        if kind == "pad" and (len(node) < 4 or str(node[3]) not in {"circle", "rect", "oval", "trapezoid", "roundrect", "chamfered_rect"}):
            raise ValueError("Unsupported pad primitive")
        layer_fields = _children(node, "layer") + _children(node, "layers")
        layer_names = [str(v) for f in layer_fields for v in f[1:] if not isinstance(v, list)]
        if any(v.endswith(".Cu") or v == "F&B.Cu" for v in layer_names):
            if kind not in {"kicad_pcb", "footprint", "module", "pad", "segment", "via", "stackup"}:
                raise ValueError(f"Unsupported primitive copper: {kind}")
        if "Edge.Cuts" in layer_names and kind != "kicad_pcb":
            if id(node) not in top or kind not in {"gr_rect", "gr_line"}:
                raise ValueError("Unsupported outline or cutout")
            _field(node, "layer", 1)
            _point(node, "start")
            _point(node, "end")
            edges.append(node)
        if kind in {"uuid", "tstamp"}:
            if len(node) != 2 or not str(node[1]):
                raise ValueError("Malformed UUID")
            identity = _id_key(str(node[1]))
            if source and identity in ids:
                raise ValueError("Duplicate source UUID")
            ids.add(identity)
    outline = _rectangle(edges, "gr_")
    if outline is None:
        raise ValueError("Unsupported actual outline: requires a closed rectangle without cutouts")
    copper = []
    for node in ast[1:]:
        if _tag(node) in _COPPER:
            copper.append({"node": node, "id": _identity(node, required=source),
                           "geometry": _geometry(node, codes, layers)})
    return {"codes": codes, "layers": layers, "outline": outline, "ids": ids, "copper": copper}


def _inside(bounds: list[float], region: list[float]) -> bool:
    return (region[0] <= bounds[0] <= bounds[2] <= region[2]
            and region[1] <= bounds[1] <= bounds[3] <= region[3])


def _selection(board: dict, nets: list[str], region: list[float], remove_ids: list[str]) -> tuple[list, list, list, list]:
    if (type(nets) is not list or not 1 <= len(nets) <= 8
            or any(type(n) is not str or not n for n in nets)
            or len(set(nets)) != len(nets)):
        raise ValueError("Nets must be 1..8 unique nonempty names")
    if not set(nets) <= set(board["codes"].values()):
        raise ValueError("Unknown selected net")
    if type(region) is not list or len(region) != 4:
        raise ValueError("Region requires [xmin, ymin, xmax, ymax]")
    region = [_number(v) for v in region]
    if region[0] >= region[2] or region[1] >= region[3] or not _inside(region, board["outline"]):
        raise ValueError("Region must be positive and inside the actual outline")
    if (type(remove_ids) is not list or len(remove_ids) > 1000
            or any(type(v) is not str or not v for v in remove_ids)):
        raise ValueError("remove_ids must contain at most 1000 nonempty UUIDs")
    remove_ids = [_id_key(v) for v in remove_ids]
    if len(set(remove_ids)) != len(remove_ids):
        raise ValueError("Duplicate removal UUID")
    by_id = {item["id"]: item for item in board["copper"]}
    removed = []
    for identity in remove_ids:
        if identity not in by_id:
            raise ValueError(f"Unknown segment/via UUID: {identity}")
        item = by_id[identity]
        if _locked(item["node"]):
            raise ValueError(f"Locked copper cannot be removed: {identity}")
        if item["geometry"]["net"] not in nets:
            raise ValueError(f"Removal UUID is on an unselected net: {identity}")
        if not _inside(item["geometry"]["bounds"], region):
            raise ValueError(f"Removal copper envelope crosses region boundary: {identity}")
        removed.append({"uuid": identity, **item["geometry"], "ast": sexpdata.dumps(item["node"])})
    return list(nets), region, remove_ids, removed


def _digest(value) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Manifest must contain finite JSON values") from exc
    return hashlib.sha256(encoded).hexdigest()


def _manifest(board: dict, source_sha256: str, nets: list, region: list, remove_ids: list) -> dict:
    nets, region, remove_ids, removed = _selection(board, nets, region, remove_ids)
    result = {"version": 1, "source_sha256": source_sha256, "nets": nets, "region": region,
              "remove_ids": remove_ids, "removed_geometry": removed}
    return {**result, "manifest_sha256": _digest(result)}


def _new_paths(inputs: list[Path], outputs: list[Path]) -> None:
    paths = [Path(p) for p in inputs + outputs]
    if len({p.resolve() for p in paths}) != len(paths):
        raise ValueError("Repair requires distinct paths; no source alias")
    for output in outputs:
        if output.exists() or output.is_symlink():
            raise ValueError("Repair output must be a new copy; no overwrite or source alias")
        if not output.parent.is_dir():
            raise ValueError("Repair output parent directory must exist")
    for i, first in enumerate(inputs):
        if any(first.samefile(other) for other in inputs[i + 1:]):
            raise ValueError("Repair inputs must not alias the source")


def _serialize(ast: list) -> bytes:
    from decimal import Decimal

    chunks = []
    line_bytes = 0
    line_limit = 60 * 1024

    def newline(depth: int) -> None:
        nonlocal line_bytes
        indent = "  " * min(depth, 16)
        chunks.append("\n" + indent)
        line_bytes = len(indent)

    def emit(token: str, depth: int, space: bool = False) -> None:
        nonlocal line_bytes
        size = len(token.encode("utf-8"))
        if size > line_limit - 32:
            raise ValueError("Board atom exceeds the native-safe line limit")
        if line_bytes + int(space) + size > line_limit:
            newline(depth)
            space = False
        if space:
            chunks.append(" ")
            line_bytes += 1
        chunks.append(token)
        line_bytes += size

    def visit(node, depth: int = 0, space: bool = False) -> None:
        if isinstance(node, list):
            emit("(", depth, space)
            for index, child in enumerate(node):
                if isinstance(child, list):
                    newline(depth + 1)
                visit(child, depth + 1, index > 0 and not isinstance(child, list))
            emit(")", depth)
        else:
            if type(node) is float:
                if not math.isfinite(node):
                    raise ValueError("Non-finite board value")
                token = format(Decimal(repr(node)), "f")
                if "." not in token:
                    token += ".0"
            else:
                token = sexpdata.dumps(node)
            emit(token, depth, space)

    # Break only between AST items; strings and escaped whitespace remain atoms.
    visit(ast)
    text = "".join(chunks) + "\n"
    payload = text.encode("utf-8")
    if any(len(line) > line_limit for line in payload.split(b"\n")):
        raise ValueError("Board serialization exceeds the native-safe line limit")
    if _parse_ast(text) != ast:
        raise ValueError("Board AST cannot be serialized without changes")
    return payload


def _write_new(items: list[tuple[Path, bytes]]) -> None:
    # Reserve every destination before writing either preparation artifact.
    opened = []
    try:
        for path, data in items:
            stream = path.open("xb")
            opened.append((path, stream, data))
        for _, stream, data in opened:
            stream.write(data)
            stream.flush()
    except BaseException:
        for path, stream, _ in opened:
            stream.close()
            path.unlink(missing_ok=True)
        raise
    finally:
        for _, stream, _ in opened:
            stream.close()


def _retained_ast(ast: list, board: dict, remove_ids: list[str]) -> list:
    removed_nodes = {id(item["node"]) for item in board["copper"] if item["id"] in remove_ids}
    return [node for node in ast if id(node) not in removed_nodes]


def prepare_repair(source: Path, target: Path, empty: Path, nets: list[str],
                   region: list[float], remove_ids: list[str]) -> dict:
    """Create a local rip-up copy and copper-free staging copy, exclusively.

    The returned JSON manifest binds source_sha256, nets, region, remove_ids and
    removed_geometry (including original AST). No companion files are copied.
    """
    source, target, empty = Path(source), Path(target), Path(empty)
    _new_paths([source], [target, empty])
    ast, source_sha = _read(source)
    board = _board(ast, source=True)
    manifest = _manifest(board, source_sha, nets, region, remove_ids)
    target_ast = _retained_ast(ast, board, manifest["remove_ids"])
    empty_ast = [node for node in ast if _tag(node) not in _COPPER]
    payloads = [(target, _serialize(target_ast)), (empty, _serialize(empty_ast))]
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha:
        raise ValueError("Source hash changed during preparation")
    _write_new(payloads)
    return manifest


def merge_repair(source: Path, routed: Path, output: Path, manifest: dict) -> dict:
    """Apply only accepted new local copper to the original AST.

    Routed noncopper is never imported. Exact geometric duplicates are counted
    once, ignoring endpoint order and UUID. Native numeric net codes are mapped
    back by name. A checksum detects manifest edits; it is not a signature.
    """
    source, routed, output = Path(source), Path(routed), Path(output)
    _new_paths([source, routed], [output])
    ast, source_sha = _read(source)
    board = _board(ast, source=True)
    keys = {"version", "source_sha256", "nets", "region", "remove_ids", "removed_geometry", "manifest_sha256"}
    if type(manifest) is not dict or set(manifest) != keys:
        raise ValueError("Invalid manifest schema")
    if manifest["source_sha256"] != source_sha:
        raise ValueError("Manifest source hash mismatch")
    expected = _manifest(board, source_sha, manifest["nets"], manifest["region"], manifest["remove_ids"])
    if _digest(manifest) != _digest(expected):
        raise ValueError("Manifest integrity or removed geometry mismatch")
    native_ast, _ = _read(routed)
    native = _board(native_ast, source=False)
    if native["layers"] != board["layers"]:
        raise ValueError("Routed copper layer table differs from source")
    if set(native["codes"].values()) != set(board["codes"].values()):
        raise ValueError("Routed net names differ from source")
    retained = [item for item in board["copper"] if item["id"] not in expected["remove_ids"]]
    seen = {_digest(item["geometry"]) for item in retained}
    used_ids = set(board["ids"])
    original_tokens = {name: code for code, name in board["codes"].items()}
    additions = []
    counts = {"added": 0, "removed": len(expected["remove_ids"]),
              "dropped_outside_region": 0, "dropped_unselected_nets": 0,
              "deduplicated": 0, "regenerated_uuids": 0}
    for item in native["copper"]:
        geometry = item["geometry"]
        key = _digest(geometry)
        if key in seen:
            counts["deduplicated"] += 1
            continue
        if geometry["net"] not in expected["nets"]:
            counts["dropped_unselected_nets"] += 1
            continue
        if not _inside(geometry["bounds"], expected["region"]):
            counts["dropped_outside_region"] += 1
            continue
        if _locked(item["node"]):
            raise ValueError("Unsupported new locked copper in routed board")
        node = deepcopy(item["node"])
        _field(node, "net", 1)[1] = original_tokens[geometry["net"]]
        identity = item["id"]
        if identity is None or identity in used_ids:
            identity = str(uuid4())
            while identity in used_ids:
                identity = str(uuid4())
            node = [v for v in node if _tag(v) not in _IDENTITY]
            node.append([sexpdata.Symbol("uuid"), identity])
            counts["regenerated_uuids"] += 1
        used_ids.add(identity)
        seen.add(key)
        additions.append(node)
    result = _retained_ast(ast, board, expected["remove_ids"]) + additions
    # Prove preservation against the source tree, never the native working tree.
    if [n for n in result if _tag(n) not in _COPPER] != [n for n in ast if _tag(n) not in _COPPER]:
        raise ValueError("Noncopper preservation failed")
    if [n for n in result if _tag(n) in _COPPER] != [item["node"] for item in retained] + additions:
        raise ValueError("Retained copper preservation failed")
    _board(result, source=True)
    payload = _serialize(result)
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha:
        raise ValueError("Source hash changed during merge")
    _write_new([(output, payload)])
    counts["added"] = len(additions)
    return {**counts, "source_sha256": source_sha,
            "preservation": {"source_sha256": source_sha,
                             "noncopper_ast_preserved": True,
                             "retained_copper_ast_preserved": True,
                             "retained_copper_count": len(retained),
                             "changed_copper_within_region": True}}
