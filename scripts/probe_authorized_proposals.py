"""Replay immutable proposal requests in a fresh evidence directory, without acceptance."""
import argparse
from pathlib import Path
from pcb_weaver.auto_proposal import run
from pcb_weaver.storage import read_json, write_json, digest
from pcb_weaver.auto_repair import compact_via_policy


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--attempts", nargs="+", type=int, default=[7, 8, 9])
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    for number in args.attempts:
        request = read_json(args.source / f"attempt-{number:02d}" / "request.json")
        if args.compact:
            source = Path(request["source"])
            request["compact_via_policy"] = compact_via_policy(source,
                {"project_sha256": digest(source.with_suffix(".kicad_pro"))}, request["net"], request["rules"])
        folder = args.output / f"attempt-{number:02d}"
        folder.mkdir(parents=True, exist_ok=False)
        write_json(folder / "request.json", request)
        result = run(request, folder)
        write_json(folder / "proposal.json", result)
        print(number, request["net"], result["status"], result.get("reason"), flush=True)
