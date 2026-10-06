"""Exercise automatic rip-up scopes through the existing native adoption gate."""
import argparse
from pathlib import Path
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver import catalog
from pcb_weaver.repair import _report
from pcb_weaver.ripup_planning import plans
from pcb_weaver.storage import write_json


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--project",required=True)
    parser.add_argument("--revision",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--max-attempts",type=int,default=3)
    parser.add_argument("--plan-only",action="store_true")
    args = parser.parse_args()
    workspace, config = load_runtime(args.root / "data",args.root / "toolchain.unified.json")
    engine = EngineeringService(workspace,config)
    data,folder = engine._verified(args.project,args.revision)
    check = catalog.verification(engine,args.project,args.revision)
    report = _report(engine,args.project,args.revision,check)
    candidates = list(plans(folder / "design" / data["board"],report["unconnected_items"]))
    result = {"status":"planned","project":args.project,"source_revision":args.revision,
              "plans":candidates,"attempts":[],"manufacturing_authorized":False}
    write_json(args.output,result)
    print([(p["target_net"],len(p["nets"]),len(p["remove_ids"]),p["region"]) for p in candidates],flush=True)
    if not args.plan_only:
        for p in candidates[:args.max_attempts]:
            attempt = engine.repair_revision(args.project,args.revision,
                p["nets"],p["region"],p["remove_ids"],p["passes"])
            result["attempts"].append(attempt)
            result["status"] = attempt["status"]
            write_json(args.output,result)
            print(attempt["status"],attempt.get("reason"),attempt.get("comparison"),flush=True)
            if attempt["status"] in {"improved","repaired"}:
                break
