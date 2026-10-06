"""Run an actual MCP stdio acceptance sequence using the installed client config."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(root, project, route, toolchain_config=None, passes=5, example_dir=None):
    config = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["pcb-weaver"]
    if toolchain_config:
        config["env"]["PCB_WEAVER_CONFIG"] = str(Path(toolchain_config).resolve(strict=True))
    parameters = StdioServerParameters(command=config["command"], args=config["args"],
                                       env=dict(os.environ, **config["env"]))
    example = Path(example_dir).resolve(strict=True) if example_dir else root / "examples" / "routing-demo"
    evidence = {"created": datetime.now(timezone.utc).isoformat(), "project": project,
                "transport": "MCP stdio", "route_requested": route, "calls": [], "status": "running"}
    output = root / "docs" / "validation" / (project + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                evidence["server"] = (await client.initialize()).model_dump(mode="json")
                evidence["tools"] = [tool.name for tool in (await client.list_tools()).tools]

                async def call(name, **arguments):
                    response = await client.call_tool(name, arguments)
                    text = next((item.text for item in response.content if item.type == "text"), "")
                    if response.isError:
                        raise RuntimeError(name + ": " + text)
                    result = json.loads(text)
                    evidence["calls"].append({"tool": name, "arguments": arguments, "result": result})
                    print(name + ": " + (result.get("status", "ok") if isinstance(result, dict) else "ok"), flush=True)
                    return result

                await call("environment_status")
                imported = await call("import_pcb_project", project=project,
                                      board_path=str(example / "two-layer.kicad_pcb"),
                                      constraints=json.loads((example / "constraints.json").read_text()))
                initial = imported["revision"]["id"]
                await call("inspect_pcb_revision", project=project, revision=initial)
                plan = await call("propose_pcb_layouts", project=project, revision=initial, count=3)
                candidate = next((item for item in plan["candidates"] if item["feasible"]), None)
                if candidate is None:
                    raise RuntimeError("No feasible layout candidate")
                applied = await call("apply_pcb_layout", project=project, revision=initial,
                                     plan_id=plan["plan_id"], candidate_id=candidate["id"])
                current = applied["revision"]["id"]
                if route:
                    routed = await call("autoroute_pcb_revision", project=project, revision=current, passes=passes)
                    if routed["status"] != "routed_unverified":
                        raise RuntimeError("Actual routing blocked; see recorded tool result")
                    current = routed["revision"]["id"]
                    verified = await call("verify_pcb_revision", project=project, revision=current)
                    if verified["status"] != "passed":
                        raise RuntimeError("Engineering verification blocked; see recorded findings")
                    release = await call("build_pcb_manufacturing_release", project=project, revision=current)
                    if release["status"] != "released":
                        raise RuntimeError("Manufacturing release blocked; see recorded findings")
                    archive = await call("verify_pcb_release_archive", archive_path=release["archive"])
                    if archive["status"] != "verified":
                        raise RuntimeError("Release archive integrity failed")
                    evidence["archive"] = release["archive"]
                evidence["report"] = await call("export_pcb_review_report", project=project, revision=current)
                await call("analyze_pcb_eco", project=project, before=initial, after=current)
                await call("pcb_project_history", project=project)
                evidence["final_revision"] = current
                evidence["status"] = "passed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["error"] = str(error)
        raise
    finally:
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Evidence: " + str(output), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="mcp-acceptance-" + uuid.uuid4().hex[:8])
    parser.add_argument("--route", action="store_true")
    parser.add_argument("--config", help="Explicit isolated toolchain configuration")
    parser.add_argument("--example-dir", help="Fixture directory containing two-layer.kicad_pcb and constraints.json")
    parser.add_argument("--passes", type=int, default=5, choices=range(1, 101), metavar="1..100")
    args = parser.parse_args()
    if not args.project or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in args.project):
        parser.error("Use an alphanumeric project identifier with optional hyphens or underscores")
    asyncio.run(run(Path(__file__).resolve().parents[1], args.project, args.route, args.config, args.passes, args.example_dir))
