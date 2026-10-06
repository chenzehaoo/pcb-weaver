"""Compile physical minima without weakening existing KiCad project settings."""
from pathlib import Path
from copy import deepcopy
from decimal import Decimal
import hashlib
import math
import re

import sexpdata

from .storage import read_json, write_json


# These checks are required evidence for this project's release scope.
CRITICAL_CHECKS = {
    "DRC": frozenset({"clearance", "shorting_items", "unconnected_items", "track_width",
                      "via_diameter", "drill_out_of_range", "annular_width", "hole_clearance",
                      "hole_near_hole", "copper_edge_clearance", "tracks_crossing"}),
    "ERC": frozenset({"pin_not_connected", "pin_not_driven", "power_pin_not_driven", "pin_to_pin"}),
}


def project_rule_issues(board_path: Path) -> list[str]:
    path = board_path.with_suffix(".kicad_pro")
    project = read_json(path) if path.exists() else {}
    settings = (("DRC", project.get("board", {}).get("design_settings", {}), "drc_exclusions"),
                ("ERC", project.get("erc", {}), "erc_exclusions"))
    issues = []
    for kind, section, exclusions in settings:
        for check, severity in section.get("rule_severities", {}).items():
            if check in CRITICAL_CHECKS[kind] and severity != "error":
                issues.append(f"{kind} critical check {check} must remain error, not {severity!r}")
        if section.get(exclusions):
            issues.append(f"{kind} exclusions require reviewed waivers, which are not supported")
    custom_path = board_path.with_suffix(".kicad_dru")
    if custom_path.exists():
        # A custom rule can independently downgrade a violation despite project severities.
        rules = sexpdata.loads("(" + custom_path.read_text(encoding="utf-8-sig") + "\n)",
                               nil=None, true=None, false=None)
        for rule in rules:
            if not isinstance(rule, list) or len(rule) < 2 or str(rule[0]) != "rule":
                continue
            for item in rule[2:]:
                if isinstance(item, list) and item and str(item[0]) == "severity":
                    if len(item) != 2 or str(item[1]) != "error":
                        issues.append(f"DRC custom rule {rule[1]!r} has non-error severity; reviewed waivers are not supported")
    return issues


def report_rule_issues(report: dict, kind: str) -> list[str]:
    issues = []
    if report.get("excluded"):
        issues.append(f"{kind} excluded findings require reviewed waivers, which are not supported")
    ignored = report.get("ignored_checks") or []
    if not isinstance(ignored, list) or any(not isinstance(check, str) for check in ignored):
        issues.append(f"{kind} ignored-check evidence has an unsupported format")
    else:
        for check in sorted(set(ignored) & CRITICAL_CHECKS[kind]):
            issues.append(f"{kind} critical check {check} was ignored")
    return issues


def _nonnegative_number(value, setting):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{setting} must be a finite nonnegative number")
    return value


def _compile_via_dimensions(item, default, drill_floor, diameter_floor, annular):
    explicit_drill = item.get("via_drill") is not None
    explicit_diameter = item.get("via_diameter") is not None
    default_drill = _nonnegative_number(default.get("via_drill") or 0, "Default.via_drill")
    default_diameter = _nonnegative_number(default.get("via_diameter") or 0, "Default.via_diameter")
    drill = _nonnegative_number(item["via_drill"] if explicit_drill else default_drill,
                                item["name"] + ".via_drill")
    diameter = _nonnegative_number(item["via_diameter"] if explicit_diameter else default_diameter,
                                   item["name"] + ".via_diameter")
    effective_drill = max(drill, drill_floor)
    # Decimal arithmetic avoids inflating an exactly legal 0.6/0.4/0.1 mm
    # diameter through binary floating-point addition.
    annular_floor = float(Decimal(str(effective_drill)) + 2 * Decimal(str(annular)))
    effective_diameter = max(diameter, diameter_floor, annular_floor)
    if item["name"] == "Default" or explicit_drill or effective_drill > default_drill:
        item["via_drill"] = effective_drill
    if item["name"] == "Default" or explicit_diameter or effective_diameter > default_diameter:
        item["via_diameter"] = effective_diameter


