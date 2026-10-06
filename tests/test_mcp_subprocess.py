"""Real stdio/subprocess isolation regression, explicitly not native EDA acceptance."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import textwrap

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


CHILD = r'''
import json
import os
from pathlib import Path
import sys
import threading
import time

root = Path(sys.argv[1])
def watchdog():
    time.sleep(12)
    (root / "exited.json").write_text(json.dumps({"reason": "watchdog", "pid": os.getpid()}))
    os._exit(91)
threading.Thread(target=watchdog, daemon=True).start()
(root / "started.json").write_text(json.dumps({"pid": os.getpid()}))
# This is the real inherited OS stdin, not a mock or a substituted Python stream.
received = sys.stdin.buffer.read(1)
(root / "stdin.json").write_text(json.dumps({"pid": os.getpid(), "received": list(received)}))
while not (root / "release").exists():
    (root / "heartbeat").write_text(str(time.monotonic_ns()))
    time.sleep(0.02)
print(json.dumps({"pid": os.getpid(), "stdin_eof": received == b"", "transport_fixture": True}), flush=True)
(root / "exited.json").write_text(json.dumps({"reason": "released", "pid": os.getpid()}))
'''


async def wait_json(path, timeout=5):
    async with asyncio.timeout(timeout):
        while True:
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_busy_stdio_polling_survives_real_child_stdin_read(tmp_path):
    root = Path(__file__).resolve().parents[1]
    probe = tmp_path / "probe"
    probe.mkdir()
    child_path = tmp_path / "stdin_reader.py"
    child_path.write_text(textwrap.dedent(CHILD), encoding="utf-8")
    bootstrap = tmp_path / "transport_server.py"
    bootstrap.write_text(textwrap.dedent(f'''
        from pathlib import Path
        import sys
        from pcb_weaver import server

        probe = Path({str(probe)!r})
        engine = server.service()
        queue = server.job_queue()
        def slow_route(project, revision, passes):
            operation = engine.toolchain._execute(
                sys.executable, ["-u", {str(child_path)!r}, str(probe)], timeout_seconds=15)
            return {{"status": "blocked", "revision": revision,
                    "reason": "Transport-only fixture; no native EDA validation performed",
                    "operation": operation}}
        engine.route_revision = slow_route
        # Explicit in-process transport fixture, not the production MCP lifecycle.
        queue.start()
        try:
            server.main()
        finally:
            (probe / "release").touch()
            queue.close()
            if queue.thread:
                queue.thread.join(timeout=16)
    '''), encoding="utf-8")
    env = dict(os.environ, PCB_WEAVER_WORKSPACE=str(tmp_path / "managed"), PYTHONDONTWRITEBYTECODE="1")
    env.pop("PCB_WEAVER_CONFIG", None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root / "src"), env.get("PYTHONPATH")]))
    params = StdioServerParameters(command=sys.executable, args=["-B", str(bootstrap)], env=env)
    evidence = {"scope": "Official MCP stdio + real Toolchain._execute child; not native EDA acceptance", "polls": []}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as client:
            await client.initialize()

            async def invoke(name, arguments):
                response = await client.call_tool(name, arguments)
                assert not response.isError, response
                if name == "list_engineering_jobs":
                    if response.structuredContent is not None:
                        return response.structuredContent["result"]
                    # SDK versions without structured list output emit one text block per item.
                    decoded = [json.loads(item.text) for item in response.content]
                    return decoded[0] if len(decoded) == 1 and isinstance(decoded[0], list) else decoded
                return json.loads(response.content[0].text)

            imported = await invoke("import_pcb_project", {"project": "stdin-poll",
                "board_path": str(root / "examples" / "routing-demo" / "two-layer.kicad_pcb")})
            revision = imported["revision"]["id"]
            queued = await invoke("submit_engineering_job", {"request": {
                "project": "stdin-poll", "operation": "route", "revision": revision}})
            identifier = queued["id"]
            try:
                started = await wait_json(probe / "started.json")
                # An inherited MCP stdin is still open here: this must be EOF from DEVNULL.
                stdin = await wait_json(probe / "stdin.json", timeout=3)
                assert stdin == {"pid": started["pid"], "received": []}
                evidence.update(job=identifier, child_pid=started["pid"], stdin=stdin)
                async with asyncio.timeout(3):
                    while not (probe / "heartbeat").exists():
                        await asyncio.sleep(0.02)
                initial_heartbeat = (probe / "heartbeat").stat().st_mtime_ns
                for index in range(12):
                    job = await invoke("get_engineering_job", {"job_id": identifier})
                    assert job["id"] == identifier and job["status"] == "running" and job["stage"] == "route"
                    assert job["result"]["revision"] == revision
                    listed = await invoke("list_engineering_jobs", {"project": "stdin-poll"})
                    assert any(item["id"] == identifier and item["status"] == "running" for item in listed)
                    assert not (probe / "exited.json").exists()
                    evidence["polls"].append({"index": index, "status": job["status"], "stage": job["stage"]})
                    await asyncio.sleep(0.04)
                assert (probe / "heartbeat").stat().st_mtime_ns > initial_heartbeat
                (probe / "release").touch()
                async with asyncio.timeout(6):
                    while True:
                        final = await invoke("get_engineering_job", {"job_id": identifier})
                        if final["status"] not in {"queued", "running"}:
                            break
                        await asyncio.sleep(0.02)
                assert final["status"] == "blocked"
                operation = final["result"]["steps"]["route"]["operation"]
                assert operation["status"] == "ok" and operation["returncode"] == 0
                assert json.loads(operation["stdout"]) == {
                    "pid": started["pid"], "stdin_eof": True, "transport_fixture": True}
                evidence.update(final_status=final["status"], operation=operation)
                (tmp_path / "mcp-subprocess-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            finally:
                (probe / "release").touch()
                # Bound cleanup even if inherited stdin stole a message or never reached EOF.
                if (probe / "started.json").exists():
                    exited = await wait_json(probe / "exited.json", timeout=16)
                    if "final_status" in evidence:
                        assert exited["reason"] == "released"
