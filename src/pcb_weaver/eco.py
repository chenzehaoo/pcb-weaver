"""Explain one-hop net and artifact dependencies for an engineering revision."""
from collections import defaultdict

from .storage import canonical


def analyze_impact(before: dict, after: dict, diff: dict, constraints: dict, changed_files: list[str], constraints_changed: bool) -> dict:
    direct = set(diff["added"]) | set(diff["removed"])
    reasons = defaultdict(set)
    for name in direct:
        reasons[name].add("component_added_or_removed")
    for item in diff["moved"]:
        direct.add(item["reference"])
        reasons[item["reference"]].add("placement_changed")
    for item in diff["modified"]:
        direct.add(item["reference"])
        reasons[item["reference"]].add("component_attributes_changed")
    for item in diff["connection_changes"]:
        direct.add(item["reference"])
        reasons[item["reference"]].add("pad_connectivity_changed")
    members = defaultdict(set)
    direct_nets = defaultdict(set)
    for board in (before, after):
        for footprint in board["footprints"]:
            for pad in footprint["pads"]:
                net = pad["net"]
                if net:
                    members[net].add(footprint["reference"])
                    if footprint["reference"] in direct:
                        direct_nets[net].update(reasons[footprint["reference"]])

    def copper(board):
        result = defaultdict(list)
        for item in board.get("track_items", []) + board.get("via_items", []):
            if item.get("net"):
                result[item["net"]].append(canonical(item))
        return {net: sorted(items) for net, items in result.items()}

    old_copper, new_copper = copper(before), copper(after)
    for net in set(old_copper) | set(new_copper):
        if old_copper.get(net) != new_copper.get(net):
            direct_nets[net].add("copper_geometry_changed")
    global_rules = constraints_changed or any(name.endswith((".kicad_pro", ".kicad_dru")) for name in changed_files)
    if global_rules:
        for net in members:
            direct_nets[net].add("project_or_constraint_rules_changed")
    affected_refs = direct | {ref for net in direct_nets for ref in members[net]}
    affected_rules = []
    for index, rule in enumerate(constraints.get("proximity", [])):
        if {rule["reference"], rule["target"]} & affected_refs:
            affected_rules.append({"constraint": f"proximity[{index}]", "reason": "affected_component"})
    for index, rule in enumerate(constraints.get("regions", [])):
        if set(rule["references"]) & affected_refs:
            affected_rules.append({"constraint": f"regions[{index}]", "reason": "affected_component"})
    for index, rule in enumerate(constraints.get("net_rules", [])):
        if set(rule["nets"]) & direct_nets.keys():
            affected_rules.append({"constraint": f"net_rules[{index}]", "reason": "affected_net"})
    if global_rules:
        affected_rules.append({"constraint": "board/fabrication", "reason": "global_rule_change"})
    changed = bool(changed_files or constraints_changed)
    return {"direct_references": sorted(direct),
            "affected_references": sorted(affected_refs),
            "affected_nets": [{"net": net, "reasons": sorted(why), "references": sorted(members[net])} for net, why in sorted(direct_nets.items())],
            "affected_constraints": affected_rules,
            "global_rules_changed": global_rules,
            "invalidated_artifacts": ["layout_plans", "DRC", "ERC", "connectivity", "manufacturing_package"] if changed else [],
            "required_checks": ["full_board_DRC", "root_schematic_ERC", "schematic_board_parity", "declared_constraints"] if changed else [],
            "scope": "One-hop net membership and direct geometry/rule dependency evidence. Not timing, power or functional propagation; no automatic copper repair."}
