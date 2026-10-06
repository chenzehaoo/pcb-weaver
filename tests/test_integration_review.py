"""Independent local-bridge boundary and contract regressions.

No workers, native tools, production databases, or external corporate services.
"""
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from jsonschema import Draft202012Validator
import pytest
from starlette.testclient import TestClient

from pcb_weaver import catalog
from pcb_weaver.exchange import exchange_bundle, export_inventory
from pcb_weaver.integration import IntegrationAccess
from pcb_weaver.platform import create_app


TOKEN = "review_only_" + "a" * 32
PREFIX = "/api/integration/v1"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples/routing-demo/two-layer.kicad_pcb"


@pytest.fixture
def bridge(tmp_path):
    source = tmp_path / "input" / "board.kicad_pcb"
    source.parent.mkdir()
    source.write_bytes(EXAMPLE.read_bytes())
    app = create_app(tmp_path / "managed", start_worker=False,
                     integration_access=IntegrationAccess(TOKEN, ("allowed",), True))
    engine = app.state.queue.engine
    revisions = {p: engine.import_project(p, str(source))["revision"]["id"] for p in ("allowed", "denied")}
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer " + TOKEN}) as client:
        client.review_revisions = revisions
        yield client
    assert app.state.queue.thread is None


def payload(bridge, project="allowed"):
    return {"project": project, "revision": bridge.review_revisions[project], "operation": "plan"}


def forbidden(*args, **kwargs):
    pytest.fail("Unauthorized request reached a protected read or submission")


@pytest.mark.parametrize("authorization", ["bearer " + TOKEN, "Bearer  " + TOKEN,
                                         "Bearer " + TOKEN + " ", "Bearer " + TOKEN + ",Bearer " + TOKEN])
def test_bearer_is_exact_before_catalog_read(bridge, monkeypatch, authorization):
    monkeypatch.setattr(catalog, "projects", forbidden)
    assert bridge.get(PREFIX + "/projects", headers={"Authorization": authorization}).status_code == 401


@pytest.mark.parametrize("project", ["denied", "Allowed", "allowed-extra"])
def test_project_allowlist_is_exact_before_revision_read(bridge, monkeypatch, project):
    monkeypatch.setattr(bridge.app.state.queue.store, "list_revisions", forbidden)
    assert bridge.get(PREFIX + f"/projects/{project}/revisions").status_code == 403


def test_job_scope_checked_before_full_job_read(bridge, monkeypatch):
    queue = bridge.app.state.queue
    denied = queue.submit(payload(bridge, "denied"))
    monkeypatch.setattr(queue, "get", forbidden)
    assert bridge.get(PREFIX + "/jobs/" + denied["id"]).status_code == 404
    assert bridge.get(PREFIX + "/jobs/job-unknown").status_code == 404


def test_submit_scope_checked_before_revision_or_job_access(bridge, monkeypatch):
    queue = bridge.app.state.queue
    monkeypatch.setattr(queue.engine, "inspect_revision", forbidden)
    monkeypatch.setattr(queue, "submit", forbidden)
    response = bridge.post(PREFIX + "/jobs", json=payload(bridge, "denied"), headers={"Idempotency-Key": "review-1"})
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["C:/private/secret.kicad_pcb", "../../private.kicad_pcb", "//server/share/board.kicad_pcb"])
def test_machine_input_paths_rejected_before_queue_or_filesystem(bridge, monkeypatch, path):
    monkeypatch.setattr(bridge.app.state.queue, "submit", forbidden)
    response = bridge.post(PREFIX + "/jobs", json={"project": "allowed", "board_path": path},
                           headers={"Idempotency-Key": "path-check"})
    assert response.status_code == 400


def test_cross_project_revision_cannot_be_read_under_allowed_project(bridge):
    denied = bridge.review_revisions["denied"]
    for suffix in ("inventory", "exchange"):
        response = bridge.get(PREFIX + f"/projects/allowed/revisions/{denied}/{suffix}")
        assert response.status_code == 400


def test_release_path_identifier_rejected_before_artifact_read(bridge, monkeypatch):
    monkeypatch.setattr(catalog, "_read", forbidden)
    for release in ("C:%5Csecret.zip", "..%5Csecret", "%252e%252e%252fsecret"):
        response = bridge.get(PREFIX + "/projects/allowed/releases/" + release)
        assert response.status_code in {400, 404}


