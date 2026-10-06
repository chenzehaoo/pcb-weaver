"""Bounded clearance repair acceptance on a recorded system-controller candidate."""
import argparse
import json
from pathlib import Path
import shutil

from pcb_weaver import catalog
from pcb_weaver.clearance import propose
from pcb_weaver.repair import _report, _net_index, connection_counts, _erc_report, _erc_violations
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import read_json, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    workspace, config = load_runtime(root / "data", root / "toolchain.unified.json")
    engine = EngineeringService(workspace, config)
    donor_project, donor_revision = "system-controller", "r-62cd6db138694725"
    project = "system-clearance-acceptance"
    with engine.store.lock(project):
        donor, donor_folder = engine._verified(donor_project, donor_revision)
        parent, copy_folder, _ = engine._new(project)
        shutil.copytree(donor_folder / "design", copy_folder / "design", dirs_exist_ok=True)
        shutil.copy2(donor_folder / "constraints.json", copy_folder / "constraints.json")
        engine._verified(donor_project, donor_revision)
        engine._seal(project, parent, copy_folder, donor["board"], None, "repair_benchmark_copy",
                     {"donor_project": donor_project, "donor_revision": donor_revision, "donor_digest": donor["digest"]})
        before = engine._verify(project, parent)
        before_report = _report(engine, project, parent, before)
        before_erc = _erc_violations(_erc_report(engine, project, parent, before))
        old, parent_folder = engine._verified(project, parent)
        index = _net_index(parent_folder / "design" / old["board"])
        baseline_connections = connection_counts(before_report, index)
        child, folder, old = engine._new(project, parent)
        work = folder / "clearance-repair"
        work.mkdir()
        current = folder / "design" / old["board"]
        scopes = [(["/xilinx/XIL_D12", "/xilinx/XIL_D33", "/xilinx/XIL_D34"], [211.9, 117.2, 217.6, 119.5]),
                  (["/inout_user/RTS1"], [155.8, 79.9, 157.9, 81.5])]
        record = {"status": "blocked", "project": project, "parent": parent,
                  "before": before, "proposals": [], "manufacturing_authorized": False}
        for i, (nets, region) in enumerate(scopes):
            target = work / f"candidate-{i}.kicad_pcb"
            result = propose(current, target, nets, region)
            record["proposals"].append(result)
            print(json.dumps({k: v for k, v in result.items() if k != "changes"}), flush=True)
            if result["status"] != "proposed":
                break
            current = target
        else:
            shutil.copy2(current, folder / "design" / old["board"])
            engine._verified(project, parent)
            engine._seal(project, child, folder, old["board"], parent, "repair_candidate",
                         {"repair_method": "bounded_clearance_joints", "scopes": scopes})
            after = engine._verify(project, child)
            report = _report(engine, project, child, after)
            after_connections = connection_counts(report, _net_index(folder / "design" / old["board"]))
            clearances = lambda raw: sum(row["type"] == "clearance" and row["severity"] == "error" for row in raw["violations"])
            gates = {"clearance_errors_zero": clearances(report) == 0,
                     "clearance_errors_reduced": clearances(report) < clearances(before_report),
                     "connections_not_increased": all(n <= baseline_connections.get(net, 0) for net, n in after_connections.items()),
                     "no_other_drc_errors": after["drc"]["errors"] == after["drc"]["unconnected"],
                     "erc_no_new_findings": not (_erc_violations(_erc_report(engine, project, child, after)) - before_erc),
                     "constraints_pass": after["constraints"]["status"] == "passed",
                     "widths_pass": after["persisted_track_minima"]["status"] == "passed",
                     "logical_connectivity_pass": after["connectivity"]["status"] == "passed"}
            record.update(status="improved" if all(gates.values()) else "blocked", candidate_revision=child,
                          after=after, gates=gates, before_connections=baseline_connections,
                          after_connections=after_connections)
        engine._verified(project, parent)
        record["parent_preserved"] = True
        write_json(work / "result.json", record)
    if "candidate_revision" in record:
        catalog.generate_report(engine, project, child)
    path = root / "docs" / "validation" / ("clearance-" + child + ".json")
    write_json(path, record)
    print(json.dumps({"status": record["status"], "candidate": record.get("candidate_revision"),
                      "gates": record.get("gates"), "evidence": str(path)}), flush=True)


if __name__ == "__main__":
    main()
