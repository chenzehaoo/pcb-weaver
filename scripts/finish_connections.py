"""Audited cleanup and additive power repair on immutable PCB revisions."""
import argparse
import json
from pathlib import Path
import shutil
from uuid import uuid4

from pcb_weaver import catalog
from pcb_weaver.board import read_board
from pcb_weaver.grid_route import propose
from pcb_weaver.controlled_neckdown import widen
from pcb_weaver.via_escape import relocate
from pcb_weaver.joint_escape import relocate as relocate_joint
from pcb_weaver.repair import (_report, _net_index, compare_checks, quality_issues,
                               _erc_report, _erc_violations, connection_counts)
from pcb_weaver.repair_geometry import prepare_repair, merge_repair, _read, _board
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, write_json

PROJECT = "system-clearance-acceptance"
BASE = "r-b8fb758edb12488f"
STUB = "afffd855-085c-48df-9a9a-b5979b965856"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--remove-confirmed-stub", action="store_true")
    parser.add_argument("--power", action="store_true")
    parser.add_argument("--allow-rule-boundary", action="store_true")
    parser.add_argument("--bounded-neckdown", action="store_true")
    parser.add_argument("--escape-width", type=float, default=.3)
    parser.add_argument("--margin", type=float, default=4)
    parser.add_argument("--fine", action="store_true")
    parser.add_argument("--compact-vias", action="store_true")
    parser.add_argument("--nudge-escapes", action="store_true")
    parser.add_argument("--bounded-bottlenecks", action="store_true")
    parser.add_argument("--final-via-escape", action="store_true")
    args = parser.parse_args()
    root, config = load_runtime(args.root / "data", args.root / "toolchain.unified.json")
    engine = EngineeringService(root, config)
    with engine.store.lock(PROJECT):
        data, parent = engine._verified(PROJECT, args.revision)
        source = parent / "design" / data["board"]
        base_data, base_folder = engine._verified(PROJECT, BASE)
        original = base_folder / "design" / base_data["board"]
        before = catalog.verification(engine, PROJECT, BASE)
        if quality_issues(before):
            raise ValueError("Baseline no longer eligible")
        before_report = _report(engine, PROJECT, BASE, before)
        previous = catalog.verification(engine, PROJECT, args.revision)
        report = _report(engine, PROJECT, args.revision, previous)
        erc_before = _erc_violations(_erc_report(engine, PROJECT, BASE, before))
        work = parent / "repairs" / ("finish-" + uuid4().hex[:12])
        work.mkdir(parents=True)
        region = read_board(source)["outline"]["bounds"]
        removed = []
        if args.remove_confirmed_stub:
            if digest(source) != "bfc912269f26e276780a93b5323ccf1a8a96cee0ad66b87341f806d8b8b8ed38":
                raise ValueError("Stub cleanup must use the reviewed exact candidate")
            if not any(v["type"] == "track_dangling" and [i["uuid"] for i in v["items"]] == [STUB] for v in report["violations"]):
                raise ValueError("Native evidence does not confirm the approved dangling branch")
            removed = [STUB]
        nets = ["/AN6", "GND", "+3.3V"] if args.power else ["/AN6"]
        nudges = []
        relocated = source
        if args.final_via_escape:
            if args.nudge_escapes or not args.power or not args.bounded_neckdown or not args.fine:
                raise ValueError("Final escape requires power, bounded neckdown and fine routing only")
            if digest(source) != "3e4b1872ccbe280b013df39317bb5c8aca5823a2c1fc8a2b4d78e39a73a79eb5":
                raise ValueError("Final escape requires the reviewed exact source")
            pro_rules = json.loads(source.with_suffix(".kicad_pro").read_text(encoding="utf-8"))["board"]["design_settings"]["rules"]
            if (pro_rules["min_via_diameter"] > .5 or pro_rules["min_through_hole_diameter"] > .4 or
                    pro_rules["min_via_annular_width"] > .05 or pro_rules["min_hole_to_hole"] > .25):
                raise ValueError("Final via would not respect persisted manufacturing minima")
            relocated = work / "final-nudge.kicad_pcb"
            proof = relocate_joint(source, relocated,
                [[135.452005,95.266843],[136.2451,95.4551],[135.7169,94.8773],[136.4476,94.8773],
                 [136.7969,95.2266],[135.382,95.3723],[135.0422,94.5204],[133.9267,95.6359]],
                [134.992,94.928])
            if proof["status"] != "proposed":
                raise ValueError(proof["reason"])
            nudges.append(proof)
            removed.extend(proof["changed_ids"])
            nets.extend(proof["nets"])
        if args.nudge_escapes:
            if digest(source) != "6d0e2468a7e9f4ceb1cd7bdb24af9a7a7fcf3ac9d1cc9d62dba4c7bd31476a47":
                raise ValueError("Via adjustment requires the reviewed exact source")
            for identity, corridor in [
                ("07d9307a-097a-4bbd-b402-f1da5cf6807c", [[212.5925,110.51],[210.9,110.51]]),
                ("f60b73c5-09bc-4119-b151-fc1538f81b55", [[134.902,93.798],[134.902,96.5]]),
            ]:
                target = work / f"nudge-{len(nudges)}.kicad_pcb"
                proof = relocate(relocated, target, identity, corridor)
                if proof["status"] != "proposed":
                    raise ValueError(proof["reason"])
                nudges.append(proof)
                removed.extend(proof["changed_ids"])
                nets.append(proof["net"])
                relocated = target
        current = work / "working.kicad_pcb"
        scope = prepare_repair(source, current, work / "empty.kicad_pcb", nets, region, removed)
        if nudges:
            current = relocated
        write_json(work / "scope.json", scope)
        scope_sha = digest(work / "scope.json")
        result = {"status":"pending", "parent":args.revision, "steps":[], "scope_sha256":scope_sha,
                  "source_sha256":digest(source), "manufacturing_authorized":False, "via_adjustments":nudges}
        write_json(work / "result.json", result)
        if args.power:
            native = engine.toolchain.inspect_board(source)
            index = _net_index(source)
            if args.bounded_neckdown and engine.toolchain.config.get("controlled_neckdown") is not True:
                raise ValueError("Controlled neckdown is not enabled by this project toolchain")
            for i, finding in enumerate(report["unconnected_items"]):
                net = index[finding["items"][0]["uuid"]]
                if net not in {"GND", "+3.3V"}:
                    continue
                xy = [item["pos"] for item in finding["items"]]
                if not 2 <= args.margin <= 10:
                    raise ValueError("Repair margin must be bounded to 2..10 mm")
                local = [max(region[0], min(p["x"] for p in xy)-args.margin), max(region[1], min(p["y"] for p in xy)-args.margin),
                         min(region[2], max(p["x"] for p in xy)+args.margin), min(region[3], max(p["y"] for p in xy)+args.margin)]
                refine = {"region":[min(p["x"] for p in xy)-2,min(p["y"] for p in xy)-2,
                                    max(p["x"] for p in xy)+2,max(p["y"] for p in xy)+2],"step":.01} if args.fine else None
                output = work / f"route-{i}.kicad_pcb"
                print(json.dumps({"stage":"search", "net":net, "pair":i, "region":local}), flush=True)
                rules = dict(native["nets"][net])
                preferred = rules["track_width"]
                minimum = max(previous["persisted_track_minima"]["global_minimum_mm"],
                              previous["persisted_track_minima"]["per_net_minimum_mm"].get(net,0))
                if args.bounded_neckdown:
                    if not minimum <= args.escape_width < preferred:
                        raise ValueError("Escape width would not respect persisted minima or native preferred width")
                    rules["track_width"] = args.escape_width
                if args.compact_vias:
                    if rules["via_drill"] != .4:
                        raise ValueError("Compact via mode requires the reviewed 0.4 mm drill")
                    rules["via_diameter"] = .6
                if args.final_via_escape:
                    rules["via_diameter"], rules["via_drill"] = .5, .4
                step = propose(current, output, net, local, finding, rules,
                               step=.1 if args.fine else .05, exact_edges=True, expand_terminals=True,
                               allow_rule_boundary=args.allow_rule_boundary, refine=refine)
                result["steps"].append(step)
                if step["status"] == "proposed":
                    if args.bounded_neckdown:
                        widened = work / f"widened-{i}.kicad_pcb"
                        physical = {c["id"]:c["geometry"] for c in _board(_read(current)[0],source=True)["copper"]}
                        contacts = [{"at":[item["pos"]["x"],item["pos"]["y"]],
                                     "layers":[physical[item["uuid"]]["layer"]]}
                                    for item in finding["items"] if item["uuid"] in physical and physical[item["uuid"]]["kind"] == "segment"]
                        proof = widen(current, output, widened, net, preferred, minimum, rules["clearance"], escape=args.escape_width, contacts=contacts,
                                      allow_bottlenecks=args.bounded_bottlenecks)
                        step["width_restoration"] = proof
                        if proof["status"] == "proposed":
                            current = widened
                        else:
                            step.update(status="blocked",reason=proof["reason"])
                    else:
                        current = output
                print(json.dumps({k:v for k,v in step.items() if k in {"status","reason","terminal_nodes","terminal_component_items","added_items","added_vias"}}), flush=True)
                write_json(work / "result.json", result)
        if digest(work / "scope.json") != scope_sha:
            raise ValueError("Scope changed during repair")
        result["patch"] = merge_repair(source, current, work / "merged.kicad_pcb", scope)
        if not result["patch"]["added"] and not result["patch"]["removed"]:
            result.update(status="no_change")
        else:
            engine._verified(PROJECT, args.revision)
            _report(engine, PROJECT, BASE, before)
            child, folder, _ = engine._new(PROJECT, args.revision)
            shutil.copy2(work / "merged.kicad_pcb", folder / "design" / data["board"])
            engine._seal(PROJECT, child, folder, data["board"], args.revision, "repair_candidate",
                         {"method":"connection_finish", "attempt":work.name, "remove_ids":removed})
            result["candidate_revision"] = child
            print(json.dumps({"stage":"native_verification", "candidate":child}), flush=True)
            after = engine._verify(PROJECT, child)
            after_report = _report(engine, PROJECT, child, after)
            comparison = compare_checks(before, after, before_report, after_report,
                                        _net_index(original), _net_index(folder / "design" / data["board"]),
                                        ["/AN5", "/AN6", "GND", "+3.3V"])
            if _erc_violations(_erc_report(engine, PROJECT, child, after)) - erc_before:
                comparison["reasons"].append("New native ERC findings")
            old_counts = connection_counts(report, _net_index(source))
            new_counts = connection_counts(after_report, _net_index(folder / "design" / data["board"]))
            if any(new_counts[net] > old_counts[net] for net in new_counts):
                comparison["reasons"].append("Connection regression relative to immediate parent")
            comparison["accepted"] = not comparison["reasons"]
            result.update(status=("passed" if after["status"] == "passed" else "improved") if comparison["accepted"] else "blocked",
                          comparison=comparison, before=before, after=after)
        engine._verified(PROJECT, args.revision)
        engine._verified(PROJECT, BASE)
        write_json(work / "result.json", result)
        print(json.dumps({"status":result["status"], "candidate":result.get("candidate_revision"),
                          "comparison":result.get("comparison"), "evidence":str(work / "result.json")}), flush=True)
    if result.get("candidate_revision"):
        catalog.generate_report(engine, PROJECT, result["candidate_revision"])


if __name__ == "__main__":
    main()