def compile_rules(board_path: Path, constraints: dict, native_state: dict | None = None) -> dict:
    issues = project_rule_issues(board_path)
    if issues:
        raise ValueError("Project rules block verification: " + "; ".join(issues))
    project_path = board_path.with_suffix(".kicad_pro")
    project = read_json(project_path) if project_path.exists() else {"meta": {"version": 1}}
    fab = constraints["fabrication"]
    rules = project.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
    changes = []
    for key, value in (("min_track_width", fab["min_track_mm"]),
                       ("min_clearance", fab["min_clearance_mm"]),
                       ("min_through_hole_diameter", fab["min_via_drill_mm"])):
        old = rules.get(key, 0)
        rules[key] = max(_nonnegative_number(old, key), _nonnegative_number(value, key + " fabrication floor"))
        changes.append({"setting": key, "previous": old, "compiled": rules[key]})
    annular = _nonnegative_number(rules.get("min_via_annular_width", 0.2), "min_via_annular_width")
    via_minimum = _nonnegative_number(rules.get("min_via_diameter", 0), "min_via_diameter")
    net_settings = project.setdefault("net_settings", {})
    # KiCad may discard this nested settings section when its schema is absent.
    # Preserve explicit legacy/newer versions so KiCad owns their migration.
    net_settings.setdefault("meta", {}).setdefault("version", 3)
    classes = net_settings.setdefault("classes", [])
    if not classes:
        classes.append({"name": "Default", "clearance": 0.2, "track_width": 0.25,
                        "via_diameter": 0.8, "via_drill": 0.4,
                        "microvia_diameter": 0.3, "microvia_drill": 0.1,
                        "diff_pair_width": 0.25, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
                        "wire_width": 6, "bus_width": 12})
    # Manufacturing floors apply globally; named-net widths do not.
    width = rules["min_track_width"]
    by_name = {item["name"]: item for item in classes}
    if len(by_name) != len(classes) or "Default" not in by_name:
        raise ValueError("Netclasses require one unique Default class")
    for item in classes:
        for key in ("via_drill", "via_diameter"):
            if item.get(key) is not None:
                _nonnegative_number(item[key], item["name"] + "." + key)
    default = by_name["Default"]
    for item in classes:
        for key, floor in (("clearance", rules["min_clearance"]), ("track_width", width)):
            # In composite classes, unset means inherit, not zero.
            if item["name"] == "Default" or item.get(key) is not None:
                item[key] = max(float(item.get(key) or 0), floor)
    # Resolve Default first, even when it is not first in the stored class list.
    for item in [default, *[item for item in classes if item is not default]]:
        _compile_via_dimensions(item, default, rules["min_through_hole_diameter"], via_minimum, annular)
    requested = {}
    for rule in constraints["net_rules"]:
        for net in rule["nets"]:
            if not isinstance(net, str) or not net:
                raise ValueError("Per-net rules require nonempty literal net names")
            requested[net] = max(requested.get(net, width), rule["min_width_mm"])
    assignments = net_settings.get("netclass_assignments") or {}
    if not isinstance(assignments, dict):
        raise ValueError("Unsupported netclass assignment schema")
    if native_state is not None:
        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if (native_state.get("status") != "ok" or native_state.get("board_sha256") != digest(board_path)
                or native_state.get("project_sha256") != digest(project_path)):
            raise ValueError("Native rule inspection is missing, stale or belongs to another board/project")
    elif requested and (assignments or net_settings.get("netclass_patterns") or len(classes) != 1):
        raise ValueError("Existing assignments/patterns require Toolchain.inspect_board() before per-net compilation")
    schema = net_settings["meta"]["version"]
    if requested and schema not in (3, 4):
        raise ValueError("Per-net compilation supports net_settings schema 3 or 4; migrate legacy projects with KiCad first")
    version = re.match(r"(\d+)\.", native_state.get("version", "")) if native_state else None
    modern = bool(version and int(version[1]) in (9, 10))
    if requested and schema == 3 and modern:
        # Encode the already-inspected native migration without changing original priorities.
        if any(not isinstance(name, str) for name in assignments.values()):
            raise ValueError("Schema 3 netclass assignments must be strings")
        assignments = {net: [name] if name else [] for net, name in assignments.items()}
        for item in classes:
            item["priority"] = native_state["class_priorities"].get(item["name"], 2147483647)
        net_settings["meta"]["version"] = schema = 4
    custom = board_path.with_suffix(".kicad_dru")
    if requested and custom.is_file():
        expressions = sexpdata.loads("(" + custom.read_text(encoding="utf-8-sig") + "\n)", nil=None, true=None, false=None)
        def atoms(value):
            if isinstance(value, list):
                for child in value:
                    yield from atoms(child)
            elif isinstance(value, str):
                yield value
        conditions = list(atoms(expressions))
        if any("hasExactNetclass" in expr or "NetClass" in re.sub(r"[AB]\.NetClass\s*(?:==|!=)\s*['\"][^,'\"]+['\"]", "", expr)
               or (schema == 3 and re.search(r"NetClass|hasNetclass", expr)) for expr in conditions):
            raise ValueError("Per-net reclassification cannot preserve exact/legacy netclass custom-rule conditions")
    owned = project.get("pcb_weaver_net_rules", {})
    generated = []
    for net, minimum in sorted(requested.items()):
        name = "PCBWeaver_" + hashlib.sha256(net.encode("utf-8")).hexdigest()[:16]
        if name in by_name and owned.get(net, {}).get("class") != name:
            raise ValueError("Generated netclass name conflicts with an existing user class: " + name)
        if native_state is not None:
            if net not in native_state.get("nets", {}):
                raise ValueError("Per-net rule references an absent board net: " + net)
            effective = native_state["nets"][net]
            # Composite membership preserves colors, schematic properties and
            # unknown source-class settings through inheritance, not copies.
            base = {} if schema == 4 else deepcopy(by_name.get(effective["class_name"], by_name["Default"]))
            if not effective.get("class_membership_verified", True):
                raise ValueError("Native class membership is ambiguous: " + net)
            base.update({key: value for key, value in effective.items()
                         if key not in ("class_name", "class_names", "class_membership_verified")})
        else:
            base = deepcopy(by_name["Default"])
        owned_names = {entry["class"] for entry in owned.values()}
        base.update(name=name, track_width=max(minimum, width, base.get("track_width", 0)),
                    priority=min([0, *[c.get("priority", 0) for c in classes if c["name"] not in owned_names]]) - 1)
        base["clearance"] = max(base.get("clearance", 0), rules["min_clearance"])
        _compile_via_dimensions(base, default, rules["min_through_hole_diameter"], via_minimum, annular)
        previous = assignments.get(net)
        members = native_state["nets"][net].get("class_names", []) if native_state else []
        if schema == 4:
            if previous is not None and not isinstance(previous, list):
                raise ValueError("Schema 4 netclass assignments must be arrays")
            assignments[net] = list(dict.fromkeys([*(previous or []), *members, name]))
        else:
            if previous is not None and not isinstance(previous, str):
                raise ValueError("Schema 3 netclass assignments must be strings")
            assignments[net] = name
        prior = owned.get(net, {})
        prior_minimum = prior.get("declared_min_width_mm", prior.get("minimums", {}).get("track_width", 0))
        owned[net] = {"class": name, "previous_assignment": prior.get("previous_assignment", previous),
                      "declared_min_width_mm": max(minimum, prior_minimum),
                      "required_memberships": members if schema == 4 else [],
                      "minimums": {key: base[key] for key in ("track_width", "clearance", "via_drill", "via_diameter",
                          "microvia_diameter", "microvia_drill", "diff_pair_width", "diff_pair_gap", "diff_pair_via_gap")
                                   if base.get(key) is not None}}
        generated.append(base)
    if generated:
        names = {item["name"] for item in generated}
        classes[:] = generated + [item for item in classes if item["name"] not in names]
        net_settings["netclass_assignments"] = assignments
        project["pcb_weaver_net_rules"] = owned
    write_json(project_path, project)
    return {"status": "ok", "changes": changes, "routing_width_floor_mm": width,
            "via_policy": {"minimum_annular_width_mm": annular,
                           "annular_source": "project.min_via_annular_width" if "min_via_annular_width" in rules else "conservative_missing_rule_fallback",
                           "minimum_via_diameter_mm": via_minimum,
                           "minimum_drill_mm": rules["min_through_hole_diameter"],
                           "preserve_existing_larger_preferences": True},
            "net_classes": {net: entry["class"] for net, entry in owned.items()},
            "per_net_widths_mm": {net: entry["minimums"]["track_width"] for net, entry in owned.items()},
            "high_speed_supported": False,
            "policy": "Per-net derived classes preserve native effective minima; no impedance or timing synthesis"}
