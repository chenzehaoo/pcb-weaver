"""Read-only audit of the observed Altium DSN export; never routing authorization."""
import argparse
from collections import Counter
import configparser
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/validation/altium-native/a201905ecc7b4c3eb819e531d52c5f1e/result.json"
INVENTORY_SHA256 = "2d306cfd23c1c5ec5fbdee41991bb494d9a23103012999477c7dc45380f5f4ad"


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pin(raw, expected, label):
    _require(isinstance(expected, str) and re.fullmatch(r"[0-9a-fA-F]{64}", expected)
             and _sha(raw) == expected.lower(), label + " SHA256 mismatch or missing pin")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def _json(raw):
    def invalid(value):
        raise ValueError("Invalid JSON constant: " + value)
    result = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=invalid)
    _require(isinstance(result, dict), "Expected JSON object")
    return result


class Quoted(str):
    """Keep quoted names distinct from form keywords without coercing numeric IDs."""


def parse_dsn(raw):
    """Bounded S-expression parser; exact numeric lexemes, one PCB root, no Lisp evaluation.

    This export has no string_quote declaration. Only double-quoted strings with
    escaped quote/backslash are supported; other quote/comment dialects fail closed.
    """
    _require(isinstance(raw, bytes) and 0 < len(raw) <= 8 * 1024 * 1024, "Missing or oversized DSN")
    text = raw.decode("utf-8-sig")
    stack, roots, index, tokens = [], [], 0, 0
    while index < len(text):
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        tokens += 1
        _require(tokens <= 1000000, "DSN token limit exceeded")
        if char == "(":
            _require(len(stack) < 64, "DSN nesting limit exceeded")
            node = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
            index += 1
            continue
        if char == ")":
            _require(stack and stack[-1] and type(stack[-1][0]) is str, "Invalid DSN list or closure")
            stack.pop()
            index += 1
            continue
        _require(bool(stack), "DSN atom outside root")
        if char == '"':
            index += 1
            value = []
            while index < len(text) and text[index] != '"':
                char = text[index]
                _require(ord(char) >= 32, "Control character in DSN string")
                if char == "\\":
                    index += 1
                    _require(index < len(text) and text[index] in '\\"', "Unsupported DSN string escape")
                    char = text[index]
                value.append(char)
                index += 1
            _require(index < len(text), "Unterminated DSN string")
            stack[-1].append(Quoted("".join(value)))
            index += 1
            _require(index == len(text) or text[index] in "() \t\r\n", "Missing DSN token separator")
        else:
            start = index
            while index < len(text) and text[index] not in "() \t\r\n":
                _require(ord(text[index]) >= 32 and text[index] not in '\";`', "Unsupported DSN token syntax")
                index += 1
            stack[-1].append(text[start:index])
    _require(not stack and len(roots) == 1 and roots[0][0] == "pcb", "Truncated, multiple, or non-PCB DSN root")
    return roots[0]


def _children(node, tag):
    return [child for child in node if isinstance(child, list) and child[0] == tag]


def _one(node, tag):
    rows = _children(node, tag)
    _require(len(rows) == 1, "Expected exactly one DSN " + tag)
    return rows[0]


def _name(value):
    _require(isinstance(value, str) and bool(value.strip()), "Invalid DSN name")
    return str(value)


def _number(value, minimum=None):
    _require(type(value) is str and re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value), "Invalid DSN numeric token")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid DSN number") from error
    _require(number.is_finite() and (minimum is None or number >= minimum), "Invalid DSN numeric value")
    return number


def _named(rows):
    result, folded = {}, set()
    for row in rows:
        _require(len(row) >= 2, "Missing DSN identity")
        name = _name(row[1])
        _require(name.casefold() not in folded, "Duplicate or ambiguous DSN identity: " + name)
        result[name] = row
        folded.add(name.casefold())
    return result


def _walk(node, path="pcb"):
    yield path, node
    for index, child in enumerate(node[1:], 1):
        if isinstance(child, list):
            yield from _walk(child, path + "/" + child[0] + "[" + str(index) + "]")


def _inventory_rows(inventory, kind):
    native = inventory["native"]
    rows = {key: value for key, value in native.items() if key.startswith(kind+".")}
    _require(rows and set(rows) == {kind+"."+str(i) for i in range(len(rows))}, "Incomplete inventory " + kind)
    return list(rows.values())


