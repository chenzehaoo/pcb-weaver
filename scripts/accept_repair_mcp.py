"""Submit a real repair through MCP, disconnect, reconnect and follow the worker."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pcb_weaver.storage import write_json


async def run(args):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["pcb-weaver"]
    profile = Path(args.config).resolve(strict=True) if args.config else root / "toolchain.unified.json"
    env = {**os.environ, **config["env"], "PCB_WEAVER_CONFIG": str(profile)}
    parameters = StdioServerParameters(command=config["command"], args=config["args"], env=env)
    output = root / "docs/validation" / (("clearance" if args.clearance else "repair") + "-mcp-" + uuid.uuid4().hex[:8] + ".json")
    evidence = {"status":"running", "transport":"real MCP stdio with disconnected submitter and persistent worker",
                "project":args.project,"revision":args.revision,"manufacturing_authorized":False}

    async def call(client, name, arguments):
        response = await asyncio.wait_for(client.call_tool(name, arguments), timeout=90)
        if response.isError:
            raise RuntimeError(str(response.content))
        return json.loads(next(c.text for c in response.content if c.type == "text"))

    try:
        async with stdio_client(parameters) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                evidence["tools"] = [t.name for t in (await client.list_tools()).tools]
                if not args.clearance:
                    evidence["diagnosis"] = await call(client,"diagnose_pcb_repair",{"project":args.project,"revision":args.revision})
                arguments = {"project":args.project,"revision":args.revision,"nets":args.net,"region":args.region}
                if not args.clearance:
                    arguments.update(remove_ids=args.remove_id, passes=args.passes)
                submitted = await call(client, "submit_pcb_clearance_repair" if args.clearance else "submit_pcb_repair", arguments)
                evidence["job_id"] = submitted["id"]
                write_json(output,evidence)
                print(json.dumps({"job":submitted["id"],"stage":"submitter_disconnecting"}),flush=True)
        evidence["submitter_disconnected"] = True
        async with stdio_client(parameters) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                deadline = time.monotonic() + 900
                while True:
                    job = await call(client,"get_engineering_job",{"job_id":evidence["job_id"]})
                    if job["status"] not in {"queued","running"}:
                        break
                    if time.monotonic() > deadline:
                        await call(client,"cancel_engineering_job",{"job_id":job["id"]})
                        raise TimeoutError("Repair job exceeded the acceptance budget; cancellation requested")
                    await asyncio.sleep(3)
                evidence["job"] = job
                if job["status"] != "completed":
                    raise RuntimeError("Repair did not complete successfully; inspect recorded job evidence")
                assert job["result"]["revision"] != args.revision
                evidence["status"] = "passed"
    except BaseException as error:
        evidence.update(status="failed",error=type(error).__name__+": "+str(error))
        raise
    finally:
        write_json(output,evidence)
        print(json.dumps({"status":evidence["status"],"evidence":str(output)}),flush=True)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",required=True)
    parser.add_argument("--revision",required=True)
    parser.add_argument("--net",action="append",required=True)
    parser.add_argument("--region",nargs=4,type=float,required=True)
    parser.add_argument("--remove-id",action="append",default=[])
    parser.add_argument("--passes",type=int,default=3)
    parser.add_argument("--clearance", action="store_true")
    parser.add_argument("--config", help="Explicit toolchain profile snapshotted with the job")
    asyncio.run(run(parser.parse_args()))
