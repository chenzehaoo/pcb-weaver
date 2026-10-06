"""Headless engineering commands; machine-readable JSON on stdout."""
import argparse
import json
from pathlib import Path
import sys

from .runtime import load_runtime
from .service import EngineeringService
from .storage import read_json


def parser():
    p = argparse.ArgumentParser(prog="pcb-weaver")
    p.add_argument("--workspace", help="Managed revision and evidence directory")
    p.add_argument("--config", help="Toolchain JSON file")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    imp = sub.add_parser("import")
    imp.add_argument("project")
    imp.add_argument("board")
    imp.add_argument("--constraints")
    imp.add_argument("--parent")
    for cmd in ("inspect", "plan", "apply", "route", "verify", "release", "report", "constraints"):
        item = sub.add_parser(cmd)
        item.add_argument("project")
        item.add_argument("revision")
        if cmd == "plan":
            item.add_argument("--count", type=int, default=3)
        elif cmd == "apply":
            item.add_argument("plan_id")
            item.add_argument("candidate_id")
        elif cmd == "route":
            item.add_argument("--passes", type=int, default=10)
        elif cmd == "constraints":
            item.add_argument("file")
    diff = sub.add_parser("eco")
    diff.add_argument("project")
    diff.add_argument("before")
    diff.add_argument("after")
    for cmd in ("history", "revisions"):
        sub.add_parser(cmd).add_argument("project")
    sub.add_parser("verify-archive").add_argument("archive")
    demo = sub.add_parser("demo")
    demo.add_argument("--example", default=str(Path(__file__).resolve().parents[2] / "examples" / "two-layer"))
    demo.add_argument("--project", default="demo")
    demo.add_argument("--route", action="store_true")
    return p


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        root, config = load_runtime(args.workspace, args.config)
        engine = EngineeringService(root, config)
        cmd = args.command
        if cmd == "doctor":
            result = engine.doctor()
        elif cmd == "import":
            result = engine.import_project(args.project, args.board, read_json(Path(args.constraints)) if args.constraints else None, args.parent)
        elif cmd == "inspect":
            result = engine.inspect_revision(args.project, args.revision)
        elif cmd == "plan":
            result = engine.plan_layout(args.project, args.revision, args.count)
        elif cmd == "apply":
            result = engine.apply_layout(args.project, args.revision, args.plan_id, args.candidate_id)
        elif cmd == "route":
            result = engine.route_revision(args.project, args.revision, args.passes)
        elif cmd == "verify":
            result = engine.verify_revision(args.project, args.revision)
        elif cmd == "release":
            result = engine.build_release(args.project, args.revision)
        elif cmd == "constraints":
            result = engine.update_constraints(args.project, args.revision, read_json(Path(args.file)))
        elif cmd == "eco":
            result = engine.compare_revisions(args.project, args.before, args.after)
        elif cmd == "history":
            result = engine.store.history(args.project)
        elif cmd == "revisions":
            result = engine.store.list_revisions(args.project)
        elif cmd == "verify-archive":
            result = engine.verify_release(args.archive)
        elif cmd == "report":
            from .catalog import generate_report
            result = generate_report(engine, args.project, args.revision)
        else:
            from .demo import run_demo
            result = run_demo(engine, Path(args.example), args.project, args.route)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if isinstance(result, dict) and result.get("status") in {"failed", "blocked"}:
            return 2
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
