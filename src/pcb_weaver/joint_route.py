"""Explicit-scope sequential routing proposals; never adoption authorization."""
from pathlib import Path

from .grid_route import propose
from .repair_geometry import prepare_repair, merge_repair, _inside
from .storage import digest, write_json


def propose_joint(source, folder, requests, region, remove_ids, step=.05):
    source, folder = Path(source), Path(folder)
    nets = [r["net"] for r in requests]
    if not requests or len(set(nets)) != len(nets):
        raise ValueError("Joint routing needs distinct explicitly selected nets")
    if any(not _inside(r["region"], region) for r in requests):
        raise ValueError("Per-net routing bounds exceed approved joint scope")
    folder.mkdir(exist_ok=False)
    working = folder / "working.kicad_pcb"
    manifest = prepare_repair(source, working, folder / "empty.kicad_pcb", nets, region, remove_ids)
    write_json(folder / "scope.json", manifest)
    scope_sha = digest(folder / "scope.json")
    result = {"status": "blocked", "source_sha256": digest(source), "scope_sha256": scope_sha,
              "nets": nets, "steps": [], "manufacturing_authorized": False,
              "requires_native_verification": True}
    for i, request in enumerate(requests):
        output = folder / f"route-{i}.kicad_pcb"
        item = propose(working, output, request["net"], request["region"],
                       request["finding"], request["rules"], step=step,
                       exact_edges=request.get("exact_edges", False), reservations=request.get("reservations"),
                       refine=request.get("refine"), layer_costs=request.get("layer_costs"),
                       expand_terminals=request.get("expand_terminals", False))
        result["steps"].append(item)
        write_json(folder / "search.json", result)
        if item["status"] != "proposed":
            result["reason"] = "No path for " + request["net"]
            write_json(folder / "search.json", result)
            return result
        working = output
    if digest(folder / "scope.json") != scope_sha:
        raise ValueError("Joint scope changed during search")
    result["patch"] = merge_repair(source, working, folder / "merged.kicad_pcb", manifest)
    if result["patch"]["dropped_outside_region"] or result["patch"]["dropped_unselected_nets"]:
        raise ValueError("Joint search produced out-of-scope copper")
    result.update(status="proposed", output_sha256=digest(folder / "merged.kicad_pcb"))
    write_json(folder / "search.json", result)
    return result
