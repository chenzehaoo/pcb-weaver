import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pcb_weaver.platform import create_app
from pcb_weaver.storage import digest


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1", headers={"X-PCB-Client": "workbench"}) as client:
        yield client


def test_workbench_assets_and_read_endpoints(client):
    assert client.get("/").status_code == 200
    assert "PCB WEAVER" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/api/health").json()["deployment"] == "local_single_user"
    assert client.get("/api/projects").json() == []
    assert client.get("/api/jobs").json() == []
    assert client.get("/api/schema").json()["properties"]["board"]
    assert client.get("/api/presets").json()


def test_http_job_roundtrip_and_inspection(client):
    preset = next(p for p in client.get("/api/presets").json() if p["id"] == "routing-demo")
    response = client.post("/api/jobs", json={"project": "http-demo", "operation": "pipeline", "board_path": preset["board_path"], "constraints": preset["constraints"], "route": False})
    assert response.status_code == 202
    identifier = response.json()["id"]
    assert client.app.state.queue.run_once()
    job = client.get("/api/jobs/" + identifier).json()
    assert job["status"] == "completed"
    revision = job["result"]["revision"]
    path = f"/api/projects/http-demo/revisions/{revision}"
    inspection = client.get(path).json()
    assert inspection["verification"]["status"] == "not_verified"
    assert len(inspection["board"]["footprints"]) == 4
    assert client.get(path + "/report").status_code == 200
    assert client.get("/api/projects").json()[0]["revision_count"] == 2
    before = client.get("/api/projects/http-demo/revisions").json()[0]["id"]
    assert client.get(f"/api/projects/http-demo/eco?before={before}&after={revision}").json()["requires_new_verification"]


def test_system_preset_declares_execution_profile_without_changing_benchmark(client):
    preset = next(p for p in client.get("/api/presets").json() if p["id"] == "system-controller")
    source = Path(preset["constraint_source"])
    assert source.parent.name == "profiles"
    assert preset["constraints"] == json.loads(source.read_text(encoding="utf-8"))
    frozen = json.loads((Path(preset["board_path"]).parent / "constraints.json").read_text(encoding="utf-8"))
    assert "COM_SEL201" in frozen["fixed_references"]
    assert "COM_SEL201" not in preset["constraints"]["fixed_references"]


def test_cross_origin_and_rebinding_blocked(client):
    assert client.get("/api/health", headers={"Host": "attacker.invalid"}).status_code == 403
    assert client.post("/api/jobs", headers={"Origin": "https://attacker.invalid"}, json={}).status_code == 403
    assert client.post("/api/jobs", headers={"X-PCB-Client": ""}, json={}).status_code == 403


@pytest.mark.parametrize("host", ["testserver", "user@localhost", "localhost/path", "localhost?x=1", "localhost:bad", "localhost:99999", "[broken"])
def test_malformed_or_non_loopback_authority_rejected(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_read_without_origin_is_denied(client, site):
    assert client.get("/api/jobs", headers={"Sec-Fetch-Site": site}).status_code == 403


def test_duplicate_host_and_origin_are_rejected(client):
    assert client.get("/api/health", headers=[("Host", "127.0.0.1"), ("Host", "evil.invalid")]).status_code == 403
    assert client.get("/api/health", headers=[("Origin", "http://127.0.0.1"), ("Origin", "http://evil.invalid")]).status_code == 403
    assert client.get("/api/health", headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/health", headers={"Origin": "http://127.0.0.1"}).status_code == 200


def test_remote_peer_cannot_spoof_localhost_header(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.5", 12345)) as remote:
        assert remote.get("/api/health").status_code == 403


def test_report_download_requires_recorded_fresh_bytes_and_is_sandboxed(client):
    from pcb_weaver import catalog
    from pcb_weaver.storage import write_json
    engine = client.app.state.queue.engine
    source = Path(__file__).resolve().parents[1] / "examples" / "routing-demo" / "two-layer.kicad_pcb"
    revision = engine.import_project("demo", str(source))["revision"]
    folder = engine.store.revision_dir("demo", revision["id"])
    path = folder / "review.html"
    url = f'/api/projects/demo/revisions/{revision["id"]}/report'
    path.write_text("<script>malicious()</script>", encoding="utf-8")
    assert client.get(url).status_code == 400
    catalog.generate_report(engine, "demo", revision["id"])
    response = client.get(url)
    assert response.status_code == 200
    assert "sandbox;" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    original = path.read_bytes()
    path.write_bytes(b"changed report")
    assert client.get(url).status_code == 400
    path.write_bytes(original)
    run = folder / "verification" / "v-new"
    write_json(run / "result.json", {"verification_id": "v-new", "revision": revision["id"],
               "revision_digest": revision["digest"], "created": "2026-09-07T01:00:00Z", "status": "blocked", "evidence_hashes": {}})
    engine.store.event("demo", "verification_completed", {"revision": revision["id"], "verification_id": "v-new",
                       "status": "blocked", "sha256": digest(run / "result.json")})
    assert client.get(url).status_code == 400
    catalog.generate_report(engine, "demo", revision["id"])
    assert client.get(url).status_code == 200


def test_invalid_and_large_json_rejected(client):
    assert client.post("/api/jobs", json={}).status_code == 400
    assert client.post("/api/jobs", content="not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/jobs", content="x" * 2_000_001, headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/jobs", content="{}").status_code == 400


def test_only_recorded_untampered_release_can_download(client):
    engine = client.app.state.queue.engine
    assert client.get("/api/projects/demo/releases/release-one").status_code == 400
    path = engine.store.project_dir("demo") / "releases" / "release-one.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"recorded-test-artifact")
    engine.store.event("demo", "release_created", {"release_id": "release-one", "revision": "r-one", "sha256": digest(path)})
    assert client.get("/api/projects/demo/releases/release-one").content == b"recorded-test-artifact"
    path.write_bytes(b"changed")
    assert client.get("/api/projects/demo/releases/release-one").status_code == 400


def test_import_snapshots_source_attribution(tmp_path):
    import shutil
    from pcb_weaver.service import EngineeringService
    source = tmp_path / "input"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "routing-demo", source)
    (source / "PROVENANCE.json").write_text('{"source":"upstream","license":"CC-BY-SA-4.0"}')
    (source / "LICENSE.KiCad.README").write_text("Attribution must travel with this design")
    service = EngineeringService(tmp_path / "workspace")
    result = service.import_project("attribution", str(source / "two-layer.kicad_pcb"))
    assert {"PROVENANCE.json", "LICENSE.KiCad.README"} <= result["revision"]["files"].keys()
