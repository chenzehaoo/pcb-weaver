"""Native automatic-repair regression on disposable copies of real donor boards."""
import argparse
import json
import math
from pathlib import Path
import shutil
from uuid import uuid4

from pcb_weaver import catalog
from pcb_weaver.auto_repair import repair
from pcb_weaver.jobs import JobQueue, JobRequest
from pcb_weaver.models import AutoRepairOptions
from pcb_weaver.repair_geometry import _read, _board, _serialize
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import write_json, read_json, digest


def run(args):
    donor_root, config = load_runtime(args.root / "data",args.root / "toolchain.unified.json")
    donor_engine = EngineeringService(donor_root,config)
    queue = JobQueue(args.output / "data",config)
    engine = queue.engine
    cases = {
        "two-layer-gap":("manufacturing-demo","r-7e370370c88841ef",["VIN"]),
        "two-layer-two-gaps":("v3-desktop-manufacturing","r-ab0413e5a612437d",["VIN","GND"]),
        "four-layer-gap":("system-clearance-acceptance","r-1bbf979cb3cc4206",["/AN5"]),
        "already-passed":("manufacturing-demo","r-7e370370c88841ef",[]),
        "critical-review":("manufacturing-demo","r-7e370370c88841ef",[]),
    }
    evidence = {"status":"running","scope":"Native regression on disposable fixtures; not industrial certification","cases":[]}
    args.output.mkdir(parents=True,exist_ok=True)
    try:
        for name in args.cases or cases:
            project, revision, gaps = cases[name]
            donor, folder = donor_engine._verified(project,revision)
            check = catalog.verification(donor_engine,project,revision)
            assert check["status"] == "passed", (name,check["status"])
            target = "auto-"+name+"-"+uuid4().hex[:6]
            with engine.store.lock(target):
                fixture, fixture_folder, _ = engine._new(target)
                shutil.copytree(folder / "design",fixture_folder / "design",dirs_exist_ok=True)
                shutil.copy2(folder / "constraints.json",fixture_folder / "constraints.json")
                source = fixture_folder / "design" / donor["board"]
                ast = _read(source)[0]
                removed = []
                copper = _board(ast,source=True)["copper"]
                for net in gaps:
                    choices = [c for c in copper if c["geometry"]["kind"] == "segment" and c["geometry"]["net"] == net
                               and 1 <= math.dist(c["geometry"]["start"],c["geometry"]["end"]) <= 12]
                    assert choices, (name,net,"No suitable deliberate fixture gap")
                    chosen = max(choices,key=lambda c:math.dist(c["geometry"]["start"],c["geometry"]["end"]))
                    ast.remove(chosen["node"])
                    removed.append(chosen["id"])
                source.write_bytes(_serialize(ast))
                if name == "critical-review":
                    intent = read_json(fixture_folder / "constraints.json")
                    intent["critical_nets"] = ["VIN"]
                    write_json(fixture_folder / "constraints.json",intent)
                engine._seal(target,fixture,fixture_folder,donor["board"],None,"auto_repair_fixture",
                             {"donor_project":project,"donor_revision":revision,"donor_digest":donor["digest"],
                              "fixture_removed_ids":removed,"test_fixture":True})
            original_sha = digest(source)
            print(json.dumps({"case":name,"project":target,"revision":fixture,"stage":"queued"}),flush=True)
            job = queue.submit(JobRequest(project=target,revision=fixture,operation="auto_repair",
                               auto_options=AutoRepairOptions(max_attempts=8,time_budget_seconds=900,proposal_timeout_seconds=120)))
            queue.run_once()
            job = queue.get(job["id"])
            result = job["result"]["steps"].get("auto_repair",{})
            final = result.get("revision",fixture)
            after = catalog.verification(engine,target,final)
            record = {"name":name,"project":target,"fixture_revision":fixture,"final_revision":final,
                      "job_id":job["id"],"job_status":job["status"],"result":result,
                      "native_status":after["status"],"fixture_preserved":digest(source)==original_sha,
                      "donor_preserved":donor_engine._verified(project,revision)[0]==donor,
                      "expectation":"blocked" if name == "critical-review" else "repaired"}
            evidence["cases"].append(record)
            write_json(args.output / "results.json",evidence)
            assert record["fixture_preserved"] and record["donor_preserved"]
            if name == "critical-review":
                assert job["status"] == result["status"] == "blocked"
                assert final == fixture and result["attempts"] == []
            else:
                assert job["status"] == "completed", result.get("reason",job["result"])
                assert result["status"] == "repaired" and after["status"] == "passed"
                assert after["drc"]["unconnected"] == after["drc"]["errors"] == after["erc"]["errors"] == 0
                if gaps:
                    assert result["before_unconnected"] > 0
                    assert final != fixture
            record["acceptance"] = "passed"
            print(json.dumps({"case":name,"acceptance":"passed","outcome":result["status"],"revision":final}),flush=True)
        evidence["status"] = "passed"
    finally:
        write_json(args.output / "results.json",evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--cases",nargs="+")
    run(parser.parse_args())
