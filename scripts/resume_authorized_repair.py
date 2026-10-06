"""Resume an authorized checkpoint through the real persistent MCP worker."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pcb_weaver.storage import read_json, write_json


async def run(args):
    reference_mode = bool(args.reference_project and args.reference_revision)
    if bool(args.reference_project) != bool(args.reference_revision):
        raise ValueError("Both reference identifiers are required")
    if args.multinet and reference_mode:
        raise ValueError("Autonomous repair must not use a reference")
    operation = "reference_repair" if reference_mode else "auto_repair"
    config = read_json(args.root / ".mcp.json")["mcpServers"]["pcb-weaver"]
    parameters = StdioServerParameters(command=config["command"],args=config["args"],env={**os.environ,**config["env"]})
    evidence = {"status":"running","project":args.project,"source_revision":args.revision,
                "authorization":"User explicitly confirmed bounded neckdown and local fanout; existing minima and old boards preserved",
                "manufacturing_authorized":False}
    if reference_mode:
        evidence.update(reference_project=args.reference_project,reference_revision=args.reference_revision,
            authorization="User authorized local multi-net rip-up/reroute; explicit compatible native reference; no manufacturing")
    if args.multinet:
        evidence.update(reference_used=False,authorization="User authorized source-only autonomous multi-net repair; no reference copper or manufacturing")
    async def call(client,name,arguments):
        response = await asyncio.wait_for(client.call_tool(name,arguments),120)
        if response.isError:
            raise RuntimeError(str(response.content))
        return json.loads(next(c.text for c in response.content if c.type == "text"))
    try:
        async with stdio_client(parameters) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                arguments = {"project":args.project,"revision":args.revision,
                    "options":{"allow_neckdown":True,"allow_local_adjustment":True,"max_attempts":24,
                               "time_budget_seconds":1800,"proposal_timeout_seconds":180,
                               "max_region_area_mm2":args.max_region_area}}
                if reference_mode:
                    arguments = {"project":args.project,"revision":args.revision,
                                 "reference_project":args.reference_project,"reference_revision":args.reference_revision}
                if args.multinet:
                    arguments["options"]["allow_multinet"] = True
                job = await call(client,"submit_pcb_"+operation,arguments)
                evidence["job_id"] = job["id"]
                write_json(args.output,evidence)
                print(json.dumps({"job":job["id"],"stage":"submitted"}),flush=True)
        evidence["submitter_disconnected"] = True
        async with stdio_client(parameters) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                deadline = time.monotonic()+2400
                last = None
                while True:
                    job = await call(client,"get_engineering_job",{"job_id":evidence["job_id"]})
                    evidence["job"] = job
                    write_json(args.output,evidence)
                    flow = job.get("result",{}).get("steps",{}).get(operation,{})
                    state = (job["status"],flow.get("stage"),flow.get("after_unconnected"),len(flow.get("attempts",[])))
                    if state != last:
                        print(json.dumps({"state":state,"revision":flow.get("revision")}),flush=True)
                        last = state
                    if job["status"] not in {"queued","running"}:
                        break
                    if time.monotonic() > deadline:
                        await call(client,"cancel_engineering_job",{"job_id":job["id"]})
                        raise TimeoutError("Client budget exhausted; cancellation requested")
                    await asyncio.sleep(10)
                evidence["status"] = "passed" if job["status"] == "completed" and flow.get("after_unconnected") == 0 else "blocked"
                evidence["revision"] = flow.get("revision",args.revision)
    except BaseException as error:
        evidence.update(status="failed",error=type(error).__name__+": "+str(error))
        raise
    finally:
        write_json(args.output,evidence)
        print(json.dumps({"status":evidence["status"],"output":str(args.output)}),flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("root","output"):
        p.add_argument("--"+name,type=Path,required=True)
    for name in ("project","revision"):
        p.add_argument("--"+name,required=True)
    p.add_argument("--authorized",action="store_true",required=True)
    p.add_argument("--max-region-area",type=float,default=900)
    p.add_argument("--reference-project")
    p.add_argument("--reference-revision")
    p.add_argument("--multinet",action="store_true")
    asyncio.run(run(p.parse_args()))
