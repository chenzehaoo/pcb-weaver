"""Native acceptance of the explicitly approved U102 /AN5 + /AN6 rework."""
import argparse
import json
from pathlib import Path
import shutil
from uuid import uuid4

from pcb_weaver import catalog
from pcb_weaver.joint_route import propose_joint
from pcb_weaver.repair import (_report, _net_index, compare_checks, quality_issues,
                               _erc_report, _erc_violations)
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, read_json, write_json
from pcb_weaver.repair_geometry import merge_repair


PARENT_SHA = "1c84db8a3ecb559c33c0b01bd62f4ba06f7afa34116e99bca7c16b2e30b06064"
REMOVE = ["05a8dab1-cf6e-4769-8870-18076025e233", "bd3c4cc8-3b8f-4e99-8b63-fbad7a234394",
          "565adeac-cac8-4d6c-b016-1bd6d82b1a21", "5d1c7860-af7c-4271-a99e-9e0b2ddf2ef6"]
REGION = [127.382, 106.148, 164.544, 142.446]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--order", choices=["an5-first", "an6-first"], default="an5-first")
    p.add_argument("--step", type=float, default=.05)
    p.add_argument("--exact-edges", action="store_true")
    p.add_argument("--reserve-an5", action="store_true")
    p.add_argument("--reserve-an6", action="store_true")
    p.add_argument("--refine", action="store_true")
    p.add_argument("--prefer-inner", action="store_true")
    p.add_argument("--extended-an6", action="store_true")
    p.add_argument("--whole-island", action="store_true")
    p.add_argument("--resume-candidate")
    args = p.parse_args()
    root, config = load_runtime(args.root / "data", args.root / "toolchain.unified.json")
    e = EngineeringService(root, config)
    project, revision = "system-clearance-acceptance", "r-b8fb758edb12488f"
    with e.store.lock(project):
        data, parent = e._verified(project, revision)
        source = parent / "design" / data["board"]
        if digest(source) != PARENT_SHA:
            raise ValueError("Approved baseline board has changed")
        before = e._verify(project, revision)
        if quality_issues(before):
            raise ValueError("Baseline is not eligible: " + str(quality_issues(before)))
        report = _report(e, project, revision, before)
        erc_before = _erc_violations(_erc_report(e, project, revision, before))
        index = _net_index(source)
        finding = next(r for r in report["unconnected_items"] if index[r["items"][0]["uuid"]] == "/AN5")
        native = e.toolchain.inspect_board(source)
        requests = [
            {"net": "/AN5", "region": REGION, "finding": finding, "rules": native["nets"]["/AN5"]},
            {"net": "/AN6", "region": [127.382, 106.148, 134, 114],
             "finding": {"items": [
                 {"uuid": "b97c83b5-3bab-422c-9d0b-c3ac00c51f87", "pos": {"x":129.882, "y":109.148}},
                 {"uuid": "afffd855-085c-48df-9a9a-b5979b965856", "pos": {"x":130.9358, "y":110.9646}}]},
             "rules": native["nets"]["/AN6"]}]
        for request in requests:
            request["exact_edges"] = args.exact_edges
            if args.refine:
                request["refine"] = {"region":[129.3,108,131.5,111.2], "step":.01}
        if args.reserve_an5:
            requests[1]["reservations"] = [{"layer":"F.Cu", "width":.2,
                "points":[[130.382,109.148],[130.382,110.208],[130.922,110.208],[130.922,111.4]]}]
        if args.reserve_an6:
            requests[0]["reservations"] = [{"layer":"F.Cu", "width":.2,
                "points":[[129.882,109.148],[129.882,110.52],[131,111.638]]}]
        if args.prefer_inner:
            requests[0]["layer_costs"] = {"F.Cu":3}
        if args.extended_an6:
            requests[1]["region"] = [127.382, 106.148, 136, 120]
        if args.whole_island:
            requests[1]["region"] = REGION
            requests[1]["expand_terminals"] = True
        if args.order == "an6-first":
            requests.reverse()
        if args.resume_candidate:
            child = args.resume_candidate
            existing, folder = e._verified(project, child)
            if existing.get("parent") != revision or existing.get("method") != "joint_grid" or existing.get("remove_ids") != REMOVE:
                raise ValueError("Candidate is not from this approved joint rework")
            work = catalog.artifact_path(parent, parent / "repairs" / existing["attempt"])
            result = read_json(work / "search.json")
            scope = read_json(work / "scope.json")
            if (result["status"] != "proposed" or result["source_sha256"] != PARENT_SHA
                    or digest(work / "scope.json") != result["scope_sha256"]
                    or scope["remove_ids"] != REMOVE or scope["region"] != REGION
                    or set(scope["nets"]) != {"/AN5", "/AN6"}
                    or digest(folder / "design" / data["board"]) != result["output_sha256"]):
                raise ValueError("Candidate or scope is not bound to the recorded search")
            audited = work / ("audit-" + uuid4().hex[:12] + ".kicad_pcb")
            merge_repair(source, work / "merged.kicad_pcb", audited, scope)
            if digest(audited) != result["output_sha256"]:
                raise ValueError("Candidate does not reproduce its retained-copper merge")
        else:
            work = parent / "repairs" / ("joint-" + uuid4().hex[:12])
            print(json.dumps({"stage": "search", "order": args.order, "evidence":str(work)}), flush=True)
            result = propose_joint(source, work, requests, REGION, REMOVE, args.step)
        result.update(parent_revision=revision, order=args.order, baseline_verification=before["verification_id"])
        if result["status"] == "proposed":
            _report(e, project, revision, before)
            if digest(work / "merged.kicad_pcb") != result["output_sha256"]:
                raise ValueError("Proposal changed before verification")
            if not args.resume_candidate:
                child, folder, _ = e._new(project, revision)
                shutil.copy2(work / "merged.kicad_pcb", folder / "design" / data["board"])
                e._verified(project, revision)
                e._seal(project, child, folder, data["board"], revision, "repair_candidate",
                        {"method":"joint_grid", "attempt":work.name, "remove_ids":REMOVE})
            print(json.dumps({"stage":"native_verification", "candidate":child}), flush=True)
            after = e._verify(project, child)
            new = _report(e, project, child, after)
            comparison = compare_checks(before, after, report, new, index,
                                        _net_index(folder / "design" / data["board"]), ["/AN5", "/AN6"])
            if _erc_violations(_erc_report(e, project, child, after)) - erc_before:
                comparison["reasons"].append("New native ERC findings")
            if any(comparison["after_by_net"].get(net, 0) for net in ["/AN5", "/AN6"]):
                comparison["reasons"].append("Both selected nets must be fully connected")
            comparison["accepted"] = not comparison["reasons"]
            result.update(status="improved" if comparison["accepted"] else "blocked",
                          candidate_revision=child, comparison=comparison, before=before, after=after)
        e._verified(project, revision)
        _report(e, project, revision, before)
        write_json(work / "result.json", result)
        print(json.dumps({"status":result["status"], "candidate":result.get("candidate_revision"),
                          "comparison":result.get("comparison"), "reason":result.get("reason"),
                          "steps":[{k:v for k,v in s.items() if k != "path"} for s in result["steps"]],
                          "evidence":str(work / "result.json")}), flush=True)
    # Report generation acquires its own project lock. Persist native evidence first.
    if result.get("candidate_revision"):
        catalog.generate_report(e, project, result["candidate_revision"])


if __name__ == "__main__":
    main()
