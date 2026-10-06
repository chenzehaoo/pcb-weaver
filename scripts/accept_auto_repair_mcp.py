"""Accept the deployed MCP auto-repair entry and persistent worker on a passed board."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pcb_weaver.storage import write_json


async def run(args):
    root = Path(args.root).resolve(strict=True)
    config = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["pcb-weaver"]
    parameters = StdioServerParameters(command=config["command"], args=config["args"],
        env={**os.environ, **config["env"], "PCB_WEAVER_CONFIG":str(root / "toolchain.unified.json")})
    evidence = {"status":"running", "project":args.project, "revision":args.revision,
                "scope":"Real MCP submission and reconnect; already-passed board must remain unchanged",
                "manufacturing_authorized":False}

    async def call(client,name,arguments):
        response = await asyncio.wait_for(client.call_tool(name,arguments),90)
        if response.isError:
            raise RuntimeError(str(response.content))
        return json.loads(next(c.text for c in response.content if c.type == "text"))

    try:
        async with stdio_client(parameters) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                tool = next(t for t in (await client.list_tools()).tools if t.name == "submit_pcb_auto_repair")
                assert set(tool.inputSchema["required"]) == {"project","revision"}
                evidence["schema"] = tool.inputSchema
                job = await call(client,tool.name,{"project":args.project,"revision":args.revision})
                evidence["job_id"] = job["id"]
                print(json.dumps({"job":job["id"],"stage":"submitter_disconnecting"}),flush=True)
                write_json(Path(args.output),evidence)
        evidence["submitter_disconnected"] = True
        async with stdio_client(parameters) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                deadline = time.monotonic()+900
                while True:
                    job = await call(client,"get_engineering_job",{"job_id":evidence["job_id"]})
                    if job["status"] not in {"queued","running"}:
                        break
                    if time.monotonic() > deadline:
                        await call(client,"cancel_engineering_job",{"job_id":job["id"]})
                        raise TimeoutError("Acceptance budget exhausted; cancellation requested")
                    await asyncio.sleep(3)
                evidence["job"] = job
                assert job["status"] == "completed", job
                result = job["result"]["steps"]["auto_repair"]
                assert result["already_passed"] and result["revision"] == args.revision
                assert result["after_unconnected"] == 0 and result["attempts"] == []
                assert not result["manufacturing_authorized"]
                evidence["status"] = "passed"
    except BaseException as error:
        evidence.update(status="failed",error=type(error).__name__+": "+str(error))
        raise
    finally:
        write_json(Path(args.output),evidence)
        print(json.dumps({"status":evidence["status"],"output":args.output}),flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("root","project","revision","output"):
        parser.add_argument("--"+flag,required=True)
    asyncio.run(run(parser.parse_args()))
