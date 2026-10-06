"""Complete, revision-bound read-only inventory, not electrical signoff.

Lengths cover straight segments only. Unsupported objects remain explicit;
metadata strings are data, never URLs to fetch or paths to open. IDs are local
indices stable only within a sealed revision, not cross-revision identities.
"""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import re

import sexpdata

from . import board as pcb
from . import catalog


_ALIASES = {
    "manufacturer": {"manufacturer", "mfr", "mfg", "manufacturername"},
    "mpn": {"mpn", "manufacturerpartnumber", "mfrpartnumber", "mfgpartnumber"},
    "datasheet": {"datasheet", "datasheeturl"},
}
_CATEGORIES = {
    "R": "resistor", "RN": "resistor_network", "C": "capacitor",
    "L": "inductor", "D": "diode", "LED": "led", "Q": "transistor",
    "U": "integrated_circuit", "IC": "integrated_circuit", "J": "connector",
    "P": "connector", "Y": "crystal_or_oscillator", "X": "crystal_or_oscillator",
    "F": "fuse", "SW": "switch", "K": "relay", "TP": "test_point",
    "H": "mounting_hardware", "BT": "battery", "T": "transformer",
}


def _category(reference):
    match = re.match(r"^([A-Za-z]+)[0-9]", reference)
    prefix = match[1].upper() if match else None
    return _CATEGORIES.get(prefix, "unknown"), {
        "method": "reference_prefix_heuristic", "prefix": prefix,
        "verified": False, "rule_version": "1.0",
    }


def _metadata(component, schematic, verification_id):
    candidates = {key: [] for key in _ALIASES}
    items = [(p["name"], p["value"], "board.properties")
             for p in component["property_items"]]
    for record in schematic:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            raise ValueError("Invalid verified component fields")
        items.extend((k, v, "verification.connectivity.components.fields") for k, v in fields.items())
    for name, value, source in items:
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("Component metadata must be strings")
        normalized = re.sub(r"[\s_-]", "", name).casefold()
        if value.strip() in {"", "~"}:
            continue
        for key, aliases in _ALIASES.items():
            if normalized in aliases:
                candidates[key].append({"field": name, "value": value, "source": source,
                                        "verification_id": verification_id if source.startswith("verification") else None})
    conflicts = []
    for key, options in candidates.items():
        values = {p["value"] for p in options}
        component[key] = next(iter(values)) if len(values) == 1 else None
        if len(values) > 1:
            conflicts.append({"field": key, "candidates": deepcopy(options)})
    for record in schematic:
        for key in ("value", "footprint", "dnp"):
            if key in record and record[key] != component[key]:
                conflicts.append({"field": key, "board_value": component[key],
                                  "schematic_value": record[key],
                                  "source": "verification.connectivity.components",
                                  "verification_id": verification_id})
    component["metadata_sources"] = candidates
    component["metadata_conflicts"] = conflicts


def _net_name(node, codes):
    token = pcb._child(node, "net", [None, 0])[1]
    if str(token) in codes:
        return codes[str(token)]
    return token if isinstance(token, str) else ""


