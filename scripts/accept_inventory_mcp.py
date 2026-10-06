"""Read every inventory page of a real complex revision through MCP stdio."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pcb_weaver.storage import write_json


async def run(args):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["pcb-weaver"]
    evidence = {"scope": "Real MCP inventory pagination, not electrical or industrial signoff", "status": "running"}
    try:
        async with stdio_client(StdioServerParameters(command=config["command"], args=config["args"], env={**os.environ, **config["env"]})) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                async def page(section, offset=0):
                    result = await client.call_tool("inspect_pcb_inventory", {"project": args.project, "revision": args.revision,
                        "section": section, "offset": offset, "limit": 100})
                    if result.isError:
                        raise RuntimeError(str(result.content))
                    return json.loads(result.content[0].text)
                summary = await page("summary")
                evidence.update({k: summary[k] for k in ("project", "revision", "revision_digest", "summary", "coverage", "sources")})
                counts = {}
                for section, count_field in {"components": "component_count", "nets": "net_count", "tracks": "track_count", "vias": "via_count", "layers": "layer_count"}.items():
                    rows, offset = [], 0
                    while True:
                        result = await page(section, offset)
                        assert result["revision_digest"] == summary["revision_digest"]
                        rows.extend(result["items"])
                        offset += len(result["items"])
                        if offset >= result["total"]:
                            break
                        assert result["items"], "Pagination did not advance"
                    assert len(rows) == summary["summary"][count_field]
                    counts[section] = len(rows)
                    if section == "components":
                        assert sum(len(c["pads"]) for c in rows) == summary["summary"]["pad_count"]
                        assert all(c["category_basis"]["verified"] is False for c in rows)
                    if section == "nets":
                        assert all(n["connectivity_status"] == "not_evaluated" for n in rows)
                evidence.update(status="passed", paged_counts=counts)
    except BaseException as error:
        evidence.update(status="failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        path = root / "docs/validation/inventory-mcp-v3.json"
        write_json(path, evidence)
        print(json.dumps({"status": evidence["status"], "evidence": str(path)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="system-mcp-acceptance")
    parser.add_argument("--revision", default="r-9b5f5075e65e4931")
    asyncio.run(run(parser.parse_args()))
