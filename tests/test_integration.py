import io
import json
from pathlib import Path
import zipfile

import pytest
from jsonschema import Draft202012Validator, ValidationError
from starlette.testclient import TestClient

from pcb_weaver.integration import IntegrationAccess, openapi_document
from pcb_weaver.platform import create_app


TOKEN = "test_token_" + "x" * 32
PREFIX = "/api/integration/v1"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples/routing-demo/two-layer.kicad_pcb"


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, start_worker=False, integration_access=IntegrationAccess(TOKEN, ("allowed",), True))
    revision = app.state.queue.engine.import_project("allowed", str(EXAMPLE))["revision"]["id"]
    app.state.queue.engine.import_project("denied", str(EXAMPLE))
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer " + TOKEN}) as client:
        client.revision = revision
        yield client


def request(client):
    return {"project": "allowed", "revision": client.revision, "operation": "plan", "candidate_count": 1}


def test_scope_and_machine_jobs_without_browser_header(client):
    assert [p["project"] for p in client.get(PREFIX + "/projects").json()] == ["allowed"]
    assert client.get(PREFIX + "/projects/denied/revisions").status_code == 403
    assert client.get(PREFIX + "/projects/allowed/revisions").status_code == 200
    submitted = client.post(PREFIX + "/jobs", json=request(client), headers={"Idempotency-Key": "order-1"})
    assert submitted.status_code == 202, submitted.text
    result = submitted.json()
    assert result["status"] == "queued"
    assert "runtime" not in result and "request" not in result
    assert client.get(PREFIX + "/jobs/" + result["id"]).status_code == 200


def test_job_retries_are_idempotent_and_conflicts_are_409(client):
    headers = {"Idempotency-Key": "same-order"}
    first = client.post(PREFIX + "/jobs", json=request(client), headers=headers)
    again = client.post(PREFIX + "/jobs", json=request(client), headers=headers)
    assert first.status_code == again.status_code == 202
    assert first.json()["id"] == again.json()["id"]
    changed = {**request(client), "candidate_count": 2}
    assert client.post(PREFIX + "/jobs", json=changed, headers=headers).status_code == 409
    assert len(client.app.state.queue.list()) == 1


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}, {"Idempotency-Key": "x" * 129}, {"Idempotency-Key": "../bad"}])
def test_invalid_idempotency_rejected(client, headers):
    assert client.post(PREFIX + "/jobs", json=request(client), headers=headers).status_code == 400
    assert client.app.state.queue.list() == []


def test_duplicate_auth_and_idempotency_rejected(client):
    assert client.get(PREFIX + "/projects", headers=[("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)]).status_code == 401
    assert client.post(PREFIX + "/jobs", json=request(client), headers=[("Idempotency-Key", "a"), ("Idempotency-Key", "b")]).status_code == 400


@pytest.mark.parametrize("authorization", ["", "Bearer bad", "Basic " + TOKEN, "Bearer " + TOKEN + "x"])
def test_unauthorized_requests_fail(client, authorization):
    assert client.get(PREFIX + "/projects", headers={"Authorization": authorization}).status_code == 401
    assert client.post(PREFIX + "/jobs", json={}, headers={"Authorization": authorization}).status_code == 401


def test_cross_site_and_nonlocal_cannot_use_valid_token(client, tmp_path):
    assert client.get(PREFIX + "/projects", headers={"Origin": "https://external.example"}).status_code == 403
    assert client.get(PREFIX + "/projects", headers={"Host": "external.example"}).status_code == 403
    assert client.get(PREFIX + "/projects", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    app = create_app(tmp_path, start_worker=False, integration_access=IntegrationAccess(TOKEN, ("allowed",)))
    with TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.1", 123)) as remote:
        assert remote.get(PREFIX + "/projects", headers={"Authorization": "Bearer " + TOKEN}).status_code == 403


def test_machine_cannot_import_arbitrary_paths_or_change_scope(client):
    payload = {"project": "allowed", "operation": "pipeline", "board_path": str(EXAMPLE)}
    assert client.post(PREFIX + "/jobs", json=payload, headers={"Idempotency-Key": "one"}).status_code == 400
    payload = {**request(client), "project": "denied"}
    assert client.post(PREFIX + "/jobs", json=payload, headers={"Idempotency-Key": "two"}).status_code == 403
    denied = client.app.state.queue.submit({**request(client), "project": "denied", "revision": client.app.state.queue.store.list_revisions("denied")[0]["id"]})
    assert client.get(PREFIX + "/jobs/" + denied["id"]).status_code == 404


def test_machine_bridge_does_not_gain_clearance_write_authority(client):
    payload = {**request(client), "operation": "clearance", "repair_nets": ["VIN"],
               "repair_region": [20, 20, 40, 45]}
    response = client.post(PREFIX + "/jobs", json=payload, headers={"Idempotency-Key": "clearance"})
    assert response.status_code == 400
    assert client.app.state.queue.list() == []


def test_bridge_disabled_and_readonly_defaults(tmp_path):
    for access, expected in [(IntegrationAccess(), 503), (IntegrationAccess(TOKEN), 403)]:
        with TestClient(create_app(tmp_path, start_worker=False, integration_access=access), base_url="http://127.0.0.1",
                        headers={"Authorization": "Bearer " + TOKEN}) as client:
            assert client.post(PREFIX + "/jobs", json={}).status_code == expected
            caps = client.get("/api/integration/capabilities").json()
            assert caps["certified_platforms"] == [] and caps["external_data_transfer"] is False
            assert TOKEN not in json.dumps(caps)


def test_public_contract_has_real_paths_and_strict_requests(client):
    doc = client.get("/api/integration/openapi.json").json()
    assert doc["openapi"] == "3.1.1"
    implemented = {route.path for route in client.app.routes}
    assert set(doc["paths"]) <= implemented
    schema = doc["components"]["schemas"]["MachineJob"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(request(client))
    for bad in [{"project": "allowed"}, {**request(client), "board_path": "/secret"}, {**request(client), "operation": "apply"}, {**request(client), "extra": 1}]:
        with pytest.raises(ValidationError):
            validator.validate(bad)
    assert doc["paths"][PREFIX + "/projects"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"


def test_inventory_http_exports_and_exchange(client):
    path = f'/api/projects/allowed/revisions/{client.revision}/inventory'
    inventory = client.get(path)
    assert inventory.status_code == 200, inventory.text
    assert inventory.json()["summary"]["component_count"] == 4
    assert client.get(path + "/export?format=components.csv").status_code == 200
    assert client.get(path + "/export?format=bad").status_code == 400
    machine = client.get(PREFIX + f'/projects/allowed/revisions/{client.revision}/inventory').json()
    assert machine == inventory.json()
    raw = client.get(PREFIX + f'/projects/allowed/revisions/{client.revision}/exchange').content
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert json.loads(archive.read("manifest.json"))["manufacturing_authorized"] is False
    assert client.get(PREFIX + "/projects/denied/releases").status_code == 403
    assert client.get(PREFIX + "/projects/allowed/releases/release-none").status_code == 400


@pytest.mark.parametrize("token", ["short", " x" * 40, "\n" * 40, "a" * 257])
def test_invalid_config_fails_closed(token):
    with pytest.raises(ValueError):
        IntegrationAccess(token)
