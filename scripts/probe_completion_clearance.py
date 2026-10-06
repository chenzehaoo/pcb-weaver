"""Diagnostic automatic scopes only; does not mark a whole-board workflow passed."""
import argparse
from pcb_weaver import catalog
from pcb_weaver.completion_cleanup import clearance_scopes
from pcb_weaver.clearance_repair import eligible
from pcb_weaver.repair import _report
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import read_json,write_json
from pathlib import Path


def run(args):
    original = read_json(args.original)
    probe = read_json(args.probe)
    engine = EngineeringService(args.original.parent / "data",original["config"])
    project,revision = probe["project"],probe["revision"]
    result = {"status":"running","source_revision":revision,"project":project,"attempts":[]}
    for margin in (.8,1.6):
        for _ in range(3):
            check = catalog.verification(engine,project,revision)
            report = _report(engine,project,revision,check)
            if eligible(check,report):
                break
            data,folder = engine._verified(project,revision)
            scopes = clearance_scopes(folder / "design" / data["board"],report,margin)
            improved = False
            for scope in scopes:
                repair = engine.repair_clearance(project,revision,scope["nets"],scope["region"])
                result["attempts"].append({"scope":scope,"repair":repair})
                if repair["status"] == "improved":
                    revision = repair["revision"]
                    improved = True
                    break
            result["revision"] = revision
            write_json(args.output,result)
            if not improved:
                break
    check = catalog.verification(engine,project,revision)
    result["verification"] = check
    result["status"] = check["status"]
    write_json(args.output,result)
    print({"revision":revision,"errors":check["drc"]["errors"],"unconnected":check["drc"]["unconnected"]})


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("original","probe","output"):
        p.add_argument("--"+name,type=Path,required=True)
    run(p.parse_args())