def audit_structure(tree, inventory):
    _require(len(tree) >= 2, "Missing DSN board name")
    board_name = _name(tree[1])
    _require(all(isinstance(node, list) for node in tree[2:]), "Unexpected root atom")
    resolution = _one(tree, "resolution")
    _require(len(resolution) == 3, "Malformed DSN resolution")
    _name(resolution[1])
    divisor = _number(resolution[2], 1)
    _require(divisor == divisor.to_integral_value(), "Non-integral DSN resolution")
    _require(len(_children(tree, "unit")) <= 1, "Duplicate DSN unit")
    structure, placement, library, network, wiring = [_one(tree, tag) for tag in ("structure", "placement", "library", "network", "wiring")]
    _require(all(isinstance(node, list) for parent in (structure, placement, library, network, wiring) for node in parent[1:]),
             "Unexpected container atom")
    layers = _named(_children(structure, "layer"))
    _require(bool(layers), "Missing DSN layers")
    layer_rows = []
    for name, row in layers.items():
        kind = _one(row, "type")
        _require(len(kind) == 2, "Malformed DSN layer type")
        layer_rows.append({"name": name, "type": _name(kind[1]), "declaration": row})
    images, stacks, nets = [_named(_children(parent, tag)) for parent, tag in
                            ((library, "image"), (library, "padstack"), (network, "net"))]
    _require(images and stacks and nets, "Missing library/network inventory")
    placements, image_pins = {}, {}
    for image, row in images.items():
        _require(all(isinstance(node, list) for node in row[2:]), "Unexpected image atom")
        pins = {}
        for pin in _children(row, "pin"):
            _require(len(pin) >= 5 and _name(pin[1]) in stacks, "Pin refers to unknown padstack")
            name = _name(pin[2])
            _require(name not in pins, "Duplicate image pin identity")
            _number(pin[3]); _number(pin[4])
            _require(all(isinstance(node, list) for node in pin[5:]), "Unexpected pin atom")
            rotations = _children(pin, "rotate")
            _require(len(rotations) <= 1, "Duplicate pin rotation")
            if rotations:
                _require(len(rotations[0]) == 2, "Malformed pin rotation")
                _number(rotations[0][1])
            pins[name] = pin
        image_pins[image] = pins
    for component in _children(placement, "component"):
        _require(len(component) >= 3 and _name(component[1]) in images, "Placement refers to unknown image")
        _require(all(isinstance(node, list) for node in component[2:]), "Unexpected component atom")
        places = _children(component, "place")
        _require(bool(places), "Empty component placement")
        for place in places:
            _require(len(place) == 6 and place[4] in ("front", "back"), "Malformed placement")
            ref = _name(place[1])
            _require(ref not in placements, "Duplicate component placement")
            for value in (place[2], place[3], place[5]):
                _number(value)
            placements[ref] = {"image": str(component[1]), "raw_place": place}
    _require(bool(placements), "Missing component placements")
    # Compose complete IDs; never split a hyphenated component/pad identifier.
    dsn_pins = {}
    for ref, place in placements.items():
        for name, pin in image_pins[place["image"]].items():
            key = ref + "-" + name
            _require(key not in dsn_pins, "Ambiguous composed DSN pin identifier")
            dsn_pins[key] = {"component": ref, "pad": name, "padstack": str(pin[1])}
    assignments = {}
    for name, row in nets.items():
        _require(all(isinstance(node, list) for node in row[2:]), "Unexpected net atom")
        for token in _one(row, "pins")[1:]:
            key = _name(token)
            _require(key in dsn_pins and key not in assignments, "Unknown or multiply assigned network pin: " + key)
            assignments[key] = name
    for stack in stacks.values():
        shapes = _children(stack, "shape")
        _require(shapes and all(isinstance(node, list) for node in stack[2:])
                 and all(len(shape) == 2 and isinstance(shape[1], list) for shape in shapes), "Malformed padstack shapes")
    for pair in _children(network, "pair"):
        members = _one(pair, "nets")[1:]
        _require(len(members) == 2 and len(set(members)) == 2 and all(value in nets for value in members), "Invalid differential pair references")
    for node in _children(structure, "via"):
        _require(len(node) > 1 and all(value in stacks for value in node[1:]), "Unknown routing via padstack")
    for row in wiring[1:]:
        _require(isinstance(row, list), "Malformed wiring entry")
        if row[0] not in {"wire", "via"}:
            continue
        net = _one(row, "net")
        _require(len(net) == 2 and net[1] in nets, "Unknown wiring net")
        if row[0] == "via":
            _require(len(row) >= 4 and row[1] in stacks, "Unknown wiring via padstack")
            _number(row[2]); _number(row[3])
        else:
            _one(row, "path")

    forms = list(_walk(tree))
    counts = Counter(node[0] for _, node in forms)
    geometry, rules, unmapped = [], [], []
    known = {"pcb", "resolution", "unit", "structure", "boundary", "via", "rule", "length_amplitude", "width", "clearance", "reorder",
             "layer", "type", "direction", "keepout", "polygon", "circle", "rect", "path", "placement", "component", "place", "library",
             "image", "pin", "rotate", "padstack", "shape", "network", "net", "pins", "pair", "nets", "wiring", "wire"}
    for path, node in forms:
        tag = node[0]
        if tag not in known:
            unmapped.append({"path": path, "tag": tag, "reason": "Uninterpreted DSN construct"})
        if tag == "unit":
            _require(len(node) == 2, "Malformed unit declaration")
            _name(node[1])
        if tag in {"rect", "circle", "path", "polygon"}:
            _require(len(node) >= 3 and node[1] in {*layers, "signal", "pcb"}, "Unknown geometry layer")
            values = [_number(value) for value in node[2:]]
            valid = ((tag == "rect" and len(values) == 4 and values[0] < values[2] and values[1] < values[3])
                     or (tag == "circle" and len(values) in {1, 3} and values[0] > 0)
                     or (tag in {"path", "polygon"} and len(values) >= (7 if tag == "polygon" else 5)
                         and len(values) % 2 == 1 and values[0] >= 0))
            _require(valid, "Malformed DSN geometry: " + path)
            geometry.append({"path": path, "kind": tag, "layer": str(node[1]), "numeric_tokens": len(values),
                             "form_sha256": _sha(json.dumps(node, ensure_ascii=True).encode("ascii"))})
        if tag == "rule":
            _require(len(node) > 1 and all(isinstance(value, list) for value in node[1:]), "Malformed rule scope")
            rules.append({"path": path, "forms": node[1:], "native_rule_mapping": "not_proven"})
        if tag in {"width", "clearance", "length_amplitude"}:
            _require(len(node) >= 2, "Empty numeric rule")
            for value in node[1:]:
                if not isinstance(value, list):
                    _number(value, 0)

    components = {row["designator"]: row for row in _inventory_rows(inventory, "component")}
    native_nets = {row["name"] for row in _inventory_rows(inventory, "net")}
    native_pads, standalone = {}, []
    for pad in _inventory_rows(inventory, "pad"):
        if "component" not in pad:
            standalone.append(pad["name"])
            continue
        key = pad["component"] + "-" + pad["name"]
        _require(key not in native_pads, "Ambiguous native pad identity")
        native_pads[key] = pad
    common = set(dsn_pins) & native_pads.keys()
    pad_mismatches = [{"pin": key, "inventory_net": native_pads[key].get("net"), "dsn_net": assignments.get(key)}
                      for key in sorted(common) if native_pads[key].get("net") != assignments.get(key)]
    placement_evidence = [{"component": ref, **placements[ref], "native_x": components[ref]["x"],
                          "native_y": components[ref]["y"], "native_rotation": components[ref]["rotation"],
                          "native_layer": components[ref]["layer"], "geometry_equivalence": "not_proven"}
                         for ref in sorted(placements.keys() & components.keys())]
    inventory_rules = _inventory_rows(inventory, "rule")
    return {"board_name": board_name, "counts": {"layers": len(layers), "nets": len(nets), "placements": len(placements),
            "images": len(images), "padstacks": len(stacks), "image_pins": len(dsn_pins), "network_pins": len(assignments),
            "rule_scopes": len(rules), "boundaries": counts["boundary"], "keepouts": counts["keepout"],
            "polygons": counts["polygon"], "wires": len(_children(wiring, "wire")), "vias": len(_children(wiring, "via"))},
            "units": {"resolution": resolution[1:], "unit_declarations": [node[1:] for _, node in forms if node[0] == "unit"],
                      "inventory_units": inventory["native"]["job"].get("units"), "equivalence_verified": False},
            "layers": layer_rows, "placement_evidence": placement_evidence, "rules": rules, "geometry_evidence": geometry,
            "mapping": {"matched_components": len(placement_evidence), "missing_components": sorted(components.keys()-placements.keys()),
                        "extra_components": sorted(placements.keys()-components.keys()), "missing_nets": sorted(native_nets-nets.keys()),
                        "extra_nets": sorted(nets.keys()-native_nets), "matched_pad_names": len(common),
                        "missing_component_pads": sorted(native_pads.keys()-dsn_pins.keys()), "extra_pads": sorted(dsn_pins.keys()-native_pads.keys()),
                        "unmapped_standalone_pads": standalone, "pad_net_mismatches": pad_mismatches,
                        "matching_assigned_pad_nets": sum(key in assignments and key not in {row["pin"] for row in pad_mismatches} for key in common)},
            "unmapped_constructs": unmapped,
            "coverage": {"complete": False, "layerstack_equivalence": False, "placement_equivalence": False, "padstack_equivalence": False,
                         "keepout_polygon_equivalence": False, "rule_numeric_equivalence": False,
                         "unmapped_enabled_rules": [row for row in inventory_rules if row["drc_enabled"] == "True"],
                         "note": "DSN polygons/keepouts are exported forms, not proof of native polygon semantics or absence of omissions"}}


