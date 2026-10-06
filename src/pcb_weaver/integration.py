"""Opt-in, project-scoped machine API on the trusted local workbench host."""
import hashlib
import hmac
import os
import re

from .jobs import JobRequest
from .storage import Store


class IntegrationError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class IntegrationAccess:
    def __init__(self, token=None, projects=(), write=False):
        if token is not None and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise ValueError("Integration token must be 32-256 URL-safe ASCII characters")
        self.token = token
        self.projects = frozenset(Store.identifier(project) for project in projects)
        self.write = bool(write)
        self.client_id = hashlib.sha256(token.encode()).hexdigest() if token else None

    @classmethod
    def environment(cls):
        write = os.environ.get("PCB_WEAVER_INTEGRATION_WRITE", "0")
        if write not in {"0", "1"}:
            raise ValueError("PCB_WEAVER_INTEGRATION_WRITE must be 0 or 1")
        return cls(os.environ.get("PCB_WEAVER_INTEGRATION_TOKEN") or None,
                   [p.strip() for p in os.environ.get("PCB_WEAVER_INTEGRATION_PROJECTS", "").split(",") if p.strip()], write == "1")

    def authorize(self, headers, project=None, write=False):
        if not self.token:
            raise IntegrationError(503, "Machine integration is disabled")
        values = headers.getlist("authorization")
        expected = "Bearer " + self.token
        if len(values) != 1 or not hmac.compare_digest(values[0].encode(), expected.encode()):
            raise IntegrationError(401, "Valid bearer authorization required")
        if project is not None and project not in self.projects:
            raise IntegrationError(403, "Project is outside integration scope")
        if write and not self.write:
            raise IntegrationError(403, "Integration write access is disabled")

    def capabilities(self):
        return {
            "schema_version": "1.0", "api_version": "v1", "deployment": "local_single_user_bridge",
            "enabled": bool(self.token), "write_enabled": bool(self.token and self.write),
            "transport": "HTTP on loopback only", "authentication": "Bearer plus exact project allowlist",
            "formats": ["json", "components.csv", "nets.csv", "tracks.csv", "vias.csv", "engineering-exchange.zip"],
            "operations": ["projects.read", "revisions.read", "inventory.read", "release.download", "jobs.read", "jobs.submit_existing_revision"],
            "idempotency": "Required Idempotency-Key on machine job submissions; conflicts return 409",
            "certified_platforms": [], "external_data_transfer": False,
            "limitations": ["No enterprise SSO, tenant isolation, TLS termination or high availability",
                            "The local workbench and MCP remain trusted local-user interfaces",
                            "Corporate PLM/ERP/MES field mappings and sandbox acceptance are not performed",
                            "Inventory presence and exchange files do not prove electrical completeness",
                            "Imports require the trusted workbench or MCP; machine API cannot read arbitrary input paths"],
        }


def job_summary(job):
    result = job.get("result") or {}
    return {**{key: job[key] for key in ("id", "project", "created", "updated", "status", "stage")},
            "revision": result.get("revision"), "cancel_requested": job["cancel_requested"],
            "steps": {name: value.get("status") for name, value in result.get("steps", {}).items() if isinstance(value, dict)}}


def authorize_job(queue, access, headers, identifier):
    access.authorize(headers)
    # Check the project in the database before reading full native logs.
    queue.store.identifier(identifier)
    with queue.store.connect() as db:
        row = db.execute("SELECT project FROM jobs WHERE id=?", (identifier,)).fetchone()
    if row is None or row[0] not in access.projects:
        raise IntegrationError(404, "Job not found in integration scope")
    return job_summary(queue.get(identifier))


def submit_job(queue, access, headers, payload):
    access.authorize(headers, write=True)
    request = JobRequest.model_validate(payload)
    access.authorize(headers, request.project, write=True)
    if not request.revision or request.board_path or request.constraints is not None or request.operation in {"constraints", "repair", "clearance", "auto_repair", "reference_repair", "complete"}:
        raise IntegrationError(400, "Machine jobs require an existing revision without input paths or constraint replacement")
    keys = headers.getlist("idempotency-key")
    if len(keys) != 1 or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", keys[0]):
        raise IntegrationError(400, "One valid Idempotency-Key is required")
    return job_summary(queue.submit(request, idempotency_key=keys[0], client_id=access.client_id))


