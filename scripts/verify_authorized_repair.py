"""Fresh native acceptance and preservation checks for an authorized repair chain."""
import argparse
from pathlib import Path
from pcb_weaver import catalog
from pcb_weaver.board import read_board
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import read_json,write_json,digest
from pcb_weaver.repair_geometry import _read,_board


def run(args):
    workspace,config = load_runtime(args.root / "data",args.root / "toolchain.unified.json")
    engine = EngineeringService(workspace,config)
    records = [read_json(path) for path in args.runs]
    project,original = records[0]["project"],records[0]["source_revision"]
    result = {"status":"running","project":project,"source_revision":original,"runs":[],"manufacturing_authorized":False}
    try:
        parent_data,parent = engine._verified(project,original)
        source = parent / "design" / parent_data["board"]
        source_sha = digest(source)
        initial_board = read_board(source)
        previous = original
        for path,record in zip(args.runs,records):
            assert record["project"] == project and record["source_revision"] == previous
            flow = record["job"]["result"]["steps"]["auto_repair"]
            assert flow["options"]["allow_neckdown"] and flow["options"]["allow_local_adjustment"]
            assert not flow["manufacturing_authorized"]
            for attempt in flow["attempts"]:
                if attempt["status"] != "accepted":
                    continue
                assert attempt["comparison"]["accepted"] and not attempt["comparison"]["reasons"]
                neck = attempt["proposal"].get("width_restoration")
                if neck:
                    assert neck["escape_length_mm"] <= 4 and neck["requires_native_verification"]
                adjustment = attempt["proposal"].get("adjustment")
                if adjustment:
                    assert adjustment["requires_native_verification"]
                    assert adjustment["max_axis_move_mm"] <= .35
                    assert all(abs(a-b) <= .350001
                        for before,after in zip(adjustment["vertices_before"],adjustment["vertices_after"])
                        for a,b in zip(before,after))
            previous = flow["revision"]
            result["runs"].append({"file":str(path),"sha256":digest(path),"job":record["job_id"],
                "before":flow["before_unconnected"],"after":flow["after_unconnected"],
                "attempts":len(flow["attempts"]),"revision":previous})
        expected_missing = result["runs"][-1]["after"]
        assert records[-1]["status"] == "passed" or args.allow_incomplete
        data,folder = engine._verified(project,previous)
        board = folder / "design" / data["board"]
        assert engine._electrical_signature(initial_board) == engine._electrical_signature(read_board(board))
        assert data["constraints_hash"] == parent_data["constraints_hash"]
        assert read_json(board.with_suffix(".kicad_pro")) == read_json(source.with_suffix(".kicad_pro"))
        original_vias = {c["id"]:c["geometry"] for c in _board(_read(source)[0],source=True)["copper"]
                         if c["geometry"]["kind"] == "via"}
        final_vias = {c["id"]:c["geometry"] for c in _board(_read(board)[0],source=True)["copper"]
                      if c["geometry"]["kind"] == "via"}
        assert all(identity in final_vias and all(via[k] == final_vias[identity][k]
            for k in ("size","drill","net","layers")) for identity,via in original_vias.items())
        check = engine.verify_revision(project,previous)
        result.update(revision=previous,verification=check,board_sha256=digest(board),source_sha256=source_sha)
        assert (check["drc"]["unconnected"],check["drc"]["errors"],check["erc"]["errors"]) == (expected_missing,expected_missing,0)
        assert (check["status"] == "passed") == (expected_missing == 0)
        assert (check["drc"]["warnings"],check["erc"]["warnings"]) == (53,16)
        assert digest(source) == source_sha
        old,old_folder = engine._verified("system-clearance-acceptance","r-1bbf979cb3cc4206")
        assert digest(old_folder / "design" / old["board"]) == "3f871730ae06e4c6c401555d0be4cb85effb43f7106385bb94387205c14584c5"
        result["report"] = catalog.generate_report(engine,project,previous)
        result["status"] = "passed" if expected_missing == 0 else "blocked"
        result["remaining_unconnected"] = expected_missing
        result["preservation_checks_passed"] = True
    except BaseException as error:
        result.update(status="failed",error=type(error).__name__+": "+str(error))
        raise
    finally:
        write_json(args.output,result)
    print({"status":result["status"],"revision":previous,"unconnected":expected_missing})


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--runs",type=Path,nargs="+",required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--allow-incomplete",action="store_true",help="Report incomplete native results as blocked, never passed")
    run(p.parse_args())
