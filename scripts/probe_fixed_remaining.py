"""Source-only repair diagnostics on a retained revision, not a board benchmark."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import pcb_weaver
from pcb_weaver.models import AutoRepairOptions
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import canonical, digest, now, read_json, write_json


def package_hashes(package):
    return {p.relative_to(package).as_posix():digest(p)
            for p in sorted(package.rglob("*.py"))}


def object_hash(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def run(args):
    root = args.root.resolve(strict=True)
    package = root / "src" / "pcb_weaver"
    if Path(pcb_weaver.__file__).resolve().parent != package:
        raise ValueError("Loaded pcb_weaver package does not match --root/src/pcb_weaver")
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Refusing to overwrite probe output: " + str(output))
    output = output.resolve()
    config_path = (args.config or root / "toolchain.unified.json").resolve(strict=True)
    options_path = args.options.resolve(strict=True)
    config_sha, options_sha = digest(config_path), digest(options_path)
    workspace, config = load_runtime(args.workspace or root / "data",config_path)
    workspace = workspace.resolve(strict=True)
    options = AutoRepairOptions.model_validate(read_json(options_path))
    if digest(config_path) != config_sha or digest(options_path) != options_sha:
        raise ValueError("Config or options changed while loading")
    if output.is_relative_to(root / "src") or output.is_relative_to(workspace / "projects"):
        raise ValueError("Probe output must be outside package and project snapshots")
    implementation = package_hashes(package)
    script = Path(__file__).resolve()
    script_sha = digest(script)
    engine = EngineeringService(workspace,config)
    original, folder = engine._verified(args.project,args.revision)
    source = folder / "design" / original["board"]
    source_sha = digest(source)
    result = {"status":"running","created":now(),"project":args.project,
        "source_revision":args.revision,"revision":args.revision,
        "root":str(root),"workspace":str(workspace),"source_board":str(source),
        "source_board_sha256":source_sha,"source_digest":original["digest"],
        "source_files":original["files"],"source_constraints_sha256":original["constraints_hash"],
        "implementation_sha256":implementation,"package_sha256":object_hash(implementation),
        "script_sha256":script_sha,"config_path":str(config_path),"config_sha256":config_sha,
        "config":config,"effective_config_sha256":object_hash(config),
        "options_path":str(options_path),"options_file_sha256":options_sha,
        "options":options.model_dump(mode="json"),"options_sha256":object_hash(options.model_dump(mode="json")),
        "scope":"Retained-revision source-only repair diagnostic; not a whole-board-from-unrouted benchmark or manufacturing acceptance",
        "reference_used":False,"manufacturing_authorized":False,
        "final_native_status":"not_run",
        "budget_scope":"Auto-repair owns its scheduling/attempt limits; final independent verification is additional diagnostic work"}
    output.parent.mkdir(parents=True,exist_ok=True)
    # Reserve the new output atomically before any native operation.
    with output.open("x",encoding="utf-8") as stream:
        json.dump(result,stream,indent=2)
        stream.write("\n")
    started = time.monotonic()
    last_marker = None

    def save():
        result["elapsed_seconds"] = round(time.monotonic()-started,3)
        write_json(output,result)

    def progress(value):
        nonlocal last_marker
        result["repair"] = deepcopy(value)
        result["revision"] = value["revision"]
        attempts = value.get("attempts",[])
        latest = attempts[-1] if attempts else {}
        summary = {"stage":value.get("stage"),"status":value.get("status"),
            "revision":value["revision"],"attempts":len(attempts),
            "before_unconnected":value.get("before_unconnected"),
            "after_unconnected":value.get("after_unconnected"),
            "net":latest.get("net"),"attempt_status":latest.get("status")}
        marker = canonical(summary)
        if marker != last_marker:
            print(json.dumps({**summary,"elapsed_seconds":value.get("elapsed_seconds")}),flush=True)
            last_marker = marker
        save()

    try:
        repair = engine.auto_repair_revision(args.project,args.revision,options,progress=progress)
        result.update(repair=repair,revision=repair["revision"])
        save()
        engine._verified(args.project,result["revision"])
        print(json.dumps({"stage":"final_native_verification","revision":result["revision"]}),flush=True)
        final = engine.verify_revision(args.project,result["revision"])
        result["final_verification"] = final
        result["final_native_status"] = final["status"]
        result["status"] = "native_passed" if final["status"] == "passed" else "blocked"
    except KeyboardInterrupt:
        result.update(status="interrupted",reason="Interrupted; inspect retained revision and recorded evidence")
    except Exception as error:
        result.update(status="failed",reason=type(error).__name__+": "+str(error))
    finally:
        integrity = {}
        try:
            unchanged, _ = engine._verified(args.project,args.revision)
            integrity["source_revision_unchanged"] = unchanged == original and digest(source) == source_sha
            final_data, final_folder = engine._verified(args.project,result["revision"])
            result["final_revision_digest"] = final_data["digest"]
            result["final_board_sha256"] = digest(final_folder / "design" / final_data["board"])
        except Exception as error:
            integrity["source_revision_unchanged"] = False
            integrity["revision_error"] = type(error).__name__+": "+str(error)
        for name, measure, expected in (
            ("package_unchanged",lambda:package_hashes(package),implementation),
            ("config_unchanged",lambda:digest(config_path),config_sha),
            ("options_unchanged",lambda:digest(options_path),options_sha),
            ("script_unchanged",lambda:digest(script),script_sha),
        ):
            try:
                integrity[name] = measure() == expected
            except OSError as error:
                integrity[name] = False
                integrity[name+"_error"] = str(error)
        result["integrity"] = integrity
        if any(value is False for value in integrity.values()):
            result["status"] = "invalid_evidence"
        save()
        print(json.dumps({"status":result["status"],"revision":result["revision"],
            "final_native_status":result["final_native_status"],"output":str(output),
            "unconnected":result.get("final_verification",{}).get("drc",{}).get("unconnected")}),flush=True)
    return {"native_passed":0,"blocked":2,"interrupted":130}.get(result["status"],1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--workspace",type=Path)
    parser.add_argument("--config",type=Path)
    parser.add_argument("--project",required=True)
    parser.add_argument("--revision",required=True)
    parser.add_argument("--options",type=Path,required=True,help="AutoRepairOptions JSON file")
    parser.add_argument("--output",type=Path,required=True,help="New diagnostic JSON path")
    raise SystemExit(run(parser.parse_args()))
