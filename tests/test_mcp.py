import json
import os
from pathlib import Path
import sys
import asyncio
import threading

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pcb_weaver.jobs import JobQueue
from pcb_weaver.models import ToolchainConfig


@pytest.fixture
def persistent_worker(tmp_path):
    queue = JobQueue(tmp_path / "data", ToolchainConfig().model_dump(exclude_none=True))
    queue.start()
    yield queue
    queue.close()
    queue.thread.join(timeout=20)
    assert not queue.thread.is_alive()


@pytest.mark.asyncio
async def test_real_stdio_handshake_schema_resource_and_project_tools(tmp_path, persistent_worker):
    env = dict(os.environ, PCB_WEAVER_WORKSPACE=str(tmp_path / "data"))
    env.pop("PCB_WEAVER_CONFIG", None)
    config = StdioServerParameters(command=sys.executable, args=["-m", "pcb_weaver.server"], env=env)
    example = Path(__file__).resolve().parents[1] / "examples" / "two-layer"
    constraints = json.loads((example / "constraints.json").read_text())
    async with stdio_client(config) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"import_pcb_project", "propose_pcb_layouts", "analyze_pcb_eco", "build_pcb_manufacturing_release"} <= names
            resources = await client.read_resource("pcb-weaver://constraints/schema")
            assert '"schema_version"' in resources.contents[0].text
            result = await client.call_tool("import_pcb_project", {"project": "mcp-test", "board_path": str(example / "two-layer.kicad_pcb"), "constraints": constraints})
            assert not result.isError
            imported = json.loads(result.content[0].text)
            revision = imported["revision"]["id"]
            result = await client.call_tool("inspect_pcb_revision", {"project": "mcp-test", "revision": revision})
            assert not result.isError
            assert len(json.loads(result.content[0].text)["board"]["footprints"]) == 4
            result = await client.call_tool("inspect_pcb_inventory", {"project": "mcp-test", "revision": revision, "section": "components", "limit": 2})
            assert not result.isError
            page = json.loads(result.content[0].text)
            assert page["total"] == 4 and len(page["items"]) == 2
            assert page["summary"]["component_count"] == 4
            resource = await client.read_resource("pcb-weaver://integration/openapi")
            assert json.loads(resource.contents[0].text)["openapi"] == "3.1.1"
            invalid = await client.call_tool("import_pcb_project", {"project": "../../escape", "board_path": str(example / "two-layer.kicad_pcb")})
            assert invalid.isError
            prompt = await client.get_prompt("pcb_engineering_workflow", {"project": "mcp-test", "board_path": "input.kicad_pcb"})
            assert prompt.messages

            async def invoke(name, arguments):
                response = await client.call_tool(name, arguments)
                assert not response.isError, response
                return json.loads(response.content[0].text)

            plan = await invoke("propose_pcb_layouts", {"project": "mcp-test", "revision": revision, "count": 2})
            candidate = next(c for c in plan["candidates"] if c["feasible"])
            applied = await invoke("apply_pcb_layout", {"project": "mcp-test", "revision": revision,
                                   "plan_id": plan["plan_id"], "candidate_id": candidate["id"]})
            child = applied["revision"]["id"]
            assert child != revision
            eco = await invoke("analyze_pcb_eco", {"project": "mcp-test", "before": revision, "after": child})
            assert eco["requires_new_verification"] and eco["impact"]["invalidated_artifacts"]
            routing = await invoke("autoroute_pcb_revision", {"project": "mcp-test", "revision": child})
            assert routing["status"] == "blocked" and "Critical" in routing["reason"]
            report = await invoke("export_pcb_review_report", {"project": "mcp-test", "revision": child})
            assert report["verification_status"] == "not_verified" and Path(report["path"]).is_file()
            queued = await invoke("submit_engineering_job", {"request": {
                "operation": "plan", "project": "mcp-test", "revision": child, "candidate_count": 1}})
            import asyncio
            async with asyncio.timeout(20):
                while True:
                    job = await invoke("get_engineering_job", {"job_id": queued["id"]})
                    if job["status"] not in {"queued", "running"}:
                        break
                    await asyncio.sleep(0.1)
            assert job["status"] == "completed"
            assert "plan" in job["result"]["steps"] and "report" in job["result"]["steps"]


@pytest.mark.asyncio
async def test_job_survives_submitting_mcp_session_exit(tmp_path, persistent_worker, monkeypatch):
    example = Path(__file__).resolve().parents[1] / "examples" / "routing-demo"
    constraints = json.loads((example / "constraints.json").read_text())
    imported = persistent_worker.engine.import_project("survives", example / "two-layer.kicad_pcb", constraints)
    entered, release = threading.Event(), threading.Event()
    original = persistent_worker.engine.plan_layout

    def held_plan(*args):
        entered.set()
        if not release.wait(timeout=15):
            raise RuntimeError("Lifecycle regression did not release worker")
        return original(*args)

    monkeypatch.setattr(persistent_worker.engine, "plan_layout", held_plan)
    env = dict(os.environ, PCB_WEAVER_WORKSPACE=str(tmp_path / "data"))
    env.pop("PCB_WEAVER_CONFIG", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "pcb_weaver.server"], env=env)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool("submit_engineering_job", {"request": {
                    "project": "survives", "operation": "plan", "revision": imported["revision"]["id"], "candidate_count": 1}})
                assert not result.isError
                job = json.loads(result.content[0].text)
                async with asyncio.timeout(10):
                    while not entered.is_set():
                        await asyncio.sleep(0.05)
        assert persistent_worker.thread.is_alive()
        assert persistent_worker.get(job["id"])["status"] == "running"
    finally:
        release.set()
    async with asyncio.timeout(20):
        while persistent_worker.get(job["id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(0.1)
    assert persistent_worker.get(job["id"])["status"] == "completed"