@pytest.mark.parametrize("headers", [{"Origin": "https://external.invalid"}, {"Origin": "null"},
                                     {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"},
                                     {"Host": "localhost.external.invalid"}, {"Host": "user@localhost"}])
def test_valid_bearer_does_not_exempt_machine_jobs_from_csrf_checks(bridge, monkeypatch, headers):
    monkeypatch.setattr(bridge.app.state.queue, "submit", forbidden)
    response = bridge.post(PREFIX + "/jobs", json=payload(bridge), headers={**headers, "Idempotency-Key": "csrf"})
    assert response.status_code == 403


def test_machine_bearer_does_not_exempt_workbench_header(bridge, monkeypatch):
    monkeypatch.setattr(bridge.app.state.queue, "submit", forbidden)
    assert bridge.post("/api/jobs", json=payload(bridge)).status_code == 403
    for path in (PREFIX + "/%2e%2e/%2e%2e/jobs", PREFIX + "/jobs/../jobs"):
        assert bridge.post(path, json=payload(bridge), headers={"Authorization": ""}).status_code in {401, 403, 404}


def test_forwarding_headers_do_not_turn_remote_peer_into_loopback(bridge):
    with TestClient(bridge.app, base_url="http://127.0.0.1", client=("192.0.2.22", 3456)) as remote:
        response = remote.get(PREFIX + "/projects", headers={"Authorization": "Bearer " + TOKEN,
                              "X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1;host=localhost"})
        assert response.status_code == 403


def test_openapi_success_shapes_match_real_json_responses(bridge):
    doc = bridge.get("/api/integration/openapi.json").json()
    revision = bridge.review_revisions["allowed"]
    for path, substitutions in [
        ("/projects", {}), ("/projects/{project}/revisions", {"project": "allowed"}),
        ("/projects/{project}/releases", {"project": "allowed"}),
        ("/projects/{project}/revisions/{revision}/inventory", {"project": "allowed", "revision": revision}),
    ]:
        operation = doc["paths"][PREFIX + path]["get"]
        response = bridge.get(PREFIX + path.format(**substitutions))
        assert response.status_code == 200
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        Draft202012Validator({**schema, "components": doc["components"]}).validate(response.json())
    response = bridge.post(PREFIX + "/jobs", json=payload(bridge), headers={"Idempotency-Key": "contract"})
    assert response.status_code == 202
    schema = doc["paths"][PREFIX + "/jobs"]["post"]["responses"]["202"]["content"]["application/json"]["schema"]
    Draft202012Validator({**schema, "components": doc["components"]}).validate(response.json())
    assert not {"request", "runtime", "events", "board_path"} & response.json().keys()


@pytest.mark.parametrize("case", ["projects", "revisions", "releases", "inventory", "submit", "job", "error"])
def test_review_response_contract_rejects_wrong_types_in_known_fields(bridge, case):
    """Known fields must reject wrong types, while nullable real data stays valid."""
    doc = bridge.get("/api/integration/openapi.json").json()
    revision = bridge.review_revisions["allowed"]
    routes = {
        "projects": "/projects", "revisions": "/projects/{project}/revisions",
        "releases": "/projects/{project}/releases",
        "inventory": "/projects/{project}/revisions/{revision}/inventory",
        "submit": "/jobs", "job": "/jobs/{job}", "error": "/projects",
    }
    identifier = "unused"
    if case in {"submit", "job"}:
        submitted = bridge.post(PREFIX + "/jobs", json=payload(bridge), headers={"Idempotency-Key": "typed-response"})
        assert submitted.status_code == 202
        identifier = submitted.json()["id"]
        # Queued output revision is legitimately null; the contract must allow it.
        assert submitted.json()["revision"] is None
    if case == "releases":
        # Metadata-only temporary fixture, not a manufactured or verified release.
        bridge.app.state.queue.store.event("allowed", "release_created", {
            "release_id": "release-review-fixture", "revision": revision, "sha256": "a" * 64})
    template = PREFIX + routes[case]
    method = "post" if case == "submit" else "get"
    response = submitted if case == "submit" else bridge.get(
        template.format(project="allowed", revision=revision, job=identifier),
        headers={"Authorization": ""} if case == "error" else {})
    assert response.status_code == (401 if case == "error" else 202 if case == "submit" else 200)
    schema = doc["paths"][template][method]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
    # Preserve local component references when the generic schema is replaced.
    validator = Draft202012Validator({**schema, "components": doc["components"]})
    actual = response.json()
    validator.validate(actual)
    changed = deepcopy(actual)
    if case == "projects":
        changed[0]["revision_count"] = "not-an-integer"
    elif case == "revisions":
        assert actual[0]["parent"] is None
        changed[0]["id"] = []
    elif case == "releases":
        changed[0]["sha256"] = []
    elif case == "inventory":
        changed["summary"]["component_count"] = "not-an-integer"
    elif case in {"submit", "job"}:
        changed["cancel_requested"] = "false"
    else:
        changed["error"] = []
    assert not validator.is_valid(changed), f"{case} response schema accepts a wrong type for an existing field"


@pytest.mark.parametrize("change", [{"project": "bad/project"}, {"revision": "../outside"}])
def test_review_openapi_rejects_identifiers_that_runtime_rejects(bridge, change):
    """Preserve the regression for missing identifier constraints."""
    doc = bridge.get("/api/integration/openapi.json").json()
    candidate = {**payload(bridge), **change}
    response = bridge.post(PREFIX + "/jobs", json=candidate, headers={"Idempotency-Key": "bad-id"})
    assert response.status_code == 400
    validator = Draft202012Validator(doc["components"]["schemas"]["MachineJob"])
    assert not validator.is_valid(candidate), "OpenAPI accepts identifier rejected by the actual MachineJob handler"


def test_csv_formula_and_cell_breakouts_stay_literal_in_every_table():
    attack = ' \t=HYPERLINK("https://invalid.example")\r\n,+SUM(A1)'
    inventory = {"project": "p", "revision": "r", "revision_digest": "hash",
                 "components": [{"value": attack}], "nets": [{"name": attack}],
                 "tracks": [{"net": attack}], "vias": [{"net": attack}]}
    for table, field in (("components", "value"), ("nets", "name"), ("tracks", "net"), ("vias", "net")):
        raw, _ = export_inventory(inventory, table + ".csv")
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
        assert len(rows) == 1 and rows[0][field] == "'" + attack


def test_exchange_zip_is_explicitly_nonmanufacturing_and_hash_complete(bridge):
    revision = bridge.review_revisions["allowed"]
    response = bridge.get(PREFIX + f"/projects/allowed/revisions/{revision}/exchange")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "engineering-exchange.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["manufacturing_authorized"] is False
        assert manifest["purpose"] == "engineering_data_exchange"
        assert set(archive.namelist()) == {"inventory.json", "components.csv", "nets.csv", "tracks.csv", "vias.csv", "manifest.json"}
        for name, checksum in manifest["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == checksum
    caps = bridge.get("/api/integration/capabilities").json()
    assert caps["certified_platforms"] == []


@pytest.mark.parametrize("table,required", [
    ("nets", {"length_status", "length_scope"}),
    ("tracks", {"kind", "mid", "length_status", "geometry_supported"}),
])
def test_review_csv_preserves_partial_length_and_unsupported_arc_semantics(bridge, tmp_path, table, required):
    """Both CSV transports must retain inventory's uncertainty and geometry."""
    source = tmp_path / "arc-input" / "board.kicad_pcb"
    source.parent.mkdir()
    text = EXAMPLE.read_text(encoding="utf-8")
    position = text.rfind(")")
    source.write_text(text[:position] + '\n(arc (start 1 1) (mid 2 2) (end 3 1) '
                      '(width 0.2) (layer "F.Cu") (net "ARC_REVIEW"))\n' + text[position:], encoding="utf-8")
    engine = bridge.app.state.queue.engine
    revision = engine.import_project("allowed", str(source))["revision"]["id"]
    url = PREFIX + f"/projects/allowed/revisions/{revision}"
    response = bridge.get(url + "/inventory")
    assert response.status_code == 200, response.text
    inventory = response.json()
    assert inventory["coverage"]["length_status"] == "partial"
    arc = next(track for track in inventory["tracks"] if track["kind"] == "arc")
    assert arc["length_mm"] is None and arc["geometry_supported"] is False
    net = next(net for net in inventory["nets"] if net["name"] == "ARC_REVIEW")
    assert net["length_mm"] == 0 and net["length_status"] == "partial"
    with zipfile.ZipFile(io.BytesIO(exchange_bundle(inventory))) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read(table + ".csv").decode("utf-8-sig"))))
    row = next(row for row in rows if row.get("name", row.get("net")) == "ARC_REVIEW")
    assert required <= row.keys(), f"{table}.csv discarded safety/geometry semantics: {required - row.keys()}"
    direct = bridge.get(f"/api/projects/allowed/revisions/{revision}/inventory/export?format={table}.csv")
    assert direct.status_code == 200
    direct_rows = list(csv.DictReader(io.StringIO(direct.content.decode("utf-8-sig"))))
    direct_row = next(row for row in direct_rows if row.get("name", row.get("net")) == "ARC_REVIEW")
    assert required <= direct_row.keys()
    if table == "nets":
        assert direct_row["length_status"] == "partial" and direct_row["length_scope"] == net["length_scope"]
    else:
        assert direct_row["length_status"] == "unknown" and direct_row["kind"] == "arc"
        assert json.loads(direct_row["mid"]) == arc["mid"]