def audit_export(result_path, *, inventory_path=INVENTORY, inventory_sha256=INVENTORY_SHA256, expected_result_sha256=None):
    result = {"schema": 1, "status": "blocked", "audit_valid": False, "full_autoroute_authorized": False,
              "native_drc": "not_run_by_audit", "manufacturing_authorized": False}
    inputs = {}
    def read(value):
        _require(isinstance(value, (str, Path)), "Missing evidence path")
        path = Path(value)
        _require(path.is_absolute() and path.is_file(), "Missing/non-absolute evidence file")
        for part in (path, *path.parents):
            _require(not part.is_symlink() and not getattr(part, "is_junction", lambda: False)(), "Linked evidence path")
        raw = path.read_bytes()
        _require(bool(raw), "Empty evidence file")
        inputs[path] = raw
        return path, raw
    try:
        path, raw = read(Path(result_path).absolute())
        if expected_result_sha256 is not None:
            _pin(raw, expected_result_sha256, "Export result")
        data = _json(raw)
        _require(data.get("mode") == "Export" and data.get("status") == "completed" and data.get("source_unchanged") is True, "Requires completed unchanged Export")
        native = data["native"]
        _require(set(native) == {"job", "completion"} and native["completion"]["status"] == "completed"
                 and isinstance(data.get("request"), str) and bool(data["request"])
                 and native["job"]["request"] == data["request"], "Export request/status mismatch")
        source, source_raw = read(data["source"])
        board, board_raw = read(data["board"])
        _require(board.parent.resolve() == path.parent.resolve() and not board.samefile(source)
                 and Path(native["job"]["board"]).resolve() == board.resolve(), "Export board identity mismatch")
        _pin(source_raw, data.get("source_sha256"), "Original")
        _pin(board_raw, data.get("source_snapshot_sha256"), "Snapshot")
        _require(source_raw == board_raw, "Export snapshot differs from original")
        _, response = read(path.parent / "response.ini")
        _pin(response, data.get("response_sha256"), "Response")
        ini = configparser.ConfigParser(interpolation=None, strict=True)
        ini.read_string(response.decode("utf-8-sig"))
        _require(not ini.defaults() and {key: dict(ini[key]) for key in ini.sections()} == native, "Response/result mismatch")
        _, job = read(path.parent / "Job.pas")
        _pin(job.decode("ascii").replace("\r\n", "\n").replace("\r", "\n").encode("ascii"), data.get("template_sha256"), "Generated script")
        _require(set(data.get("artifacts", {})) == {"export.dsn"}, "Missing or unexpected export artifact")
        _, dsn = read(path.parent / "export.dsn")
        _pin(dsn, data["artifacts"]["export.dsn"], "DSN")
        inv_path, inv_raw = read(Path(inventory_path).absolute())
        _pin(inv_raw, inventory_sha256, "Pinned inventory")
        inventory = _json(inv_raw)
        _require(inventory["mode"] == "Inventory" and inventory["status"] == "completed" and inventory["source_unchanged"] is True
                 and Path(inventory["source"]).resolve() == source.resolve(), "Inventory source/status mismatch")
        _pin(source_raw, inventory["source_sha256"], "Inventory source")
        inv_board, inv_snapshot = read(inventory["board"])
        _pin(inv_snapshot, inventory["source_sha256"], "Inventory snapshot")
        _require(inventory["native"]["completion"]["status"] == "completed"
                 and inventory["native"]["job"]["request"] == inventory["request"]
                 and Path(inventory["native"]["job"]["board"]).resolve() == inv_board.resolve(), "Inventory native identity mismatch")
        tree = parse_dsn(dsn)
        _require(tree[1] == board.name, "DSN PCB name does not match export board")
        audit = audit_structure(tree, inventory)
        _require(all(file.read_bytes() == content for file, content in inputs.items()), "Evidence changed during audit")
        result.update(audit_valid=True, request=data["request"], **audit,
                      integrity={"result_sha256": _sha(raw), "dsn_sha256": _sha(dsn), "response_sha256": _sha(response),
                                 "source_sha256": _sha(source_raw), "snapshot_sha256": _sha(board_raw), "script_byte_sha256": _sha(job),
                                 "inventory_path": str(inv_path), "inventory_sha256": _sha(inv_raw), "inputs_unchanged": True},
                      blocking_reasons=["Units, image-flattened placement, native layer/padstack and polygon semantics are not proven equivalent",
                                        "Pinned inventory lacks numeric rule values; enabled-rule coverage cannot be established"])
    except (ValueError, OSError, TypeError, KeyError, IndexError, configparser.Error) as error:
        result["errors"] = [str(error)]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    args = parser.parse_args(argv)
    result = audit_export(args.result)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 2 if result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
