"""Execute one scoped, native-verified clearance repair from the local command line."""
import argparse
import json
from pathlib import Path

from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--net", action="append", required=True)
    parser.add_argument("--region", nargs=4, type=float, required=True)
    args = parser.parse_args()
    root, config = load_runtime(args.root / "data", args.root / "toolchain.unified.json")
    engine = EngineeringService(root, config)
    result = engine.repair_clearance(args.project, args.revision, args.net, args.region)
    print(json.dumps({k: result.get(k) for k in ("status", "revision", "candidate_revision", "attempt_id", "reason", "comparison")}), flush=True)
    if result["status"] != "improved":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
