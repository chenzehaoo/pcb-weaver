"""Run an actual unrouted board through the persisted completion workflow."""
import argparse
from pathlib import Path
from uuid import uuid4

from pcb_weaver import catalog
from pcb_weaver.board import read_board
from pcb_weaver.jobs import JobQueue, JobRequest
from pcb_weaver.runtime import load_runtime
from pcb_weaver.storage import read_json, write_json, digest


def run(args):
    args.output.mkdir(parents=True,exist_ok=True)
    _, config = load_runtime(args.root / "data",args.root / "toolchain.unified.json")
    config["route_timeout_seconds"] = args.route_timeout
    if args.optimizer_passes is not None:
        config["router_optimizer_max_passes"] = args.optimizer_passes
    queue = JobQueue(args.output / "data",config)
    engine = queue.engine
    board = args.board.resolve(strict=True)
    model = read_board(board)
    assert model["tracks"] == 0 and model["vias"] == 0
    project = args.name+"-"+uuid4().hex[:6]
    source_hash = digest(board)
    evidence = {"status":"running","project":project,"input_sha256":source_hash,
                "source_board":str(board),"config":config,"manual_routing_interventions":0}
    try:
        imported = engine.import_project(project,str(board),read_json(board.parent / "constraints.json"))
        revision = imported["revision"]["id"]
        evidence["input_revision"] = revision
        job = queue.submit(JobRequest(operation="complete",project=project,revision=revision,
            completion_options={"candidate_count":args.candidates,"route_passes":args.passes,"time_budget_seconds":args.budget,
                "routing_policy":args.routing_policy,"repair":{"max_attempts":args.repair_attempts,"time_budget_seconds":args.repair_budget,
                    "allow_neckdown":args.allow_neckdown,"allow_local_adjustment":args.allow_local_adjustment}}))
        evidence["job_id"] = job["id"]
        write_json(args.output / "result.json",evidence)
        print(f"Submitted {project} {job['id']}",flush=True)
        assert queue.run_once()
        job = queue.get(job["id"])
        evidence["job"] = job
        result = job["result"]["steps"].get("complete",{})
        evidence["result"] = result
        if result.get("best"):
            evidence["verification"] = catalog.verification(engine,project,result["revision"])
        assert digest(board) == source_hash
        engine._verified(project,revision)
        assert job["status"] == "completed", result.get("reason",job)
        assert evidence["verification"]["status"] == "passed"
        assert result["baseline"]["tracks"] == 0 and result["baseline"]["vias"] == 0
        assert result["best"]["tracks"] > 0 and result["best"]["unconnected"] == 0
        evidence["status"] = "passed"
    except BaseException as error:
        evidence.update(status="failed",error=type(error).__name__+": "+str(error))
        raise
    finally:
        write_json(args.output / "result.json",evidence)
        print(f"{project}: {evidence['status']}; evidence: {args.output / 'result.json'}",flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root","board","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--name",required=True)
    parser.add_argument("--candidates",type=int,default=3)
    parser.add_argument("--passes",type=int,default=20)
    parser.add_argument("--budget",type=int,default=1800)
    parser.add_argument("--route-timeout",type=int,default=300)
    parser.add_argument("--optimizer-passes",type=int)
    parser.add_argument("--routing-policy",choices=("strict","normalize_widths"),default="strict")
    parser.add_argument("--repair-attempts",type=int,default=6)
    parser.add_argument("--repair-budget",type=int,default=600)
    parser.add_argument("--allow-neckdown",action="store_true")
    parser.add_argument("--allow-local-adjustment",action="store_true")
    run(parser.parse_args())
