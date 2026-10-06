"""Native MCP negative acceptance: unrouted system input must not be released."""
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    settings = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["pcb-weaver"]
    parameters = StdioServerParameters(command=settings["command"], args=settings["args"],
                                      env={**os.environ, **settings["env"]})
    example = root / "examples/system-controller"
    project = "negative-system-" + uuid.uuid4().hex[:8]
    evidence = {"status": "running", "transport": "MCP stdio / real KiCad checks", "project": project, "calls": []}
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()

                async def call(name, **arguments):
                    response = await client.call_tool(name, arguments)
                    payload = next(item.text for item in response.content if item.type == "text")
                    if response.isError:
                        raise RuntimeError(payload)
                    result = json.loads(payload)
                    evidence["calls"].append({"tool": name, "arguments": arguments, "result": result})
                    return result

                imported = await call("import_pcb_project", project=project,
                                      board_path=str(example / "kit-dev-coldfire-xilinx_5213.kicad_pcb"),
                                      constraints=json.loads((example / "constraints.json").read_text(encoding="utf-8")))
                revision = imported["revision"]["id"]
                plan = await call("propose_pcb_layouts", project=project, revision=revision, count=1)
                assert not any(c["feasible"] for c in plan.get("candidates", [])), "Frozen mechanical conflicts unexpectedly passed"
                release = await call("build_pcb_manufacturing_release", project=project, revision=revision)
                assert release["status"] == "blocked", "Unrouted system input was released"
                check = release["verification"]
                assert check["drc"]["status"] == "ok" and check["drc"]["unconnected"] > 0, "Native DRC was not actually executed"
                assert check["erc"]["status"] == "ok", "Native ERC was not actually executed"
                assert check["constraints"]["passed"] is False
                history = await call("pcb_project_history", project=project)
                assert not any(e["kind"] == "release_created" for e in history["events"])
                evidence["status"] = "passed"
                evidence["assertion"] = "An unrouted, mechanically infeasible real system board was refused after native checks; no release record exists"
    except BaseException as error:
        evidence.update(status="failed", error=str(error))
        raise
    finally:
        output = root / "docs/validation/negative-system-mcp.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": evidence["status"], "project": project, "evidence": str(output)}), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