def test_complete_openapi_local_references_and_operation_contracts(bridge):
    """Offline semantic checks complement the one-off official OAS meta-schema check."""
    doc = bridge.get("/api/integration/openapi.json").json()
    assert doc["openapi"] == "3.1.1"
    assert doc["info"]["title"] and doc["info"]["version"]
    security = doc["components"]["securitySchemes"]
    assert security["LocalBearer"] == {"type": "http", "scheme": "bearer"}
    implemented = {route.path: route.methods for route in bridge.app.routes if hasattr(route, "methods")}
    operation_ids = []

    def inspect(node):
        if isinstance(node, dict):
            if "$ref" in node:
                assert node["$ref"].startswith("#/"), "This local contract must not require remote schema retrieval"
                target = doc
                for part in node["$ref"][2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                assert isinstance(target, (dict, bool))
            for key, value in node.items():
                if key == "schema":
                    Draft202012Validator.check_schema(value)
                inspect(value)
        elif isinstance(node, list):
            for item in node:
                inspect(item)

    inspect(doc)
    for component in doc["components"]["schemas"].values():
        Draft202012Validator.check_schema(component)
    for path, item in doc["paths"].items():
        assert path in implemented
        expected_parameters = set(re.findall(r"\{([^{}]+)\}", path))
        for method, operation in item.items():
            assert method.upper() in implemented[path]
            operation_ids.append(operation["operationId"])
            assert operation["security"] == [{"LocalBearer": []}]
            parameters = operation.get("parameters", [])
            assert len({(p["name"], p["in"]) for p in parameters}) == len(parameters)
            path_parameters = {p["name"] for p in parameters if p["in"] == "path"}
            assert path_parameters == expected_parameters
            assert all(p["required"] is True for p in parameters if p["in"] == "path")
            for code, response in operation["responses"].items():
                assert re.fullmatch(r"[1-5][0-9]{2}", code) and response["description"]
                assert len(response["content"]) == 1
                media, definition = next(iter(response["content"].items()))
                assert media in {"application/json", "application/zip"}
                if media == "application/zip":
                    assert definition["schema"] == {"type": "string", "format": "binary"}
                if int(code) >= 400:
                    validator = Draft202012Validator({**definition["schema"], "components": doc["components"]})
                    validator.validate({"error": "test rejection"})
                    assert not validator.is_valid({"error": False})
    assert len(operation_ids) == len(set(operation_ids))
    submit = doc["paths"][PREFIX + "/jobs"]["post"]
    assert submit["requestBody"]["required"] is True
    assert set(submit["requestBody"]["content"]) == {"application/json"}
    assert {"202", "409"} <= submit["responses"].keys() and "200" not in submit["responses"]
    assert any(p["in"] == "header" and p["name"] == "Idempotency-Key" and p["required"] for p in submit["parameters"])
