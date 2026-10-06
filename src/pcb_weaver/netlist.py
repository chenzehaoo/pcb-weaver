"""Compare physical pad connectivity, independent of net numeric codes."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def _board_name_map(board):
    # KiCad CTX_NETNAME stores embedded '/' as {slash}; XML exports display names.
    # Formatting/text-variable braces retain their meaning. Reject ambiguous aliases.
    names = {pad["net"] for fp in board["footprints"] for pad in fp["pads"] if pad["net"]}
    mapped, reverse = {}, {}
    for name in sorted(names):
        display = re.sub(r"(?<![$~^_])\{slash\}", "/", name)
        if display in reverse and reverse[display] != name:
            raise ValueError(f"Ambiguous KiCad network names after escape normalization: {reverse[display]!r}, {name!r}")
        mapped[name] = display
        reverse[display] = name
    return mapped


def inspect_netlist(path: Path, board: dict) -> dict:
    raw = path.read_bytes()
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD and entity declarations are not supported in KiCad netlists")
    root = ET.fromstring(raw)
    if root.tag != "export" or root.find("components") is None or root.find("nets") is None:
        raise ValueError("Expected a KiCad export with components and nets")
    components = []
    excluded = set()
    seen_refs = set()
    for comp in root.findall("./components/comp"):
        ref = comp.get("ref", "")
        if not ref or ref in seen_refs:
            raise ValueError("Missing or duplicate schematic reference")
        seen_refs.add(ref)
        properties = {p.get("name"): p.get("value", "") for p in comp.findall("property")}
        skip = "exclude_from_board" in properties
        if skip:
            excluded.add(ref)
        components.append({"reference": ref, "value": comp.findtext("value", ""),
                           "footprint": comp.findtext("footprint", ""),
                           "fields": {f.get("name"): f.text or "" for f in comp.findall("./fields/field")},
                           "dnp": "dnp" in properties, "exclude_from_board": skip})
    expected = {}
    mechanical = []
    board_footprints = {f["reference"]:f for f in board["footprints"]}
    for net in root.findall("./nets/net"):
        for node in net.findall("node"):
            if node.get("ref") not in excluded:
                key = (node.get("ref", ""), node.get("pin", ""))
                pads = board_footprints.get(key[0],{}).get("pads",[])
                if (key[1] == "" and len(net.findall("node")) == 1
                        and "no_connect" in node.get("pintype","").split("+") and pads
                        and all(p.get("type") == "np_thru_hole" and p["number"] == "" and not p["net"] for p in pads)):
                    mechanical.append({"reference":key[0],"reason":"Explicit singleton no-connect with only unnumbered non-plated holes"})
                    continue
                if key in expected and expected[key] != net.get("name", ""):
                    raise ValueError("Netlist assigns a pad to multiple nets")
                expected[key] = net.get("name", "")
    actual = {}
    board_names = _board_name_map(board)
    for footprint in board["footprints"]:
        for pad in footprint["pads"]:
            if pad["number"]:
                key = (footprint["reference"], pad["number"])
                name = board_names.get(pad["net"], pad["net"])
                if key in actual and actual[key] != name:
                    raise ValueError("Stacked pads have inconsistent nets")
                actual[key] = name
    differences = []
    for key in sorted(set(expected) | set(actual)):
        # Unconnected physical pads may be absent in the exported schematic netlist.
        if key not in expected and not actual.get(key):
            continue
        if expected.get(key) != actual.get(key):
            differences.append({"reference": key[0], "pad": key[1],
                                "schematic_net": expected.get(key), "board_net": actual.get(key)})
    schematic_refs = {c["reference"] for c in components if not c["exclude_from_board"]}
    board_refs = {f["reference"] for f in board["footprints"] if f["pads"]}
    missing_refs = sorted(schematic_refs - board_refs)
    footprints = {f["reference"]: f for f in board["footprints"]}
    component_differences = []
    for comp in components:
        if comp["exclude_from_board"] or comp["reference"] not in footprints:
            continue
        footprint = footprints[comp["reference"]]
        for field in ("value", "footprint"):
            if comp[field] != footprint.get(field, ""):
                component_differences.append({"reference": comp["reference"], "field": field,
                                              "schematic_value": comp[field],
                                              "board_value": footprint.get(field, "")})
    return {"status": "passed" if not differences and not missing_refs and not component_differences else "failed",
            "mechanical_no_connects":mechanical,
            "differences": differences, "component_differences": component_differences,
            "missing_references": missing_refs, "components": components,
            "name_normalization": {name: display for name, display in board_names.items() if name != display}}