def _inventory(ast, inspected, constraints, check):
    schematic = defaultdict(list)
    if check["status"] not in {"not_verified", "invalid_evidence"}:
        connectivity = check.get("connectivity", {})
        for item in connectivity.get("components", []):
            if not isinstance(item, dict) or not isinstance(item.get("reference"), str):
                raise ValueError("Invalid verified component metadata")
            if not item.get("exclude_from_board", False):
                schematic[item["reference"]].append(item)
    components = deepcopy(inspected["footprints"])
    for component in components:
        component["category"], component["category_basis"] = _category(component["reference"])
        _metadata(component, schematic[component["reference"]], check.get("verification_id"))
        for index, pad in enumerate(component["pads"]):
            pad["id"] = f"{component['reference']}:pad:{index}"

    tracks = [{**t, "id": f"track:{i}", "kind": "segment", "geometry_supported": True,
               "length_status": "complete"} for i, t in enumerate(inspected["track_items"])]
    codes = {str(n[1]): str(n[2]) for n in pcb._children(ast, "net")}
    # Arcs are listed without inventing a length or changing the board parser's
    # established segment-only geometry contract.
    for node in pcb._children(ast, "arc"):
        tracks.append({"id": f"track:{len(tracks)}", "kind": "arc",
                       "net": _net_name(node, codes), "layer": pcb._layer(node),
                       "start": list(pcb._xy(node, "start")), "mid": list(pcb._xy(node, "mid")),
                       "end": list(pcb._xy(node, "end")),
                       "width_mm": pcb._number(pcb._child(node, "width")[1]),
                       "length_mm": None, "length_status": "unknown", "geometry_supported": False,
                       "raw_sexpression": sexpdata.dumps(node)})
    vias = [{**v, "id": f"via:{i}"} for i, v in enumerate(inspected["via_items"])]
    net_pads, net_tracks, net_vias = defaultdict(list), defaultdict(list), defaultdict(list)
    for component in components:
        for pad in component["pads"]:
            if pad["net"]:
                net_pads[pad["net"]].append({"reference": component["reference"],
                                           **{k: pad[k] for k in ("id", "number", "x", "y", "layers")}})
    for track in tracks:
        if track["net"]:
            net_tracks[track["net"]].append(track)
    for via in vias:
        if via["net"]:
            net_vias[via["net"]].append(via)
    names = (set(codes.values()) | net_pads.keys() | net_tracks.keys() | net_vias.keys()) - {""}
    copper_layers = inspected["copper_layers"]
    # Preserve even zone-only networks and explicitly report their unsupported
    # copper contribution. Never present a segment sum as all copper length.
    zones = pcb._children(ast, "zone")
    zone_nets = {_net_name(z, codes) for z in zones} - {""}
    names |= zone_nets
    nets = []
    global_partial = bool(inspected["unsupported"])
    for name in sorted(names):
        pads, segments, holes = net_pads[name], net_tracks[name], net_vias[name]
        widths = [t["width_mm"] for t in segments]
        layers = {t["layer"] for t in segments}
        for item in pads:
            declared = item["layers"]
            if "*.Cu" in declared:
                layers.update(copper_layers)
            elif "F&B.Cu" in declared:
                layers.update(["F.Cu", "B.Cu"])
            else:
                layers.update(v for v in declared if v.endswith(".Cu"))
        for item in holes:
            declared = item["layers"]
            stack = ["F.Cu", *(f"In{i}.Cu" for i in range(1, len(copper_layers) - 1)), "B.Cu"]
            if len(declared) == 2 and all(v in copper_layers and v in stack for v in declared):
                # Declared layer tables need not be stack-order sorted.
                a, b = sorted(stack.index(v) for v in declared)
                layers.update(stack[a:b + 1])
            else:
                layers.update(declared)
        nets.append({"name": name, "pads": pads, "pad_count": len(pads),
                     "references": sorted({p["reference"] for p in pads}),
                     "track_count": len(segments), "via_count": len(holes),
                     "length_mm": sum(t["length_mm"] for t in segments if t["length_mm"] is not None),
                     "length_status": "partial" if global_partial else "complete",
                     "length_scope": "straight_segments_only_excludes_via_barrels_and_zones",
                     "min_width_mm": min(widths, default=None), "max_width_mm": max(widths, default=None),
                     "layers": sorted(layers), "connectivity_status": "not_evaluated",
                     "constraints": {"critical": name in constraints.get("critical_nets", []),
                                     "net_rules": [deepcopy(r) for r in constraints.get("net_rules", []) if name in r["nets"]],
                                     "status": "declared_not_evaluated"}})
    layers = []
    for name in dict.fromkeys([*copper_layers, *(t["layer"] for t in tracks)]):
        items = [t for t in tracks if t["layer"] == name]
        layers.append({"name": name, "declared": name in copper_layers, "track_count": len(items),
                       "length_mm": sum(t["length_mm"] for t in items if t["length_mm"] is not None),
                       "length_status": "partial" if global_partial else "complete",
                       "length_scope": "straight_segments_only_excludes_via_barrels_and_zones"})
    return {
        "components": components, "nets": nets, "tracks": tracks, "vias": vias, "layers": layers,
        "summary": {"component_count": len(components), "pad_count": sum(len(c["pads"]) for c in components),
                    "net_count": len(nets), "track_count": inspected["tracks"], "via_count": inspected["vias"],
                    "layer_count": len(copper_layers), "package_count": len({c["footprint"] for c in components})},
        "constraints": constraints,
        "coverage": {"unsupported_geometry": inspected["unsupported"],
                     "geometry_status": "partial" if global_partial else "complete_for_supported_geometry",
                     "truncated": False, "listed_track_count": len(tracks),
                     "unknown_length_track_count": sum(t["length_mm"] is None for t in tracks),
                     "zone_count": len(zones), "length_scope": "straight_segments_only_excludes_via_barrels_and_zones",
                     "length_status": "partial" if global_partial else "complete",
                     "metadata_missing": {key: sum(c[key] is None for c in components) for key in _ALIASES},
                     "metadata_conflict_count": sum(len(c["metadata_conflicts"]) for c in components),
                     "unassigned_pad_count": sum(not p["net"] for c in components for p in c["pads"]),
                     "unassigned_track_count": sum(not t["net"] for t in tracks),
                     "unassigned_via_count": sum(not v["net"] for v in vias),
                     "metadata_verification_status": check["status"]},
    }


