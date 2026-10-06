"""Native four-layer routing smoke test of the explicit no-neckdown profile."""
import argparse
import json
from pathlib import Path
import uuid

from pcb_weaver.compiler import compile_rules
from pcb_weaver.runtime import load_runtime
from pcb_weaver.storage import write_json
from pcb_weaver.toolchain import Toolchain


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    _, config = load_runtime(args.root / "data", args.root / "toolchain.hard-width.json")
    tc = Toolchain(config)
    folder = args.root / "docs/validation" / ("hard-width-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    result = {"status": "running", "config": config, "scope": "four-layer synthetic POWER/LINK board",
              "manufacturing_authorized": False}
    try:
        board, routed = folder / "board.kicad_pcb", folder / "routed.kicad_pcb"
        result["sample"] = tc._native("sample-platform-4", board)
        assert result["sample"]["status"] == "ok"
        result["compiled"] = compile_rules(board, {"fabrication": {"min_track_mm": .25,
            "min_clearance_mm": .2, "min_via_drill_mm": .3}, "net_rules": [{"nets": ["POWER"], "min_width_mm": .6}]},
            native_state=tc.inspect_board(board))
        dsn, ses = folder / "board.dsn", folder / "board.ses"
        result["export"] = tc.export_dsn(board, dsn)
        assert result["export"]["status"] == "ok"
        result["route"] = tc.route(dsn, ses, passes=5)
        assert result["route"]["status"] == "ok" and result["route"]["automatic_neckdown"] is False
        result["import"] = tc.import_ses(board, ses, routed)
        assert result["import"]["status"] == "ok" and result["import"]["track_width_audit"]["verified"]
        result["drc"] = tc.run_drc(routed, folder / "drc.json")
        assert result["drc"]["status"] == "ok" and result["drc"]["errors"] == result["drc"]["unconnected"] == 0
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        write_json(folder / "result.json", result)
        print(json.dumps({"status": result["status"], "evidence": str(folder / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
