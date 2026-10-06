"""Protect retained native DSN copper without deriving or replacing routing rules.

This verifies the DSN transformation, not a Freerouting/native-import round trip.
Import can normalize traces and coalesce vias; the caller must verify its merge.
"""

from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re

import sexpdata


_FR_COMMIT = "c88ad67e6d7cdb83ad52589a84e43ef9fe1e8620"
_FR_BASE = f"https://github.com/freerouting/freerouting/blob/{_FR_COMMIT}/src/main/java/app/freerouting/"
_KICAD_COMMIT = "286b0611feca00727bf70bfa184ec2c28a745dc3"
_DECLARATION = '(string_quote ")'
_NORMALIZED_DECLARATION = '(string_quote "double_quote")'
# Match whole strings first so declaration-like text inside a name is untouched.
_TOKENS = re.compile(r'\(string_quote "\)|"(?:\\.|[^"\\])*"|[()]|[^\s()"]+|\s+')
_STATES = {"route", "normal", "fix", "protect", "shove_fixed"}


def _is(node, name):
    return isinstance(node, list) and bool(node) and node[0] == sexpdata.Symbol(name)


def _children(node, name):
    return [child for child in node if _is(child, name)]


def _one(node, name):
    values = _children(node, name)
    if len(values) != 1:
        raise ValueError(f"Protection requires exactly one DSN {name}")
    return values[0]


def _name(value):
    return isinstance(value, (str, sexpdata.Symbol)) and bool(str(value))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _parse(text):
    if "\0" in text:
        raise ValueError("Invalid NUL in DSN")
    parts, end, declarations = [], 0, 0
    for match in _TOKENS.finditer(text):
        if match.start() != end:
            raise ValueError("Invalid DSN string syntax")
        token = match[0]
        if token == _DECLARATION:
            token = _NORMALIZED_DECLARATION
            declarations += 1
        elif token[0] not in '()"' and not token.isspace():
            # DSN names contain Lisp punctuation, e.g. Rect[T]Pad and class,A.
            # Escape atoms for sexpdata only; serialization restores native atoms.
            token = sexpdata.dumps(sexpdata.Symbol(token))
        parts.append(token)
        end = match.end()
    if end != len(text) or declarations != 1:
        raise ValueError("Requires the canonical KiCad double-quote declaration")
    try:
        roots = sexpdata.parse("".join(parts), nil=None, true=None, false=None)
    except Exception as exc:
        raise ValueError("Invalid DSN syntax") from exc
    if len(roots) != 1 or not _is(roots[0], "pcb"):
        raise ValueError("Protection requires a native PCB DSN, not a rules/session file")
    return roots[0]


def _serialize(node, declaration):
    if node is declaration:
        return _DECLARATION
    if isinstance(node, list):
        return "(" + " ".join(_serialize(child, declaration) for child in node) + ")"
    if isinstance(node, sexpdata.Symbol):
        return str(node)
    if isinstance(node, str) or _number(node):
        return sexpdata.dumps(node)
    raise ValueError("Unsupported DSN AST value")


def _validate_rules(node):
    if not isinstance(node, list):
        return
    if _is(node, "rule"):
        if len(node) < 2 or any(not isinstance(child, list) or not child for child in node[1:]):
            raise ValueError("Malformed DSN rule")
        for child in node[1:]:
            if _is(child, "width") or _is(child, "clearance"):
                if len(child) < 2 or not _number(child[1]) or child[1] < 0:
                    raise ValueError("Invalid DSN rule width/clearance")
                if _is(child, "width") and (len(child) != 2 or child[1] == 0):
                    raise ValueError("Invalid DSN rule width")
                if _is(child, "clearance") and any(not isinstance(v, list) for v in child[2:]):
                    raise ValueError("Invalid DSN rule clearance qualifier")
    for child in node:
        _validate_rules(child)


def _zero_edges(path):
    points = list(zip(path[3::2], path[4::2]))
    return [i for i, (a, b) in enumerate(zip(points, points[1:])) if a == b]


def _validate_native_zero_path(tree, parser):
    """Accept representation zeros only in the observed native export profile."""
    version = _one(parser, "host_version")
    if (len(version) != 2 or not isinstance(version[1], str)
            or not re.fullmatch(r"9\.0\.\d+(?:[-+~][^\s]+)?", version[1])
            or _one(tree, "resolution")[1:] != [sexpdata.Symbol("um"), 10]
            or _one(tree, "unit")[1:] != [sexpdata.Symbol("um")]
            or _one(parser, "space_in_quoted_tokens")[1:] != [sexpdata.Symbol("on")]
            or _children(parser, "generated_by_freeroute")):
        raise ValueError("Repeated wire points require the canonical KiCad 9.0 um/10 export profile")


