"""Exercise durable engineering jobs through a configured real MCP client."""
import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    root = Path(__file__).resolve().parents[1]
    settings = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["pcb-weaver"]
    if args.config:
        settings["env"]["PCB_WEAVER_CONFIG"] = str(Path(args.config).resolve(strict=True))
    parameters = StdioServerParameters(command=settings["command"], args=settings["args"],
                                      env={**os.environ, **settings["env"]})
    suffix = "-resume-" + uuid.uuid4().hex[:8] if args.resume_job else ""
    output = root / "docs" / "validation" / (args.project + suffix + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {"created": datetime.now(timezone.utc).isoformat(), "transport": "MCP stdio durable job",
                "project": args.project, "status": "running", "transitions": []}

    def save():
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    errlog = output.with_suffix(".stderr.log").open("w", encoding="utf-8")
    try:
        async with stdio_client(parameters, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as client:
                evidence["server"] = (await client.initialize()).model_dump(mode="json")
                evidence["tools"] = [tool.name for tool in (await client.list_tools()).tools]

                async def call(name, **arguments):
                    evidence["last_call"] = {"tool": name, "at": datetime.now(timezone.utc).isoformat()}
                    save()
                    response = await client.call_tool(name, arguments)
                    payload = next((item.text for item in response.content if item.type == "text"), "")
                    if response.isError:
                        raise RuntimeError(name + ": " + payload)
                    return json.loads(payload)

                evidence["environment"] = await call("environment_status")
                job = await call("get_engineering_job", job_id=args.resume_job) if args.resume_job else await call("submit_engineering_job", request={
                    "operation": "pipeline", "project": args.project,
                    "board_path": str(Path(args.board).resolve()),
                    "constraints": json.loads(Path(args.constraints).read_text(encoding="utf-8-sig")),
                    "candidate_count": 1, "passes": args.passes, "route": True, "release": True})
                if job["project"] != args.project:
                    raise RuntimeError("Resumed job belongs to a different project")
                evidence["job_id"] = job["id"]
                evidence["resumed"] = bool(args.resume_job)
                previous = None
                while True:
                    state = (job["status"], job["stage"])
                    if state != previous:
                        evidence["transitions"].append({"status": state[0], "stage": state[1], "at": job["updated"]})
                        evidence["job"] = job
                        save()
                        print(f'{job["id"]}: {state[0]} / {state[1]}', flush=True)
                        previous = state
                    if job["status"] not in {"queued", "running"}:
                        break
                    await asyncio.sleep(5)
                    job = await call("get_engineering_job", job_id=job["id"])
                evidence["job"] = job
                if job["status"] != "completed":
                    raise RuntimeError("Engineering job did not pass: " + job["status"])
                release = job["result"]["steps"]["release"]
                if release["status"] != "released":
                    raise RuntimeError("No recorded manufacturing release")
                evidence["archive_check"] = await call("verify_pcb_release_archive", archive_path=release["archive"])
                if evidence["archive_check"]["status"] != "verified":
                    raise RuntimeError("Manufacturing archive integrity failed")
                evidence["status"] = "passed"
    except BaseException as error:
        evidence["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)) else "failed"
        evidence["error"] = repr(error)
        raise
    finally:
        errlog.close()
        save()
        print("Evidence: " + str(output), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="system-mcp-" + uuid.uuid4().hex[:8])
    parser.add_argument("--board", default=str(root / "examples/system-controller/kit-dev-coldfire-xilinx_5213.kicad_pcb"))
    parser.add_argument("--constraints", default=str(root / "profiles/system-controller.json"))
    parser.add_argument("--passes", type=int, default=20, choices=range(1, 101), metavar="1..100")
    parser.add_argument("--resume-job", help="Reconnect to an existing job without resubmitting or overwriting prior evidence")
    parser.add_argument("--config", help="Use an explicit isolated toolchain configuration for this MCP client")
    args = parser.parse_args()
    if not args.project or len(args.project) > 64 or args.project[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.project):
        parser.error("Use a safe project identifier of up to 64 characters")
    asyncio.run(run(args))
