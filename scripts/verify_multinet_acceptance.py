"""Independently verify a reference-guided repair chain and fresh native checks."""
import argparse
from pathlib import Path

from pcb_weaver import catalog
from pcb_weaver.board import read_board, _tag
from pcb_weaver.models import Constraints
from pcb_weaver.repair_geometry import _read, _board, _inside
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import canonical, digest, read_json, write_json


def run(args):
    workspace, config = load_runtime(args.root / "data", args.root / "toolchain.unified.json")
    engine = EngineeringService(workspace, config)
    record = read_json(args.run)
    result = {"status": "running", "manufacturing_authorized": False, "patches": []}
    try:
        assert record["status"] == "passed" and record["submitter_disconnected"]
        from pcb_weaver.jobs import JobQueue
        job = JobQueue(workspace, engine=engine).get(record["job_id"])
        assert job["status"] == "completed" and job["result"] == record["job"]["result"]
        assert record["job"]["status"] == "completed"
        flow = record["job"]["result"]["steps"]["reference_repair"]
        assert flow["status"] == "repaired" and flow["after_unconnected"] == 0
        assert not flow["manufacturing_authorized"] and not record["manufacturing_authorized"]
        project, original = record["project"], record["source_revision"]
        refdata, refroot = engine._verified(record["reference_project"], record["reference_revision"])
        reference = refroot / "design" / refdata["board"]
        reference_sha = digest(reference)
        initial, initial_root = engine._verified(project, original)
        source = initial_root / "design" / initial["board"]
        source_sha = digest(source)
        assert initial["digest"] == flow["source_digest"] and refdata["digest"] == flow["reference_digest"]
        evidence = [p for p in (initial_root / "repairs").glob("reference-*/result.json")
                    if read_json(p) == flow]
        assert len(evidence) == 1, "Persistent repair evidence differs from MCP result"
        previous, missing = original, flow["before_unconnected"]
        removed_initial = set()
        for index, attempt in enumerate(flow["attempts"], 1):
            if attempt["status"] != "accepted":
                continue
            assert attempt["parent_revision"] == previous
            plan, proposal, comparison = attempt["plan"], attempt["proposal"], attempt["comparison"]
            assert comparison["accepted"] and not comparison["reasons"]
            assert comparison["before_unconnected"] == missing > comparison["after_unconnected"]
            assert proposal["requires_native_verification"] and not proposal["manufacturing_authorized"]
            work = evidence[0].parent / f"attempt-{index:02d}"
            assert digest(work / "scope.json") == proposal["scope_sha256"]
            manifest = read_json(work / "scope.json")
            assert all(manifest[k] == plan[k] for k in ("nets", "region", "remove_ids", "source_sha256"))
            before_data, before_root = engine._verified(project, previous)
            after_data, after_root = engine._verified(project, attempt["candidate_revision"])
            before = before_root / "design" / before_data["board"]
            after = after_root / "design" / after_data["board"]
            assert digest(before) == plan["source_sha256"] and reference_sha == plan["reference_sha256"]
            assert digest(after) == proposal["output_sha256"] == digest(work / proposal["output"])
            trees = [_read(p)[0] for p in (before, after)]
            boards = [_board(t, source=True) for t in trees]
            old, new = [{c["id"]: c for c in b["copper"]} for b in boards]
            removed = set(plan["remove_ids"])
            assert set(old)-set(new) == removed
            assert all(new[k]["node"] == c["node"] for k, c in old.items() if k not in removed)
            assert [n for n in trees[0] if _tag(n) not in {"segment", "via", "arc"}] == [
                n for n in trees[1] if _tag(n) not in {"segment", "via", "arc"}]
            additions = [c for k, c in new.items() if k not in old]
            reference_board = _board(_read(reference)[0], source=True)
            expected = {canonical(c["geometry"]) for c in reference_board["copper"]
                        if c["id"] in plan["reference_ids"]}
            assert {canonical(c["geometry"]) for c in additions} == expected
            changes = [old[k] for k in removed] + additions
            assert all(c["geometry"]["net"] in plan["nets"] and _inside(c["geometry"]["bounds"], plan["region"])
                       for c in changes)
            x0, y0, x1, y1 = plan["region"]
            area = (x1-x0)*(y1-y0)
            assert 0 < area <= 2500 and len(plan["nets"]) <= 8 and len(removed) <= 1000
            assert proposal["patch"]["dropped_outside_region"] == proposal["patch"]["dropped_unselected_nets"] == 0
            previous, missing = attempt["candidate_revision"], comparison["after_unconnected"]
            removed_initial.update(removed)
            result["patches"].append({"revision": previous, "nets": plan["nets"], "area_mm2": area,
                "removed": len(removed), "added": len(additions), "after_unconnected": missing,
                "retained_copper_ast_preserved": True, "noncopper_ast_preserved": True})
        assert previous == flow["revision"] and missing == 0
        final_data, final_root = engine._verified(project, previous)
        final_board = final_root / "design" / final_data["board"]
        assert engine._electrical_signature(read_board(source)) == engine._electrical_signature(read_board(final_board))
        assert Constraints.model_validate(read_json(initial_root / "constraints.json")) == Constraints.model_validate(
            read_json(final_root / "constraints.json"))
        for suffix in (".kicad_pro", ".kicad_dru"):
            a, b = source.with_suffix(suffix), final_board.with_suffix(suffix)
            assert a.exists() == b.exists()
            if a.exists():
                assert (read_json(a) == read_json(b)) if suffix == ".kicad_pro" else (digest(a) == digest(b))
        check = engine.verify_revision(project, previous)
        assert check["status"] == "passed"
        assert (check["drc"]["errors"], check["drc"]["unconnected"], check["erc"]["errors"]) == (0, 0, 0)
        assert (check["drc"]["warnings"], check["erc"]["warnings"]) == (53, 16)
        assert digest(source) == source_sha and digest(reference) == reference_sha
        assert digest(final_board) != reference_sha, "Whole-board reference replacement is not local repair"
        result.update(status="passed", project=project, source_revision=original, revision=previous,
            source_sha256=source_sha, reference_sha256=reference_sha, board_sha256=digest(final_board),
            verification=check, report=catalog.generate_report(engine, project, previous),
            evidence_sha256=digest(args.run), preservation_checks_passed=True,
            changed_original_copper=len(removed_initial))
    except BaseException as error:
        result.update(status="failed", error=type(error).__name__+": "+str(error))
        raise
    finally:
        write_json(args.output, result)
    print({"status": result["status"], "revision": previous, "patches": len(result["patches"])})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
