"""Native scoped repair experiment; preserves donors and retains rejected candidates."""
import argparse
import json
from pathlib import Path
import shutil
import uuid

import sexpdata

from pcb_weaver import board as pcb
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, read_json, write_json


def run(args):
    root = Path(__file__).resolve().parents[1]
    workspace, config = load_runtime(root / "data", args.config or root / "toolchain.unified.json")
    engine = EngineeringService(workspace, config)
    identifier = args.project or "repair-acceptance-" + uuid.uuid4().hex[:8]
    evidence = {"status": "running", "scope": "Real native local copper repair, not industrial or manufacturing certification"}
    output = root / "docs/validation" / (identifier + "-" + uuid.uuid4().hex[:8] + ".json")
    try:
        if args.mode == "small":
            donor, folder = engine._verified(args.donor_project, args.donor_revision)
            evidence["donor_digest"] = donor["digest"]
            design = root / "validation" / identifier / "design"
            shutil.copytree(folder / "design", design)
            path = design / donor["board"]
            ast = sexpdata.loads(path.read_text(encoding="utf-8-sig"), nil=None, true=None, false=None)
            codes = {str(n[1]): str(n[2]) for n in pcb._children(ast, "net")}
            tracks = [t for t in pcb._children(ast, "segment") if codes.get(str(pcb._child(t,"net")[1])) == "VIN"]
            if len(tracks) < 2:
                raise ValueError("Expected native manufacturing-demo donor with at least two VIN segments")
            tracks.sort(key=lambda t: (pcb._xy(t,"start")[0]-pcb._xy(t,"end")[0])**2 +
                                      (pcb._xy(t,"start")[1]-pcb._xy(t,"end")[1])**2, reverse=True)
            removed = tracks[0]
            ast.remove(removed)
            path.write_text(sexpdata.dumps(ast) + "\n", encoding="utf-8")
            # The donor already contains compiled per-net rules. This explicit
            # test fixture keeps their exact bytes, without recompilation or
            # weakening the normal external-import gate.
            with engine.store.lock(identifier):
                revision, fixture_folder, _ = engine._new(identifier)
                shutil.copytree(design, fixture_folder / "design", dirs_exist_ok=True)
                shutil.copy2(folder / "constraints.json", fixture_folder / "constraints.json")
                engine._seal(identifier, revision, fixture_folder, donor["board"], None, "repair_fixture",
                             {"donor_digest": donor["digest"], "test_fixture": True})
            # A deliberate gap in the disposable fixture tests reconnection;
            # a second explicitly named segment tests scoped ripup as well.
            remove_ids = [str(pcb._child(tracks[1], "uuid")[1])]
            nets, region = ["VIN"], [22, 32, 34, 39]
            evidence.update(project=identifier, fixture_gap_uuid=str(pcb._child(removed,"uuid")[1]),
                            donor_project=args.donor_project, donor_revision=args.donor_revision,
                            explicit_ripup=remove_ids)
        else:
            revision = args.revision
            if not revision or not args.project or not args.net or not args.region:
                raise ValueError("System mode requires existing --project --revision --net --region")
            nets, region, remove_ids = args.net, args.region, args.remove_id
            evidence["project"] = identifier
            if args.clone_from_project:
                donor, donor_folder = engine._verified(args.clone_from_project, revision)
                with engine.store.lock(identifier):
                    revision, fixture_folder, _ = engine._new(identifier)
                    shutil.copytree(donor_folder / "design", fixture_folder / "design", dirs_exist_ok=True)
                    shutil.copy2(donor_folder / "constraints.json", fixture_folder / "constraints.json")
                    copied = engine._seal(identifier, revision, fixture_folder, donor["board"], None, "repair_benchmark_copy",
                                          {"donor_project":args.clone_from_project,"donor_revision":args.revision,
                                           "donor_digest":donor["digest"]})
                    assert copied["digest"] == donor["digest"]
                evidence.update(donor_project=args.clone_from_project, donor_revision=args.revision, donor_digest=donor["digest"])
        before, _ = engine._verified(identifier, revision)
        evidence.update(source_revision=revision, source_digest=before["digest"])
        print(json.dumps({"stage":"repair", "project":identifier,"revision":revision,"nets":nets,"region":region}), flush=True)
        result = engine.repair_revision(identifier, revision, nets, region, remove_ids, args.passes)
        evidence["result"] = result
        after, _ = engine._verified(identifier, revision)
        assert before == after, "Original sealed revision changed"
        evidence["parent_preserved"] = True
        if args.clone_from_project:
            assert engine._verified(args.clone_from_project, args.revision)[0] == donor
            evidence["donor_preserved"] = True
        if args.mode == "small":
            assert engine._verified(args.donor_project, args.donor_revision)[0] == donor
            assert result["status"] == "repaired", result.get("reason")
            assert result["comparison"]["after_unconnected"] == 0
        evidence["status"] = "passed" if result["status"] in {"improved", "repaired"} else "blocked"
    except BaseException as error:
        evidence.update(status="failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        write_json(output, evidence)
        print(json.dumps({"status":evidence["status"],"evidence":str(output)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["small", "system"], default="small")
    parser.add_argument("--project")
    parser.add_argument("--revision")
    parser.add_argument("--clone-from-project", help="Copy this donor's --revision unchanged into the new --project")
    parser.add_argument("--net", action="append")
    parser.add_argument("--region", nargs=4, type=float)
    parser.add_argument("--remove-id", action="append", default=[])
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--config")
    parser.add_argument("--donor-project", default="v3-manufacturing")
    parser.add_argument("--donor-revision", default="r-77e9f0682e3a488f")
    run(parser.parse_args())
