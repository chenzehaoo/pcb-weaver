"""Native validation of a source-only autonomous probe; never force acceptance."""
import argparse
from pathlib import Path
import shutil
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver import catalog
from pcb_weaver.board import read_board
from pcb_weaver.repair import _report,_erc_report,_erc_violations,_net_index,compare_checks
from pcb_weaver.repair_geometry import prepare_repair,merge_repair
from pcb_weaver.storage import digest,read_json,write_json


def run(args):
    workspace,config = load_runtime(args.root / "data",args.root / "toolchain.unified.json")
    engine = EngineeringService(workspace,config)
    request = read_json(args.probe / "request.json")
    proposed = read_json(args.probe / "proposal.json")
    result = {"status":"running","reference_used":False,"manufacturing_authorized":False}
    try:
        assert proposed["status"] == "proposed" and not proposed["reference_used"]
        with engine.store.lock(args.project):
            data,root = engine._verified(args.project,args.revision)
            source = root / "design" / data["board"]
            assert digest(source) == request["source_sha256"]
            check = catalog.verification(engine,args.project,args.revision)
            before_report = _report(engine,args.project,args.revision,check)
            scope = request["ripup"]
            work = args.output.parent / (args.output.stem+"-patch")
            work.mkdir(parents=True)
            manifest = prepare_repair(source,work / "working.kicad_pcb",work / "empty.kicad_pcb",
                                      scope["nets"],scope["region"],scope["remove_ids"])
            write_json(work / "scope.json",manifest)
            candidate = catalog.artifact_path(args.probe,args.probe / proposed["output"])
            assert digest(candidate) == proposed["output_sha256"]
            merged = work / "merged.kicad_pcb"
            patch = merge_repair(source,candidate,merged,manifest)
            assert patch["dropped_outside_region"] == patch["dropped_unselected_nets"] == 0
            assert engine._electrical_signature(read_board(source)) == engine._electrical_signature(read_board(merged))
            child,folder,_ = engine._new(args.project,args.revision)
            shutil.copy2(merged,folder / "design" / data["board"])
            engine._seal(args.project,child,folder,data["board"],args.revision,"repair_candidate",
                         {"autonomous_probe_sha256":digest(args.probe / "proposal.json"),"scope_sha256":digest(work / "scope.json"),"reference_used":False})
            after = engine._verify(args.project,child)
            comparison = compare_checks(check,after,before_report,_report(engine,args.project,child,after),
                _net_index(source),_net_index(folder / "design" / data["board"]),scope["nets"])
            if _erc_violations(_erc_report(engine,args.project,child,after))-_erc_violations(_erc_report(engine,args.project,args.revision,check)):
                comparison["reasons"].append("New native ERC findings")
            comparison["accepted"] = not comparison["reasons"]
            assert digest(source) == request["source_sha256"]
            result.update(status="improved" if comparison["accepted"] else "rejected",project=args.project,
                source_revision=args.revision,revision=child,verification=after,comparison=comparison,patch=patch,
                source_sha256=digest(source),board_sha256=digest(folder / "design" / data["board"]))
    except BaseException as error:
        result.update(status="failed",error=type(error).__name__+": "+str(error))
        raise
    finally:
        write_json(args.output,result)
    print({k:result[k] for k in ("status","revision","comparison")})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root","probe","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--project",required=True)
    parser.add_argument("--revision",required=True)
    run(parser.parse_args())
