"""Derive a fully unrouted regression input from an immutable reviewed placement."""
import argparse
from pathlib import Path
import shutil
from pcb_weaver.board import read_board
from pcb_weaver.repair_geometry import _read, _serialize
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest,read_json,write_json


def run(args):
    workspace,config = load_runtime(args.root / "data",args.root / "toolchain.unified.json")
    engine = EngineeringService(workspace,config)
    data,folder = engine._verified(args.project,args.revision)
    if args.output.exists():
        raise ValueError("Use a new benchmark directory")
    shutil.copytree(folder / "design",args.output)
    board = args.output / data["board"]
    before = read_board(board)
    ast = _read(board)[0]
    removed = [x for x in ast if isinstance(x,list) and x and str(x[0]) in {"segment","via","arc","zone","group"}]
    for item in removed:
        ast.remove(item)
    board.write_bytes(_serialize(ast))
    after = read_board(board)
    assert before["footprints"] == after["footprints"] and before["nets"] == after["nets"]
    assert after["tracks"] == 0 and after["vias"] == 0 and not after["unsupported"]
    intent = read_json(folder / "constraints.json")
    intent["placement"]["algorithm"] = "legalize"
    write_json(args.output / "constraints.json",intent)
    provenance = read_json(args.output / "PROVENANCE.json") if (args.output / "PROVENANCE.json").exists() else {}
    provenance["completion_derivative"] = {"source_project":args.project,"source_revision":args.revision,
        "source_board_sha256":digest(folder / "design" / data["board"]),"input_board_sha256":digest(board),
        "removed_copper_records":len(removed),"notice":"All routing removed, not isolated gaps. Reviewed placement is the seed; this does not test placement from an arbitrary pile of components.",
        "preserved":"Every component, pad, network, side, rotation, placement, board outline and electrical minimum"}
    write_json(args.output / "PROVENANCE.json",provenance)
    engine._verified(args.project,args.revision)
    print(board)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("root","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    for name in ("project","revision"):
        parser.add_argument("--"+name,required=True)
    run(parser.parse_args())
