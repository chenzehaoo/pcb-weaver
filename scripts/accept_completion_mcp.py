"""Real desktop import/completion through MCP, including submitter disconnection."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pcb_weaver.storage import read_json,write_json,digest


async def run(args):
    root = args.root.resolve(strict=True)
    board = args.board.resolve(strict=True)
    config = read_json(root / ".mcp.json")["mcpServers"]["pcb-weaver"]
    parameters = StdioServerParameters(command=config["command"],args=config["args"],env={**os.environ,**config["env"]})
    evidence = {"status":"running","project":args.project,"source_board":str(board),"source_sha256":digest(board),
                "transport":"Real MCP stdio import, completion submit, disconnect and reconnect", "manufacturing_authorized":False}
    async def call(client,name,arguments):
        response = await asyncio.wait_for(client.call_tool(name,arguments),120)
        if response.isError:
            raise RuntimeError(str(response.content))
        return json.loads(next(c.text for c in response.content if c.type == "text"))
    try:
        async with stdio_client(parameters) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                tool = next(t for t in (await client.list_tools()).tools if t.name == "submit_pcb_completion")
                assert set(tool.inputSchema["required"]) == {"project","revision"}
                evidence["schema"] = tool.inputSchema
                imported = await call(client,"import_pcb_project",{"project":args.project,"board_path":str(board),
                    "constraints":read_json(board.parent / "constraints.json")})
                evidence["input_revision"] = imported["revision"]["id"]
                request = {"project":args.project,"revision":evidence["input_revision"]}
                if args.routing_policy:
                    request["options"] = {"routing_policy":args.routing_policy}
                job = await call(client,"submit_pcb_completion",request)
                evidence["job_id"] = job["id"]
                write_json(args.output,evidence)
                print(json.dumps({"job":job["id"],"stage":"submitter_disconnecting"}),flush=True)
        evidence["submitter_disconnected"] = True
        async with stdio_client(parameters) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                deadline = time.monotonic()+2400
                while True:
                    job = await call(client,"get_engineering_job",{"job_id":evidence["job_id"]})
                    if job["status"] not in {"queued","running"}:
                        break
                    if time.monotonic() > deadline:
                        await call(client,"cancel_engineering_job",{"job_id":job["id"]})
                        raise TimeoutError("Acceptance budget exhausted; requested cancellation")
                    await asyncio.sleep(8)
                evidence["job"] = job
                assert job["status"] == "completed",job.get("result",{}).get("blocking")
                result = job["result"]["steps"]["complete"]
                assert result["baseline"]["tracks"] == 0 and result["baseline"]["vias"] == 0
                assert result["best"]["tracks"] > 0 and result["best"]["unconnected"] == 0
                assert result["best"]["status"] == "passed" and not result["manufacturing_authorized"]
                assert digest(board) == evidence["source_sha256"]
                evidence.update(status="passed",revision=result["revision"])
    except BaseException as error:
        evidence.update(status="failed",error=type(error).__name__+": "+str(error))
        raise
    finally:
        write_json(args.output,evidence)
        print(json.dumps({"status":evidence["status"],"evidence":str(args.output)}),flush=True)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("root","board","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--project",required=True)
    parser.add_argument("--routing-policy",choices=("strict","normalize_widths"))
    asyncio.run(run(parser.parse_args()))