def response_schemas():
    text = {"type": "string"}
    nullable = {"type": ["string", "null"]}
    count = {"type": "integer", "minimum": 0}
    length = {"type": ["number", "null"], "minimum": 0}
    identifier = {"type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"}
    def obj(properties, required=None):
        return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}
    def array(items):
        return {"type": "array", "items": items}
    summary = obj({name: count for name in ("component_count", "pad_count", "net_count", "track_count", "via_count", "layer_count", "package_count")})
    component = obj({"reference": text, "value": text, "footprint": text, "category": text,
                     "category_basis": {"type": "object"}, "manufacturer": nullable, "mpn": nullable, "datasheet": nullable,
                     "layer": text, "x": {"type": "number"}, "y": {"type": "number"}, "rotation": {"type": "number"},
                     "locked": {"type": "boolean"}, "dnp": {"type": "boolean"}, "pads": array({"type": "object"})})
    net = obj({"name": text, "pad_count": count, "track_count": count, "via_count": count,
               "references": array(text), "pads": array({"type": "object"}), "layers": array(text),
               "length_mm": length, "length_status": {"enum": ["complete", "partial"]}, "length_scope": text,
               "min_width_mm": length, "max_width_mm": length, "connectivity_status": {"const": "not_evaluated"}})
    track = obj({"id": text, "kind": {"enum": ["segment", "arc"]}, "net": text, "layer": text,
                 "start": array({"type": "number"}), "end": array({"type": "number"}), "width_mm": length,
                 "length_mm": length, "length_status": {"enum": ["complete", "unknown"]}, "geometry_supported": {"type": "boolean"}})
    via = obj({"id": text, "net": text, "x": {"type": "number"}, "y": {"type": "number"},
               "diameter_mm": length, "drill_mm": length, "layers": array(text)})
    inventory = obj({"schema_version": {"const": "1.0"}, "project": identifier, "revision": identifier,
                     "revision_digest": text, "units": {"const": "mm"}, "summary": summary,
                     "components": array(component), "nets": array(net), "tracks": array(track), "vias": array(via),
                     "layers": array({"type": "object"}), "coverage": {"type": "object"}, "sources": {"type": "object"}})
    return {
        "ProjectSummary": obj({"project": identifier, "updated": text, "revision_count": count, "latest_revision": identifier}),
        "RevisionSummary": obj({"id": identifier, "project": identifier, "parent": nullable, "created": text, "operation": text, "digest": text}),
        "ReleaseSummary": obj({"release_id": identifier, "revision": nullable, "sha256": text}),
        "JobSummary": obj({"id": identifier, "project": identifier, "created": text, "updated": text,
                           "status": {"enum": ["queued", "running", "completed", "blocked", "failed", "cancelled", "interrupted"]},
                           "stage": text, "revision": nullable, "cancel_requested": {"type": "boolean"},
                           "steps": {"type": "object", "additionalProperties": {"type": ["string", "null"]}}}),
        "Inventory": inventory, "Error": obj({"error": text}),
    }


def openapi_document():
    prefix = "/api/integration/v1"
    def ref(name):
        return {"$ref": "#/components/schemas/" + name}
    def response(description="Successful response", binary=False, model="JobSummary"):
        return {"description": description, "content": {"application/zip" if binary else "application/json":
                {"schema": {"type": "string", "format": "binary"} if binary else ref(model)}}}
    def operation(identifier, parameters=(), binary=False):
        return {"operationId": identifier, "security": [{"LocalBearer": []}],
                "parameters": [{"name": name, "in": "path", "required": True, "schema": {"type": "string"}} for name in parameters],
                "responses": {"200": response(binary=binary), **{str(n): response("Request refused", model="Error") for n in (400, 401, 403, 404, 503)}}}
    paths = {}
    for path, identifier, params, binary in [
        ("/projects", "listIntegrationProjects", (), False),
        ("/projects/{project}/revisions", "listIntegrationRevisions", ("project",), False),
        ("/projects/{project}/revisions/{revision}/inventory", "getIntegrationInventory", ("project", "revision"), False),
        ("/projects/{project}/revisions/{revision}/exchange", "downloadEngineeringExchange", ("project", "revision"), True),
        ("/projects/{project}/releases", "listIntegrationReleases", ("project",), False),
        ("/projects/{project}/releases/{release}", "downloadIntegrationRelease", ("project", "release"), True),
        ("/jobs/{job}", "getIntegrationJob", ("job",), False),
    ]:
        action = operation(identifier, params, binary)
        model = {"listIntegrationProjects": "ProjectSummary", "listIntegrationRevisions": "RevisionSummary",
                 "listIntegrationReleases": "ReleaseSummary"}.get(identifier)
        if model:
            action["responses"]["200"]["content"]["application/json"]["schema"] = {"type": "array", "items": ref(model)}
        if identifier == "getIntegrationInventory":
            action["responses"]["200"] = response(model="Inventory")
        paths[prefix + path] = {"get": action}
    submit = operation("submitIntegrationJob")
    submit["parameters"] = [{"name": "Idempotency-Key", "in": "header", "required": True,
                             "schema": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"}}]
    submit["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/MachineJob"}}}}
    submit["responses"]["202"] = submit["responses"].pop("200")
    submit["responses"]["409"] = response("Idempotency key conflicts with another request or runtime", model="Error")
    paths[prefix + "/jobs"] = {"post": submit}
    machine = JobRequest.model_json_schema()
    machine.pop("$defs", None)
    for field in ("repair_nets", "repair_region", "repair_remove_ids", "auto_options", "completion_options"):
        machine["properties"].pop(field, None)
    # Embed the existing request schema, narrowing fields rejected by this bridge.
    for field in ("project", "revision"):
        machine["properties"][field] = {"type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"}
    machine["properties"]["board_path"] = {"type": "null"}
    machine["properties"]["constraints"] = {"type": "null"}
    machine["properties"]["operation"]["enum"] = ["pipeline", "plan", "apply", "route", "verify", "release", "report"]
    machine["required"] = ["project", "revision"]
    machine["allOf"] = [{"if": {"properties": {"operation": {"const": "apply"}}, "required": ["operation"]},
                         "then": {"required": ["plan_id", "candidate_id"], "properties": {
                             "plan_id": {"type": "string", "minLength": 1}, "candidate_id": {"type": "string", "minLength": 1}}}}]
    # Unreferenced definitions are deliberately omitted from the API contract.
    return {"openapi": "3.1.1", "info": {"title": "PCB Weaver Local Integration", "version": "1.0.0",
            "description": "Project-scoped local bridge, not a certified enterprise platform connector. All distances use millimetres."},
            "servers": [{"url": "/"}], "paths": paths,
            "components": {"securitySchemes": {"LocalBearer": {"type": "http", "scheme": "bearer"}},
                           "schemas": {"MachineJob": machine, **response_schemas()}}}
