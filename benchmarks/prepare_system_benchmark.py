"""Reproducibly derive an unrouted benchmark without inventing a circuit."""
import argparse
from collections import Counter
from copy import deepcopy
from fnmatch import fnmatchcase
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import sexpdata

from inspect_candidates import children, inventory


COMMIT = "286b0611feca00727bf70bfa184ec2c28a745dc3"
PROJECT = "kit-dev-coldfire-xilinx_5213"
ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "benchmarks" / "upstream" / "kicad-source"
sys.path.insert(0, str(ROOT / "src"))
from pcb_weaver.board import read_board


def load(path):
    return sexpdata.loads(path.read_text(encoding="utf-8-sig"), nil=None, true=None, false=None)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def properties(tree):
    return {str(node[1]): node for node in children(tree, "property")}


def prepare(source, destination):
    if destination.exists():
        raise ValueError("Destination already exists; never overwrite a benchmark under evaluation")
    shutil.copytree(source, destination)
    board_path = destination / (PROJECT + ".kicad_pcb")
    original = load(board_path)
    inspected = read_board(board_path)
    old_outline = inspected["outline"]["bounds"]
    envelopes = [old_outline, *[fp["bounds"] for fp in inspected["footprints"]]]
    expanded_outline = [math.floor(min(b[0] for b in envelopes) - 5), math.floor(min(b[1] for b in envelopes) - 5),
                        math.ceil(max(b[2] for b in envelopes) + 5), math.ceil(max(b[3] for b in envelopes) + 5)]
    footprint_ids = {str(properties(fp)["Reference"][2]): str(fp[1]) for fp in children(original, "footprint")}
    schematic_changes = []
    schematic_trees = {p: load(p) for p in sorted(destination.glob("*.kicad_sch"))}
    allowed_packages = {}
    for schematic, tree in schematic_trees.items():
        for symbol in children(tree, "symbol"):
            props = properties(symbol)
            ref = str(props["Reference"][2])
            if ref not in footprint_ids:
                continue
            lib_id = str(children(symbol, "lib_id")[0][1])
            allowed_packages.setdefault(lib_id, set()).add(footprint_ids[ref].split(":", 1)[1])
            field = props.get("Footprint")
            if field is None:
                raise ValueError(f"Missing explicit footprint for {ref}")
            if str(field[2]) != footprint_ids[ref]:
                schematic_changes.append({"sheet": schematic.name, "reference": ref,
                                          "before": str(field[2]), "after": footprint_ids[ref]})
                field[2] = footprint_ids[ref]
    filter_changes = []

    def update_filter(symbol, lib_id, location):
        field = properties(symbol).get("ki_fp_filters")
        if field is None or not str(field[2]):
            return
        patterns = str(field[2]).split()
        additions = [name for name in sorted(allowed_packages.get(lib_id, []))
                     if not any(fnmatchcase(name.lower(), pattern.lower()) for pattern in patterns)]
        if additions:
            before = str(field[2])
            field[2] = " ".join(patterns + additions)
            filter_changes.append({"location": location, "symbol": lib_id, "before": before, "after": field[2]})

    for schematic, tree in schematic_trees.items():
        for cache in children(tree, "lib_symbols"):
            for symbol in children(cache, "symbol"):
                update_filter(symbol, str(symbol[1]), schematic.name)
        schematic.write_text(sexpdata.dumps(tree) + "\n", encoding="utf-8")
    for library in destination.glob("*.kicad_sym"):
        tree = load(library)
        for symbol in children(tree, "symbol"):
            update_filter(symbol, library.stem + ":" + str(symbol[1]), library.name)
        library.write_text(sexpdata.dumps(tree) + "\n", encoding="utf-8")
    removed = Counter()
    derived = [deepcopy(original[0])]
    for node in original[1:]:
        tag = str(node[0]) if isinstance(node, list) and node else ""
        layer = children(node, "layer") if isinstance(node, list) else []
        copper_graphic = tag.startswith("gr_") and layer and str(layer[0][1]).endswith(".Cu")
        edge = tag.startswith("gr_") and layer and str(layer[0][1]) == "Edge.Cuts"
        if tag in {"segment", "via", "arc", "zone"} or copper_graphic or edge:
            removed[tag] += 1
        else:
            derived.append(deepcopy(node))
    derived.append(sexpdata.loads(
        f'(gr_rect (start {expanded_outline[0]} {expanded_outline[1]}) '
        f'(end {expanded_outline[2]} {expanded_outline[3]}) (stroke (width 0.05) (type default)) '
        f'(fill none) (layer "Edge.Cuts") (uuid "{uuid.uuid5(uuid.NAMESPACE_URL, COMMIT + "/benchmark-outline")}"))'))
    board_path.write_text(sexpdata.dumps(derived) + "\n", encoding="utf-8")
    project_path = board_path.with_suffix(".kicad_pro")
    project = json.loads(project_path.read_text())
    severity_changes = []
    for kind, settings in (("DRC", project["board"]["design_settings"]), ("ERC", project["erc"])):
        for check, severity in settings["rule_severities"].items():
            target = "warning" if severity == "ignore" else severity
            if check in {"pin_to_pin", "footprint_symbol_mismatch", "missing_footprint", "extra_footprint", "net_conflict"}:
                target = "error"
            if target != severity:
                severity_changes.append({"kind": kind, "check": check, "before": severity, "after": target})
                settings["rule_severities"][check] = target
        if settings.get("drc_exclusions") or settings.get("erc_exclusions"):
            raise ValueError("Upstream has waivers; do not silently drop them")
    project.setdefault("text_variables", {}).update({"BENCHMARK_SOURCE_COMMIT": COMMIT,
                                                    "BENCHMARK_SCOPE": "Unrouted derivative; no product or manufacturing approval"})
    write_json(project_path, project)
    metrics = inventory(board_path)
    fps = children(derived, "footprint")
    fixed = [str(properties(fp)["Reference"][2]) for fp in fps
             if any(token in str(fp[1]) for token in ("PinHeader", "DSUB", "JACK", "Altech"))]
    constraints = {"schema_version": 1, "board": {"layers": 4, "max_voltage": 24,
                    "edge_clearance_mm": 0.5, "component_gap_mm": 0.25},
                   "fixed_references": sorted(fixed), "critical_nets": [],
                   "proximity": [{"reference": "Y101", "target": "U102", "max_distance_mm": 25}],
                   "regions": [], "net_rules": [],
                   "fabrication": {"min_track_mm": 0.2, "min_clearance_mm": 0.15, "min_via_drill_mm": 0.4},
                   "release": {"require_erc": True},
                   "placement": {"algorithm": "auto", "seed": 1729, "max_iterations": 80, "passes": 4, "block_size": 8}}
    write_json(destination / "constraints.json", constraints)
    provenance = {"name": "PCB Weaver System Controller Benchmark", "upstream_project": PROJECT,
                  "upstream_repository": "https://github.com/KiCad/kicad-source-mirror", "upstream_tag": "9.0.0",
                  "upstream_commit": COMMIT, "upstream_directory": "demos/" + PROJECT,
                  "license": "CC-BY-SA-4.0", "license_evidence": "LICENSE.KiCad.README",
                  "copyright": "Original KiCad demo contributors; title/date/revision notices retained",
                  "derivative_notice": "PCB Weaver benchmark derivative. Not an original reference circuit or production-approved board.",
                  "purpose": "Native four-layer ERC/parity and placement/routing regression with a real multi-module circuit",
                  "source_hashes": {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in sorted(source.rglob("*")) if p.is_file()},
                  "removed_copper_objects": dict(removed), "schematic_footprint_id_changes": schematic_changes,
                  "outline_change": {"before": old_outline, "after": expanded_outline,
                                     "reason": "Software optimization envelope: existing off-board connector bodies fit with 5 mm margin; not original mechanical outline"},
                  "explicit_footprint_filter_updates": filter_changes,
                  "severity_changes": severity_changes,
                  "preserved": ["all components and values", "all pin types and schematic wires", "all pad/net assignments",
                                "original component positions, rotations and sides", "four-layer stackup",
                                "native local footprints and symbol libraries"],
                  "metrics": metrics, "limitations": ["14 backside capacitors require mirrored geometry support",
                      "J201 has a native slotted drill requiring directional drill-envelope handling",
                      "TSSOP is not present; the two QFP and SOIC families are genuine",
                      "Global libraries, 3D rendering, firmware, SI/PI, EMC and manufacture are not certified"]}
    provenance["metrics"]["path"] = board_path.name
    write_json(destination / "PROVENANCE.json", provenance)
    shutil.copy2(UPSTREAM / "LICENSE.README", destination / "LICENSE.KiCad.README")
    print(json.dumps({"destination": str(destination), "metrics": metrics,
                      "footprint_id_changes": len(schematic_changes), "removed": removed}, indent=2))
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "examples" / "system-controller")
    args = parser.parse_args()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=UPSTREAM, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--", "demos/" + PROJECT], cwd=UPSTREAM, text=True)
    if head != COMMIT or dirty:
        raise ValueError("Expected the clean pinned upstream demo before deriving the benchmark")
    prepare(UPSTREAM / "demos" / PROJECT, args.output.resolve())