def _validate(tree):
    if len(tree) < 2 or not _name(tree[1]) or any(not isinstance(c, list) for c in tree[2:]):
        raise ValueError("Malformed PCB DSN")
    parser = _one(tree, "parser")
    if _one(parser, "host_cad")[1:] != ["KiCad's Pcbnew"] or _children(parser, "generated_by_freerouting"):
        raise ValueError("Protection supports only native KiCad-generated DSN")
    declaration = _one(parser, "string_quote")
    if declaration[1:] != ["double_quote"]:
        raise ValueError("Unsupported DSN quote declaration")
    structure = _one(tree, "structure")
    layers = _children(structure, "layer")
    if len(layers) not in (2, 4, 6, 8):
        raise ValueError("Requires a native 2/4/6/8 all-signal layer stack")
    names = []
    for layer in layers:
        if len(layer) < 3 or not _name(layer[1]) or _one(layer, "type")[1:] != [sexpdata.Symbol("signal")]:
            raise ValueError("Invalid DSN signal layer")
        names.append(str(layer[1]))
    if len(set(names)) != len(names):
        raise ValueError("Duplicate DSN layer name")
    resolution = _one(tree, "resolution")
    if len(resolution) != 3 or str(resolution[1]) != "um" or type(resolution[2]) is not int or resolution[2] <= 0:
        raise ValueError("Unsupported native DSN resolution")
    _one(structure, "boundary")
    _one(tree, "placement")
    library = _one(tree, "library")
    network = _one(tree, "network")
    nets, padstacks = set(), set()
    for container, kind, identifiers in ((network, "net", nets), (library, "padstack", padstacks)):
        for child in _children(container, kind):
            if len(child) < 2 or not _name(child[1]) or str(child[1]) in identifiers:
                raise ValueError(f"Malformed or duplicate DSN {kind}")
            identifiers.add(str(child[1]))
    _validate_rules(tree)
    wiring = _one(tree, "wiring")
    for item in wiring[1:]:
        if not (_is(item, "wire") or _is(item, "via")):
            raise ValueError("Unsupported DSN wiring: only wire paths and vias are supported")
        wire = _is(item, "wire")
        scopes = item[1:] if wire else item[4:]
        allowed = {"net", "type", "clearance_class"} | ({"path"} if wire else set())
        if any(not isinstance(c, list) or not c or not isinstance(c[0], sexpdata.Symbol)
               or str(c[0]) not in allowed for c in scopes):
            raise ValueError("Malformed or unsupported DSN wiring scope")
        for key in allowed:
            if len(_children(scopes, key)) > 1:
                raise ValueError(f"Duplicate wiring {key}")
        net = _one(scopes, "net")
        if len(net) != 2 or not _name(net[1]) or str(net[1]) not in nets:
            raise ValueError("Wiring requires one declared native net")
        for state in _children(scopes, "type"):
            if len(state) != 2 or not isinstance(state[1], sexpdata.Symbol) or str(state[1]) not in _STATES:
                raise ValueError("Malformed or unsupported wiring type")
        for clearance in _children(scopes, "clearance_class"):
            if len(clearance) != 2 or not _name(clearance[1]):
                raise ValueError("Malformed wiring clearance_class")
        if wire:
            path = _one(scopes, "path")
            if (len(path) < 7 or not _name(path[1]) or str(path[1]) not in names
                    or not _number(path[2]) or path[2] <= 0 or len(path[3:]) % 2
                    or any(not _number(v) for v in path[3:])):
                raise ValueError("Malformed wire path, layer, width or coordinates")
            if _zero_edges(path):
                # Native POINT::Format uses %.6g, so nonzero source segments can
                # collapse here. Preserve them; the original-board merge owns
                # physical copper preservation, not the rounded DSN/import.
                _validate_native_zero_path(tree, parser)
        elif (len(item) < 5 or not _name(item[1]) or str(item[1]) not in padstacks
              or not _number(item[2]) or not _number(item[3])):
            raise ValueError("Malformed via padstack or coordinates")
    return declaration, wiring


def _without_types(tree):
    copy = deepcopy(tree)
    for item in _one(copy, "wiring")[1:]:
        item[:] = [child for child in item if not _is(child, "type")]
    return copy


def _evidence():
    scopes = {
        "designforms/specctra/Wiring.java": "write_fixed, calc_fixed, read_wire_scope, read_via_scope, read_scope",
        "board/FixedState.java": "fixed-state ordering",
        "board/Item.java": "is_user_fixed, is_delete_fixed, is_shove_fixed, unfix",
        "board/Trace.java": "is_routable, is_shove_fixed",
        "board/Via.java": "is_routable",
        "autoroute/MazeSearchAlgo.java": "check_ripup",
        "board/BasicBoard.java": "remove_items, normalize_traces",
    }
    return {
        "version": "1.9.0", "tag": "v1.9.0", "commit": _FR_COMMIT,
        "verification": "official source inspection; no routing/native-import execution",
        "fixed_state": "USER_FIXED", "dsn_type": "protect",
        "semantics": "Wiring reads protect as USER_FIXED. Trace/Via.is_routable is false; "
                     "check_ripup rejects it. Item guards mark it delete-fixed and shove-fixed. "
                     "USER_FIXED can still be explicitly unfixed; it is not SYSTEM_FIXED.",
        "sources": [{"url": _FR_BASE + path, "reading_scope": scope} for path, scope in scopes.items()],
        "limitations": "Source inspection covers the listed methods at 1.9.0 only. "
                       "Import normalizes traces, rounds coordinates and skips duplicate vias. "
                       "It does not prove unchanged copper after routing, session export or native import.",
        "native_export": {
            "version": "9.0.0", "commit": _KICAD_COMMIT,
            "url": f"https://github.com/KiCad/kicad-source-mirror/blob/{_KICAD_COMMIT}/pcbnew/specctra_import_export/specctra_export.cpp",
            "reading_scope": "track/via IsLocked branches assigning m_wire_type/m_via_type",
            "locked_type": "fix", "unlocked_type": "route",
            "note": "The exporter notes that fix items are omitted from sessions. "
                    "Changing fix to protect changes this state; callers must merge retained copper carefully.",
        },
    }


