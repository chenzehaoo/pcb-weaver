"""Isolated additive proposal and native comparison against an explicit revision."""
import argparse
import json
from pathlib import Path
import shutil
from uuid import uuid4

from pcb_weaver import catalog
from pcb_weaver.grid_route import propose
from pcb_weaver.repair import _report, _net_index, connection_counts, compare_checks, _erc_report, _erc_violations
from pcb_weaver.repair_geometry import prepare_repair, merge_repair
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import write_json


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--net", required=True)
    p.add_argument("--region", nargs=4, type=float, required=True)
    p.add_argument("--step", type=float, default=.1)
    args = p.parse_args()
    root, config = load_runtime(args.root / "data", args.root / "toolchain.unified.json")
    e = EngineeringService(root, config)
    with e.store.lock(args.project):
        data, parent = e._verified(args.project, args.revision)
        source = parent / "design" / data["board"]
        work = parent / "repairs" / ("grid-" + uuid4().hex[:12])
        work.mkdir(parents=True)
        before = e._verify(args.project, args.revision)
        report = _report(e, args.project, args.revision, before)
        index = _net_index(source)
        finding = next(r for r in report["unconnected_items"] if index[r["items"][0]["uuid"]] == args.net)
        rules = e.toolchain.inspect_board(source)["nets"][args.net]
        result = propose(source, work / "proposal.kicad_pcb", args.net, args.region, finding, rules, step=args.step)
        print(json.dumps({k:v for k,v in result.items() if k not in {"path", "rules"}}), flush=True)
        if result["status"] == "proposed":
            manifest = prepare_repair(source, work / "working.kicad_pcb", work / "empty.kicad_pcb", [args.net], args.region, [])
            patch = merge_repair(source, work / "proposal.kicad_pcb", work / "merged.kicad_pcb", manifest)
            child, folder, _ = e._new(args.project, args.revision)
            shutil.copy2(work / "merged.kicad_pcb", folder / "design" / data["board"])
            e._verified(args.project, args.revision)
            e._seal(args.project, child, folder, data["board"], args.revision, "repair_candidate", {"method": "additive_grid", "attempt": work.name})
            after = e._verify(args.project, child)
            new = _report(e, args.project, child, after)
            comparison = compare_checks(before, after, report, new, index, _net_index(folder / "design" / data["board"]), [args.net])
            if _erc_violations(_erc_report(e, args.project, child, after)) - _erc_violations(_erc_report(e, args.project, args.revision, before)):
                comparison["accepted"] = False
                comparison["reasons"].append("New native ERC findings")
            result.update(status="improved" if comparison["accepted"] else "blocked", candidate_revision=child,
                          comparison=comparison, before=before, after=after, patch=patch)
        e._verified(args.project, args.revision)
        write_json(work / "result.json", result)
        print(json.dumps({"status":result["status"], "candidate":result.get("candidate_revision"),
                          "comparison":result.get("comparison"), "evidence":str(work / "result.json")}), flush=True)
    if result.get("candidate_revision"):
        catalog.generate_report(e, args.project, result["candidate_revision"])


if __name__ == "__main__":
    main()
