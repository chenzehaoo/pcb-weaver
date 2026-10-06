"""Diagnose a completed rejected SES; never count this probe as completion acceptance."""
import argparse
from pathlib import Path
import shutil
from pcb_weaver.board import read_board
from pcb_weaver.service import EngineeringService
from pcb_weaver.ses_widths import raise_minima
from pcb_weaver.storage import read_json,write_json,digest


def run(args):
    evidence = read_json(args.result)
    engine = EngineeringService(args.result.parent / "data",evidence["config"])
    project = evidence["project"]
    attempt = evidence["result"]["attempts"][0]
    operations = attempt["routing"]["details"]
    route = operations[1]
    ses = Path(route["output_path"])
    dsn = Path(operations[0]["output_path"])
    assert route["status"] == "ok" and route["effective_settings_verified"]
    assert route["ses_sha256"] == digest(ses) and route["dsn_sha256"] == digest(dsn)
    assert route["router_result"]["final_state"] == "COMPLETED"
    assert digest(Path(route["router_result_path"])) == route["router_result_sha256"]
    parent = attempt["placement_revision"]
    result = {"scope":"Diagnostic candidate, not completion acceptance","project":project,"parent":parent}
    with engine.store.lock(project):
        old,parent_folder = engine._verified(project,parent)
        assert digest(dsn.parent.parent / "design" / old["board"]) == digest(parent_folder / "design" / old["board"])
        assert digest(dsn.parent.parent / "constraints.json") == digest(parent_folder / "constraints.json")
        child,folder,old = engine._new(project,parent)
        board_path = folder / "design" / old["board"]
        board = read_board(board_path)
        assert board["tracks"] == 0 and board["vias"] == 0
        rules = read_json(board_path.with_suffix(".kicad_pro"))
        floor = rules["board"]["design_settings"]["rules"]["min_track_width"]
        per_net = {net:row.get("declared_min_width_mm",row.get("minimums",{}).get("track_width"))
                   for net,row in rules.get("pcb_weaver_net_rules",{}).items()}
        output = folder / "width-probe"
        output.mkdir()
        adjusted = output / "minimum-width.ses"
        result["width_audit"] = raise_minima(ses,adjusted,floor,per_net,{n["name"] for n in board["nets"]})
        result["import"] = engine.toolchain.import_ses(board_path,adjusted,output / old["board"])
        assert result["import"]["status"] == "ok",result["import"].get("reason")
        assert engine._electrical_signature(board) == engine._electrical_signature(read_board(output / old["board"]))
        shutil.copy2(output / old["board"],board_path)
        revision = engine._seal(project,child,folder,old["board"],parent,"repair_candidate",{"source_ses_sha256":digest(ses)})
    result["revision"] = revision["id"]
    result["verification"] = engine.verify_revision(project,revision["id"])
    engine._verified(project,parent)
    write_json(args.output,result)
    check = result["verification"]
    print({"revision":revision["id"],"status":check["status"],"drc":{k:check["drc"].get(k) for k in ("errors","warnings","unconnected")}},flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    run(parser.parse_args())