def protect_dsn(source: Path, output: Path) -> dict:
    """Exclusively write a protected canonical KiCad DSN and return scoped evidence.

    Reject aliases, existing destinations, malformed/ambiguous wiring, non-path
    wires, unknown nets/padstacks, and layer stacks outside the toolchain profile.
    Only direct wiring type scopes change. This is not a complete DSN rule/DRC
    validator; existing rules remain authoritative and are never synthesized.
    """
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve() or (source.exists() and output.exists() and source.samefile(output)):
        raise ValueError("Source and output must not alias")
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if not source.is_file():
        raise ValueError("Source must be an existing DSN file")
    original_bytes = source.read_bytes()
    try:
        tree = _parse(original_bytes.decode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("DSN must be UTF-8") from exc
    declaration, wiring = _validate(tree)
    original = deepcopy(tree)
    counts = {"wire_count": 0, "via_count": 0, "protected_count": 0, "already_protected_count": 0}
    zero_paths = []
    for item in wiring[1:]:
        counts[str(item[0]) + "_count"] += 1
        if _is(item, "wire"):
            path = _one(item, "path")
            zero_edges = _zero_edges(path)
            if zero_edges:
                points = [list(point) for point in zip(path[3::2], path[4::2])]
                zero_paths.append({"wire_index": counts["wire_count"] - 1,
                                   "net": str(_one(item, "net")[1]), "layer": str(path[1]),
                                   "width": path[2], "points": points,
                                   "zero_length_edge_indices": zero_edges,
                                   "entire_path_zero_length": len(zero_edges) == len(points) - 1})
        states = _children(item, "type")
        if states:
            counts["already_protected_count"] += states[0][1] == sexpdata.Symbol("protect")
            states[0][1] = sexpdata.Symbol("protect")
        else:
            item.append([sexpdata.Symbol("type"), sexpdata.Symbol("protect")])
        counts["protected_count"] += 1
    contents = (_serialize(tree, declaration) + "\n").encode("utf-8")
    reparsed = _parse(contents.decode("utf-8"))
    _validate(reparsed)
    if reparsed != tree or _without_types(original) != _without_types(reparsed):
        raise ValueError("DSN serialization did not preserve the AST except wiring type")
    if source.read_bytes() != original_bytes:
        raise ValueError("Source DSN changed during protection")
    with output.open("xb") as stream:
        stream.write(contents)
    if output.read_bytes() != contents or source.read_bytes() != original_bytes:
        raise ValueError("DSN source/output changed during exclusive write")
    return {
        "source_path": str(source), "output_path": str(output),
        "input_dsn_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "output_dsn_sha256": hashlib.sha256(contents).hexdigest(),
        **counts,
        "repeated_point_path_count": len(zero_paths),
        "zero_length_path_count": sum(p["entire_path_zero_length"] for p in zero_paths),
        "zero_length_edge_count": sum(len(p["zero_length_edge_indices"]) for p in zero_paths),
        "representation_zero_paths": zero_paths,
        "representation_zero_evidence": {
            "classification": "native DSN representation; not a physical source-geometry verdict",
            "accepted_profile": "KiCad 9.0.x, unit um, resolution um 10, canonical double-quote parser",
            "handling": "All path coordinates and widths preserved; only wiring type changes to protect",
            "native_format": "POINT::Format uses %.6g for coordinates; distinct endpoints can round equal",
            "native_source": "https://docs.kicad.org/doxygen/specctra_8h_source.html#l00162",
            "reader_source": "https://github.com/freerouting/freerouting/blob/v1.9.0/src/main/java/app/freerouting/designforms/specctra/Wiring.java#L454-L474",
            "reader_semantics": "The reviewed v1.9.0 reader constructs a Polygon and skips paths "
                                "with fewer than two remaining corners",
            "limitation": "No router/import execution or downstream copper-survival guarantee. "
                          "Merge must retain original copper exactly and final native DRC remains required",
        },
        "type_changed_count": counts["protected_count"] - counts["already_protected_count"],
        "ast_preserved_except_wiring_type": True,
        "source_bytes_unchanged_verified": True,
        "physical_rule_overrides": False,
        "fixed_state_evidence": _evidence(),
    }
