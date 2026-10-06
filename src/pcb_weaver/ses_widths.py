"""Raise SES trace widths only; geometry must still pass native import and DRC."""
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
import hashlib
import math
from pathlib import Path

import sexpdata

from .repair_geometry import _serialize


def _children(node, tag):
    return [child for child in node if isinstance(child, list) and child and str(child[0]) == tag]


def _one(node, tag):
    values = _children(node, tag)
    if len(values) != 1:
        raise ValueError("Expected exactly one SES " + tag)
    return values[0]


def raise_minima(source: Path, output: Path, global_mm: float, per_net: dict, known_nets: set) -> dict:
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError("SES output must be a new file")
    for value in [global_mm, *per_net.values()]:
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Track minima must be finite positive numbers")
    if not set(per_net).issubset(known_nets):
        raise ValueError("Minimum rule refers to an unknown net")
    raw = source.read_bytes()
    tree = sexpdata.loads(raw.decode("utf-8"), nil=None, true=None, false=None)
    if not isinstance(tree, list) or not tree or str(tree[0]) != "session":
        raise ValueError("Expected native SES session")
    original = deepcopy(tree)
    routes = _one(tree, "routes")
    resolution = _one(routes, "resolution")
    if (len(resolution) != 3 or str(resolution[1]) != "um" or
            type(resolution[2]) is not int or resolution[2] <= 0):
        raise ValueError("Unsupported SES resolution")
    network = _one(routes, "network_out")
    changes, seen, restorations = [], set(), []
    for net in network[1:]:
        if not isinstance(net, list) or len(net) < 2 or str(net[0]) != "net":
            raise ValueError("Unexpected SES network entry")
        name = str(net[1])
        if name not in known_nets or name in seen:
            raise ValueError("Unknown or duplicate SES net")
        seen.add(name)
        required = max(global_mm, per_net.get(name, global_mm))
        units = int((Decimal(str(required)) * 1000 * resolution[2]).to_integral_value(rounding=ROUND_CEILING))
        for index, wire in enumerate(_children(net, "wire")):
            path = _one(wire, "path")
            if (len(path) < 7 or len(path[3:]) % 2 or any(type(v) not in (int, float) or
                    not math.isfinite(v) for v in path[2:]) or path[2] <= 0):
                raise ValueError("Unsupported SES path")
            if path[2] < units:
                changes.append({"net": name, "wire_index": index, "from_units": path[2],
                                "to_units": units, "minimum_mm": required})
                restorations.append((path, path[2]))
                path[2] = units
    contents = _serialize(tree)
    if sexpdata.loads(contents.decode("utf-8"), nil=None, true=None, false=None) != tree:
        raise ValueError("SES serialization changed AST")
    for path, width in restorations:
        path[2] = width
    if tree != original or source.read_bytes() != raw:
        raise ValueError("SES non-width content changed")
    with output.open("xb") as stream:
        stream.write(contents)
    return {"source_sha256": hashlib.sha256(raw).hexdigest(),
            "output_sha256": hashlib.sha256(contents).hexdigest(), "changes": changes,
            "non_width_ast_preserved": True, "resolution_um": resolution[2],
            "requires_native_drc": True, "manufacturing_authorized": False}
