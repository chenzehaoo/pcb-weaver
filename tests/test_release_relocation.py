"""Copy/transport regression with fake EDA outputs, not native acceptance."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
from starlette.testclient import TestClient

from pcb_weaver import catalog
from pcb_weaver.jobs import JobQueue
from pcb_weaver.platform import create_app
from pcb_weaver.storage import digest
from test_service import imported, setup


@pytest.fixture
def copied_release(setup, tmp_path):
    engine, revision = imported(setup)
    release = engine.build_release("demo", revision["id"])
    assert release["status"] == "released"
    report = catalog.generate_report(engine, "demo", revision["id"])
    queue = JobQueue(engine.store.root)
    job = queue.submit({"project": "demo", "operation": "release", "revision": revision["id"]})
    # Seed a completed historical job with the actual service outputs, including
    # their old absolute paths; no worker or native operation is started here.
    with queue.store.connect() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    queue._finish(job["id"], "completed", {"project": "demo", "revision": revision["id"],
                                         "steps": {"release": release, "report": report}})
    original = {
        "job": queue.get(job["id"]), "history": engine.store.history("demo"),
        "releases": catalog.releases(engine, "demo"),
        "verification": catalog.verification(engine, "demo", revision["id"]),
        "report": catalog.report_bytes(engine, "demo", revision["id"]),
        "archive": Path(release["archive"]).read_bytes(),
    }
    destination = tmp_path / "Desktop delivery" / "data"
    # Store connections are scoped and closed before copying. This does not
    # model a hot SQLite/WAL backup of a running deployment.
    shutil.copytree(engine.store.root, destination)
    return {"source": engine.store.root, "root": destination, "release": release,
            "revision": revision["id"], "job_id": job["id"], "original": original}


def test_relocated_history_download_and_report_do_not_need_old_root(copied_release):
    case = copied_release
    source = case["source"]
    assert source.is_relative_to(case["root"].parents[1])
    source.rename(source.with_name("offline-original"))
    assert not Path(case["release"]["archive"]).exists()
    app = create_app(case["root"], start_worker=False)
    engine = app.state.queue.engine
    release_id = case["release"]["release_id"]
    actual = case["root"] / "projects" / "demo" / "releases" / (release_id + ".zip")
    assert catalog.archive_path(engine, "demo", release_id) == actual
    assert catalog.archive_bytes(engine, "demo", release_id) == case["original"]["archive"]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/projects").json()[0]["project"] == "demo"
        assert client.get("/api/projects/demo/releases").json() == case["original"]["releases"]
        download = client.get(f"/api/projects/demo/releases/{release_id}")
        assert download.status_code == 200
        assert download.content == case["original"]["archive"]
        assert download.headers["content-disposition"] == f'attachment; filename="{release_id}.zip"'
        report = client.get(f"/api/projects/demo/revisions/{case['revision']}/report")
        assert report.status_code == 200 and report.content == case["original"]["report"]
        detail = client.get(f"/api/jobs/{case['job_id']}").json()
        assert detail == case["original"]["job"]
        assert detail["result"]["steps"]["release"]["archive"] == case["release"]["archive"]
        assert client.get("/api/jobs").json()[0]["result"]["revision"] == case["revision"]
    assert engine.store.history("demo") == case["original"]["history"]
    assert catalog.verification(engine, "demo", case["revision"]) == case["original"]["verification"]
    assert engine.verify_release(actual)["status"] == "verified"
    assert app.state.queue.thread is None


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_copy_download_never_falls_back_to_valid_original(copied_release, damage):
    case = copied_release
    app = create_app(case["root"], start_worker=False)
    release_id = case["release"]["release_id"]
    actual = case["root"] / "projects" / "demo" / "releases" / (release_id + ".zip")
    assert digest(Path(case["release"]["archive"])) == case["release"]["sha256"]
    if damage == "missing":
        actual.unlink()
    else:
        actual.write_bytes(b"changed copied archive")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get(f"/api/projects/demo/releases/{release_id}").status_code == 400
    with pytest.raises((ValueError, OSError)):
        catalog.archive_path(app.state.queue.engine, "demo", release_id)
    assert app.state.queue.get(case["job_id"]) == case["original"]["job"]


@pytest.mark.asyncio
async def test_official_stdio_mcp_verifies_actual_relocated_archive(copied_release):
    case = copied_release
    source = case["source"]
    assert source.is_relative_to(case["root"].parents[1])
    source.rename(source.with_name("offline-original"))
    actual = case["root"] / "projects" / "demo" / "releases" / (case["release"]["release_id"] + ".zip")
    env = dict(os.environ, PCB_WEAVER_WORKSPACE=str(case["root"]))
    env.pop("PCB_WEAVER_CONFIG", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "pcb_weaver.server"], env=env)
    async with asyncio.timeout(30):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                response = await client.call_tool("verify_pcb_release_archive", {"archive_path": str(actual)})
                assert not response.isError
                verified = json.loads(response.content[0].text)
                assert verified["status"] == "verified"
                assert verified["sha256"] == case["release"]["sha256"]
                assert "not cryptographic publisher authentication" in verified["scope"]
                old = await client.call_tool("verify_pcb_release_archive", {"archive_path": case["release"]["archive"]})
                assert old.isError
                # Alter one member but keep its old manifest digest. The real
                # MCP integrity checker must reject even at the new valid path.
                with zipfile.ZipFile(actual) as archive:
                    members = {name: archive.read(name) for name in archive.namelist()}
                members["manufacturing/test.gbr"] = b"altered copied fixture"
                with zipfile.ZipFile(actual, "w") as archive:
                    for name, raw in members.items():
                        archive.writestr(name, raw)
                response = await client.call_tool("verify_pcb_release_archive", {"archive_path": str(actual)})
                assert not response.isError
                failed = json.loads(response.content[0].text)
                assert failed["status"] == "failed"
                assert "manufacturing/test.gbr" in failed["differences"]
    relocated = JobQueue(case["root"])
    assert relocated.get(case["job_id"]) == case["original"]["job"]
    assert relocated.store.history("demo") == case["original"]["history"]