def build_inventory(engine, project, revision) -> dict:
    """Read a sealed revision optimistically without the project write lock.

    _verified requires a registered revision; in-progress staging directories
    are not readable. Board/constraint bytes are hashed before parsing, and the
    revision plus metadata evidence are checked again before returning. Native
    writers must retain their existing locks and copy-on-write revision flow.
    Concurrent evidence publication can fail this read closed; callers may
    retry, but no stale or unverified fallback is returned. No EDA is launched.
    """
    data, folder = engine._verified(project, revision)
    design = folder / "design"
    path = catalog.artifact_path(design, design / data["board"])
    raw = path.read_bytes()
    board_sha = hashlib.sha256(raw).hexdigest()
    if board_sha != data["files"].get(data["board"]):
        raise ValueError("Inventory source hash changed during read")
    constraint_path = catalog.artifact_path(folder, folder / "constraints.json")
    constraint_raw = constraint_path.read_bytes()
    if hashlib.sha256(constraint_raw).hexdigest() != data["constraints_hash"]:
        raise ValueError("Inventory constraints hash changed during read")
    ast = sexpdata.loads(raw.decode("utf-8-sig"), nil=None, true=None, false=None)
    if pcb._tag(ast) != "kicad_pcb":
        raise ValueError("Expected a kicad_pcb S-expression")
    check = catalog.verification(engine, project, revision)
    if check["status"] == "invalid_evidence":
        raise ValueError("Inventory metadata verification evidence is invalid")
    result = _inventory(ast, pcb._inspect(ast), json.loads(constraint_raw), check)
    after, _ = engine._verified(project, revision)
    if after != data or catalog.verification(engine, project, revision) != check:
        raise ValueError("Inventory revision or metadata changed during read")
    return {"schema_version": "1.0", "project": project, "revision": revision,
            "revision_digest": data["digest"], "units": "mm", **result,
            "sources": {"board": {"path": "design/" + data["board"], "sha256": board_sha},
                        "constraints": {"path": "constraints.json", "sha256": data["constraints_hash"]},
                        "verification": {"status": check["status"], "verification_id": check.get("verification_id"),
                                         "connectivity_status": check.get("connectivity", {}).get("status")},
                        "external_resources_fetched": False}}
