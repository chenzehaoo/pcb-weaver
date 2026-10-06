"""Probe additive completion repair after native clearance cleanup."""
import argparse
from pathlib import Path
from pcb_weaver import catalog
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import read_json,write_json


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("original","probe","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    args = parser.parse_args()
    original,probe = read_json(args.original),read_json(args.probe)
    engine = EngineeringService(args.original.parent / "data",original["config"])
    result = engine.auto_repair_revision(probe["project"],probe["revision"],
        {"max_attempts":24,"time_budget_seconds":1800,"proposal_timeout_seconds":120},
        progress=lambda value:write_json(args.output,value))
    result["verification"] = catalog.verification(engine,probe["project"],result["revision"])
    write_json(args.output,result)
    print({"status":result["status"],"revision":result["revision"],"unconnected":result["verification"]["drc"]["unconnected"]},flush=True)
