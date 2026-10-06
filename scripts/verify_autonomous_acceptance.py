"""Independent source-only repair-chain audit and fresh native acceptance."""
import argparse
from pathlib import Path
from pcb_weaver import catalog
from pcb_weaver.board import read_board,_tag
from pcb_weaver.jobs import JobQueue
from pcb_weaver.models import Constraints
from pcb_weaver.repair_geometry import _read,_board,_inside
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest,read_json,write_json


def run(args):
    workspace,config = load_runtime(args.root / "data",args.root / "toolchain.unified.json")
    engine = EngineeringService(workspace,config)
    evidence = read_json(args.run)
    result = {"status":"running","reference_used":False,"manufacturing_authorized":False,"patches":[]}
    implementation = {path.name:digest(path) for path in sorted((args.root / "src" / "pcb_weaver").glob("*.py"))}
    try:
        assert evidence["status"] == "passed" and evidence["submitter_disconnected"]
        assert evidence["reference_used"] is False
        job = JobQueue(workspace,engine=engine).get(evidence["job_id"])
        assert job["status"] == "completed" and job["result"] == evidence["job"]["result"]
        assert job["request"]["operation"] == "auto_repair" and job["request"]["auto_options"]["allow_multinet"]
        assert not job["request"].get("reference_project") and not job["request"].get("release")
        flow = job["result"]["steps"]["auto_repair"]
        assert flow["status"] == "repaired" and flow["before_unconnected"] == 3 and flow["after_unconnected"] == 0
        project,original = evidence["project"],evidence["source_revision"]
        initial,root = engine._verified(project,original)
        source = root / "design" / initial["board"]
        assert digest(source) == "0092bf967ca1331ca500c3a2a9caf4a5ee02a7694a7051e4a1275fec3b291ecf"
        workroot = root / "repairs" / flow["attempt_id"]
        assert read_json(workroot / "result.json") == flow
        previous,missing = original,3
        for index,attempt in enumerate(flow["attempts"],1):
            assert attempt["strategy"] == "multinet"
            if attempt["status"] != "accepted":
                continue
            assert attempt["parent_revision"] == previous
            work = workroot / f"attempt-{index:02d}"
            request = read_json(work / "request.json")
            proposal = read_json(work / "proposal.json")
            assert proposal == attempt["proposal"] and proposal["reference_used"] is False
            assert proposal["implementation_sha256"] == implementation
            assert proposal["original_partitions_preserved"] and not proposal["manufacturing_authorized"]
            before_data,before_root = engine._verified(project,previous)
            after_data,after_root = engine._verified(project,attempt["candidate_revision"])
            before = before_root / "design" / before_data["board"]
            after = after_root / "design" / after_data["board"]
            assert digest(before) == request["source_sha256"]
            assert digest(after) == digest(work / "merged.kicad_pcb")
            assert digest(work / proposal["output"]) == proposal["output_sha256"]
            audit = proposal["board_access_audit"]
            assert Path(audit["source"]) == before.resolve() and Path(audit["working_root"]) == work.resolve()
            assert str(before.resolve()) in audit["opened"]
            assert all(Path(p) == before.resolve() or Path(p).is_relative_to(work.resolve()) for p in audit["opened"])
            scope = read_json(work / "scope.json")
            assert after_data["scope_sha256"] == digest(work / "scope.json")
            assert after_data["strategy"] == "multinet" and after_data["reference_used"] is False
            removed = set(request["ripup"]["remove_ids"])
            assert removed == set(scope["remove_ids"]) == set(proposal["adjustment"]["changed_ids"])
            assert set(scope["nets"]) == set(request["ripup"]["nets"])
            trees = [_read(p)[0] for p in (before,after)]
            boards = [_board(t,source=True) for t in trees]
            old,new = [{c["id"]:c for c in b["copper"]} for b in boards]
            assert set(old)-set(new) == removed
            assert all(new[k]["node"] == c["node"] for k,c in old.items() if k not in removed)
            assert [n for n in trees[0] if _tag(n) not in {"segment","via","arc"}] == [
                n for n in trees[1] if _tag(n) not in {"segment","via","arc"}]
            additions = [c for k,c in new.items() if k not in old]
            assert all(_inside(c["geometry"]["bounds"],scope["region"]) and c["geometry"]["net"] in scope["nets"]
                       for c in additions+[old[k] for k in removed])
            x0,y0,x1,y1 = scope["region"]
            assert (x1-x0)*(y1-y0) <= 2500 and len(scope["nets"]) <= 8 and len(removed) <= 64
            comparison = attempt["comparison"]
            assert comparison["accepted"] and not comparison["reasons"]
            assert comparison["before_unconnected"] == missing > comparison["after_unconnected"]
            previous,missing = attempt["candidate_revision"],comparison["after_unconnected"]
            result["patches"].append({"revision":previous,"nets":scope["nets"],"removed":len(removed),
                "added":len(additions),"missing":missing,"source_only_access_verified":True,
                "retained_copper_ast_preserved":True,"noncopper_ast_preserved":True})
        assert previous == flow["revision"] and missing == 0
        final,final_root = engine._verified(project,previous)
        board = final_root / "design" / final["board"]
        assert engine._electrical_signature(read_board(source)) == engine._electrical_signature(read_board(board))
        assert Constraints.model_validate(read_json(root / "constraints.json")) == Constraints.model_validate(read_json(final_root / "constraints.json"))
        for suffix in (".kicad_pro",".kicad_dru"):
            a,b = source.with_suffix(suffix),board.with_suffix(suffix)
            assert a.exists() == b.exists()
            if a.exists():
                assert (read_json(a)==read_json(b)) if suffix==".kicad_pro" else (digest(a)==digest(b))
        check = engine.verify_revision(project,previous)
        assert check["status"] == "passed"
        assert (check["drc"]["errors"],check["drc"]["unconnected"],check["erc"]["errors"]) == (0,0,0)
        assert (check["drc"]["warnings"],check["erc"]["warnings"]) == (53,16)
        engine._verified(project,original)
        result.update(status="passed",project=project,source_revision=original,revision=previous,
            implementation_sha256=implementation,
            board_sha256=digest(board),source_sha256=digest(source),verification=check,
            report=catalog.generate_report(engine,project,previous),mcp_evidence_sha256=digest(args.run))
    except BaseException as error:
        result.update(status="failed",error=type(error).__name__+": "+str(error))
        raise
    finally:
        write_json(args.output,result)
    print({"status":result["status"],"revision":previous,"patches":len(result["patches"])})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root","run","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    run(parser.parse_args())
