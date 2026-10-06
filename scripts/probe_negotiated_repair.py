"""Bounded geometric multi-net trials; proposed is never native acceptance."""
import argparse
from pathlib import Path
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver import catalog
from pcb_weaver.repair import _report
from pcb_weaver.ripup_planning import plans
from pcb_weaver.auto_repair import compact_via_policy,proposal
from pcb_weaver.negotiated_reroute import run
from pcb_weaver.storage import write_json


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--project",required=True)
    p.add_argument("--revision",required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--limit",type=int,default=6)
    p.add_argument("--net",help="Restrict a diagnostic run to one native missing net")
    p.add_argument("--extent",choices=("escape","routing_region"))
    p.add_argument("--preserve-cut-terminals",action="store_true")
    p.add_argument("--timeout",type=float,default=180)
    args = p.parse_args()
    workspace,config = load_runtime(args.root / "data",args.root / "toolchain.unified.json")
    engine = EngineeringService(workspace,config)
    data,folder = engine._verified(args.project,args.revision)
    source = folder / "design" / data["board"]
    check = catalog.verification(engine,args.project,args.revision)
    report = _report(engine,args.project,args.revision,check)
    native = engine.toolchain.inspect_board(source)
    assert native["status"] == "ok"
    candidates = (plan for plan in plans(source,report["unconnected_items"])
                  if (args.net is None or plan["target_net"] == args.net)
                  and (args.extent is None or plan["ripup_extent"] == args.extent))
    for i,plan in enumerate(candidates):
        if i >= args.limit:
            break
        routing = {}
        for net in plan["nets"]:
            rules = dict(native["nets"][net])
            rules["preferred_width"] = rules["track_width"]
            rules["track_width"] = max(check["persisted_track_minima"]["global_minimum_mm"],
                check["persisted_track_minima"]["per_net_minimum_mm"].get(net,0))
            compact = compact_via_policy(source,native,net,rules)
            if compact:
                rules["via_diameter"] = compact["diameter_mm"]
            routing[net] = rules
        request = {"source":str(source),"source_sha256":plan["source_sha256"],"net":plan["target_net"],
            "preserve_cut_terminals":args.preserve_cut_terminals,
            "routing_rules":routing,"ripup":plan,"finding":next(f for f in report["unconnected_items"]
                if any(item["uuid"] == plan["target_pad"] for item in f["items"]))}
        work = args.output / f"attempt-{i:02d}"
        work.mkdir(parents=True,exist_ok=False)
        write_json(work / "request.json",request)
        result = proposal(work,request,args.timeout,lambda:None,worker_module="pcb_weaver.negotiated_reroute")
        write_json(work / "proposal.json",result)
        print(i,plan["target_net"],result["status"],result.get("reason"),flush=True)
        if result["status"] == "proposed":
            break
