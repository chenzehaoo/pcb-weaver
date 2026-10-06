"""Reverify a revision and read every inventory page through configured MCP."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pcb_weaver.storage import write_json


async def run(args):
    config = json.loads((args.root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["pcb-weaver"]
    target = {"project":"system-clearance-acceptance", "revision":args.revision}
    async with stdio_client(StdioServerParameters(command=config["command"], args=config["args"],
                           env={**os.environ, **config["env"]})) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, **extra):
                result = await client.call_tool(name, {**target,**extra})
                assert not result.isError, str(result.content)
                return json.loads(result.content[0].text)

            check = await call("verify_pcb_revision")
            assert check["status"] == "passed"
            assert check["drc"]["errors"] == check["drc"]["unconnected"] == check["erc"]["errors"] == 0
            diagnosis = await call("diagnose_pcb_repair")
            assert not diagnosis["proposals"]
            summary = await call("inspect_pcb_inventory", section="summary")
            counts = {}
            for section, field in {"components":"component_count", "nets":"net_count", "tracks":"track_count",
                                   "vias":"via_count", "layers":"layer_count"}.items():
                count = 0
                while True:
                    page = await call("inspect_pcb_inventory", section=section, offset=count, limit=500)
                    assert page["revision_digest"] == summary["revision_digest"]
                    count += len(page["items"])
                    if count >= page["total"]:
                        break
                    assert page["items"]
                assert count == summary["summary"][field]
                counts[section] = count
            evidence = {"status":"passed", **target, "verification_id":check["verification_id"],
                        "revision_digest":summary["revision_digest"], "summary":summary["summary"],
                        "paged_counts":counts, "remaining_repair_proposals":0, "verification_runs":1,
                        "board_mutations":0,"release_mutations":0,
                        "manufacturing_authorized":False}
            write_json(args.output, evidence)
            print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
