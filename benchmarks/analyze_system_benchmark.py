"""Measure real exported connectivity and current adapter coverage, never emulate EDA."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pcb_weaver.board import read_board
from pcb_weaver.compiler import project_rule_issues
from pcb_weaver.models import Constraints
from pcb_weaver.netlist import inspect_netlist
from pcb_weaver.planning import audit_constraints
from pcb_weaver.service import design_files


def net_signature(path):
    root = ET.parse(path).getroot()
    return {net.get("name"): sorted((node.get("ref"), node.get("pin")) for node in net.findall("node"))
            for net in root.findall("./nets/net")}


def analyze(project, evidence):
    board_path = next(project.glob("*.kicad_pcb"))
    board = read_board(board_path)
    intent = Constraints.model_validate_json((project / "constraints.json").read_text()).model_dump(mode="json")
    netlist = evidence / "netlist.xml"
    root = ET.parse(netlist).getroot()
    comps = {c.get("ref"): c for c in root.findall("./components/comp")}
    modules = {ref: c.find("sheetpath").get("names") for ref, c in comps.items()}
    graph = {ref: set() for ref in comps}
    cross_module = []
    for net in root.findall("./nets/net"):
        refs = {n.get("ref") for n in net.findall("node")}
        for ref in refs:
            graph[ref].update(refs - {ref})
        sheets = {modules[ref] for ref in refs}
        if len(sheets) > 1:
            cross_module.append({"net": net.get("name"), "modules": sorted(sheets), "references": sorted(refs)})
    connected = []
    remaining = set(graph)
    while remaining:
        pending, found = [next(iter(remaining))], set()
        while pending:
            ref = pending.pop()
            if ref not in found:
                found.add(ref)
                pending.extend(graph[ref] - found)
        remaining -= found
        connected.append(sorted(found))
    parity = inspect_netlist(netlist, board)
    audit = audit_constraints(board, intent)
    files = design_files(project)
    result = {
        "design_hashes": files, "board": board_path.name,
        "metrics": {"components": len(board["footprints"]), "physical_pads": sum(len(f["pads"]) for f in board["footprints"]),
                    "electrical_pad_identities": len({(f["reference"], p["number"]) for f in board["footprints"] for p in f["pads"] if p["number"]}),
                    "nets": len(board["nets"]), "copper_layers": board["copper_layers"], "outline": board["outline"],
                    "multi_terminal_nets": sum(len(net.findall("node")) > 1 for net in root.findall("./nets/net")),
                    "single_terminal_nets": sum(len(net.findall("node")) == 1 for net in root.findall("./nets/net")),
                    "modules": dict(Counter(modules.values())), "cross_module_nets": len(cross_module),
                    "connected_component_sizes": sorted([len(c) for c in connected], reverse=True),
                    "isolated_references": sorted(ref for ref in graph if not graph[ref]),
                    "pin_types": dict(Counter(pin.get("type") for pin in root.findall("./libparts/libpart/pins/pin")))},
        "cross_module_nets": cross_module, "adapter_netlist_parity": parity,
        "upstream_netlist_unchanged": net_signature(netlist) == net_signature(ROOT / "benchmarks/results/upstream-9.0.0/netlist.xml"),
        "project_rule_issues": project_rule_issues(board_path), "adapter_audit": audit,
        "unsupported_geometry": board["unsupported"],
        "adapter_code_hashes": {name: hashlib.sha256((ROOT / "src/pcb_weaver" / name).read_bytes()).hexdigest()
                                for name in ("board.py", "planning.py", "service.py", "compiler.py", "netlist.py")},
        "scope": "Measured structural/electrical regression coverage. A connected graph and ERC pass do not certify circuit function or SI/PI."}
    return result


if __name__ == "__main__":
    result = analyze(ROOT / "examples/system-controller", ROOT / "benchmarks/results/system-controller-frozen")
    (ROOT / "benchmarks/SYSTEM_BASELINE.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"metrics": result["metrics"], "parity": result["adapter_netlist_parity"]["status"],
                      "upstream_netlist_unchanged": result["upstream_netlist_unchanged"],
                      "project_rule_issues": result["project_rule_issues"],
                      "unsupported_geometry": result["unsupported_geometry"],
                      "audit_status": result["adapter_audit"]["status"]}, indent=2))
