"""Local-only engineering workbench HTTP transport; no cloud upload required."""
import argparse
from contextlib import asynccontextmanager
import json
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import catalog
from .jobs import JobQueue, IdempotencyConflict
from .models import Constraints
from .runtime import load_runtime
from .integration import IntegrationAccess, IntegrationError, authorize_job, submit_job, openapi_document
from .exchange import export_inventory, exchange_bundle
from . import __version__


class LocalRequests(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        hosts = request.headers.getlist("host")
        try:
            host = urlsplit("//" + hosts[0]) if len(hosts) == 1 else None
            valid_host = (host is not None and host.hostname in {"127.0.0.1", "localhost", "::1"}
                          and not host.username and not host.password and not host.path and not host.query
                          and not host.fragment and (host.port is None or 0 < host.port <= 65535))
        except ValueError:
            valid_host = False
        if not valid_host:
            return JSONResponse({"error": "Loopback host required"}, status_code=403)
        peer = request.client.host if request.client else ""
        try:
            # TestClient is an in-process ASGI transport, never a TCP peer address.
            local_peer = peer == "testclient" or ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local_peer = False
        if not local_peer:
            return JSONResponse({"error": "Loopback client required"}, status_code=403)
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            return JSONResponse({"error": "Cross-site requests are not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if len(request.headers.getlist("origin")) > 1 or (origin is not None and origin != str(request.base_url).rstrip("/")):
            return JSONResponse({"error": "Cross-origin requests are not allowed"}, status_code=403)
        machine = request.url.path.startswith("/api/integration/v1/")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not machine and request.headers.get("x-pcb-client") != "workbench":
            return JSONResponse({"error": "Explicit workbench request header required"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app(root, config=None, start_worker=True, integration_access=None):
    access = integration_access if integration_access is not None else IntegrationAccess.environment()
    queue = JobQueue(root, config)
    engine = queue.engine
    assets = Path(__file__).parent / "web"
    example_root = Path(__file__).resolve().parents[2] / "examples"

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            queue.start()
        try:
            yield
        finally:
            queue.close()

    async def body(request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ValueError("JSON content type required")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 2_000_000:
                raise ValueError("Request exceeds 2 MB")
        return json.loads(raw)

    async def index(request):
        return FileResponse(assets / "index.html")

    async def health(request):
        return JSONResponse({"status": "ok", "version": __version__, "workspace": str(engine.store.root),
                             "deployment": "local_single_user", "cancellation": "between engineering stages"})

    async def environment(request):
        return JSONResponse(await run_in_threadpool(engine.doctor))

    async def project_list(request):
        return JSONResponse(await run_in_threadpool(catalog.projects, engine))

    async def revision_list(request):
        return JSONResponse(await run_in_threadpool(engine.store.list_revisions, request.path_params["project"]))

    async def inspection(request):
        project, revision = request.path_params["project"], request.path_params["revision"]
        result = await run_in_threadpool(engine.inspect_revision, project, revision)
        result["verification"] = await run_in_threadpool(catalog.verification, engine, project, revision)
        findings = await run_in_threadpool(catalog.drc_findings, engine, project, revision)
        if findings.get("verification_id") != result["verification"].get("verification_id"):
            findings = {"status": "stale", "items": [], "total": 0}
        result["drc_findings"] = findings
        return JSONResponse(result)

    async def plan_list(request):
        return JSONResponse(await run_in_threadpool(catalog.plans, engine, request.path_params["project"], request.path_params["revision"]))

    async def inventory_data(request):
        from .inventory import build_inventory
        return await run_in_threadpool(build_inventory, engine, request.path_params["project"], request.path_params["revision"])

    async def inventory(request):
        return JSONResponse(await inventory_data(request))

    async def repair_diagnosis(request):
        result = await run_in_threadpool(engine.diagnose_repair, request.path_params["project"], request.path_params["revision"])
        return JSONResponse(result)

    async def inventory_export(request):
        format = request.query_params.get("format", "json")
        raw, media = await run_in_threadpool(export_inventory, await inventory_data(request), format)
        name = "inventory.json" if format == "json" else format
        return Response(raw, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def inventory_exchange(request):
        raw = await run_in_threadpool(exchange_bundle, await inventory_data(request))
        return Response(raw, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="engineering-exchange.zip"'})

    async def integration_capabilities(request):
        return JSONResponse(access.capabilities())

    async def integration_schema(request):
        return JSONResponse(openapi_document())

    async def machine_projects(request):
        access.authorize(request.headers)
        return JSONResponse([p for p in await run_in_threadpool(catalog.projects, engine) if p["project"] in access.projects])

    async def machine_revisions(request):
        access.authorize(request.headers, request.path_params["project"])
        rows = await run_in_threadpool(engine.store.list_revisions, request.path_params["project"])
        return JSONResponse([{k: row[k] for k in ("id", "project", "parent", "created", "operation", "digest")} for row in rows])

    async def machine_inventory(request):
        access.authorize(request.headers, request.path_params["project"])
        return await inventory(request)

    async def machine_exchange(request):
        access.authorize(request.headers, request.path_params["project"])
        return await inventory_exchange(request)

    async def machine_releases(request):
        access.authorize(request.headers, request.path_params["project"])
        rows = await run_in_threadpool(catalog.releases, engine, request.path_params["project"])
        return JSONResponse([{k: row.get(k) for k in ("release_id", "revision", "sha256")} for row in rows])

    async def machine_download(request):
        access.authorize(request.headers, request.path_params["project"])
        return await download(request)

    async def machine_submit(request):
        access.authorize(request.headers, write=True)
        return JSONResponse(await run_in_threadpool(submit_job, queue, access, request.headers, await body(request)), status_code=202)

    async def machine_job(request):
        return JSONResponse(await run_in_threadpool(authorize_job, queue, access, request.headers, request.path_params["job"]))

    async def release_list(request):
        return JSONResponse(await run_in_threadpool(catalog.releases, engine, request.path_params["project"]))

    async def download(request):
        release_id = request.path_params["release"]
        raw = await run_in_threadpool(catalog.archive_bytes, engine, request.path_params["project"], release_id)
        return Response(raw, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{release_id}.zip"'})

    async def report(request):
        raw = await run_in_threadpool(catalog.report_bytes, engine, request.path_params["project"], request.path_params["revision"])
        return Response(raw, media_type="text/html", headers={"Content-Security-Policy":
                        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"})

    async def schema(request):
        return JSONResponse(Constraints.model_json_schema())

    async def preset_list(request):
        result = []
        profile_root = example_root.parent / "profiles"
        for directory in sorted(example_root.iterdir()) if example_root.exists() else []:
            boards = list(directory.glob("*.kicad_pcb"))
            intent = directory / "constraints.json"
            execution = profile_root / (directory.name + ".json")
            if execution.is_file():
                intent = execution
            if len(boards) == 1 and intent.is_file():
                result.append({"id": directory.name, "board_path": str(boards[0]),
                               "constraint_source": str(intent),
                               "constraints": json.loads(intent.read_text(encoding="utf-8-sig"))})
        return JSONResponse(result)

    async def jobs(request):
        if request.method == "POST":
            return JSONResponse(await run_in_threadpool(queue.submit, await body(request)), status_code=202)
        result = await run_in_threadpool(queue.list, request.query_params.get("project"))
        for item in result:
            if item["result"]:
                item["result"] = {key:value for key,value in item["result"].items() if key != "steps"}
        return JSONResponse(result)

    async def job(request):
        return JSONResponse(await run_in_threadpool(queue.get, request.path_params["job"]))

    async def cancel(request):
        return JSONResponse(await run_in_threadpool(queue.cancel, request.path_params["job"]))

    async def eco(request):
        return JSONResponse(await run_in_threadpool(engine.compare_revisions, request.path_params["project"],
                                                    request.query_params["before"], request.query_params["after"]))

    async def error(request, exc):
        status = exc.status if isinstance(exc, IntegrationError) else 409 if isinstance(exc, IdempotencyConflict) else 400
        return JSONResponse({"error": str(exc)}, status_code=status)

    prefix = "/api/projects/{project}"
    machine_prefix = "/api/integration/v1/projects/{project}"
    app = Starlette(lifespan=lifespan, routes=[Route("/", index), Route("/api/health", health),
        Route("/api/environment", environment), Route("/api/projects", project_list),
        Route(prefix + "/revisions", revision_list), Route(prefix + "/revisions/{revision}", inspection),
        Route(prefix + "/revisions/{revision}/plans", plan_list), Route(prefix + "/revisions/{revision}/report", report),
        Route(prefix + "/revisions/{revision}/inventory", inventory),
        Route(prefix + "/revisions/{revision}/repair-diagnosis", repair_diagnosis),
        Route(prefix + "/revisions/{revision}/inventory/export", inventory_export),
        Route(prefix + "/revisions/{revision}/inventory/exchange", inventory_exchange),
        Route(prefix + "/releases", release_list), Route(prefix + "/releases/{release}", download),
        Route(prefix + "/eco", eco), Route("/api/schema", schema), Route("/api/presets", preset_list),
        Route("/api/jobs", jobs, methods=["GET", "POST"]), Route("/api/jobs/{job}", job),
        Route("/api/jobs/{job}/cancel", cancel, methods=["POST"]),
        Route("/api/integration/capabilities", integration_capabilities), Route("/api/integration/openapi.json", integration_schema),
        Route("/api/integration/v1/projects", machine_projects),
        Route(machine_prefix + "/revisions", machine_revisions),
        Route(machine_prefix + "/revisions/{revision}/inventory", machine_inventory),
        Route(machine_prefix + "/revisions/{revision}/exchange", machine_exchange),
        Route(machine_prefix + "/releases", machine_releases), Route(machine_prefix + "/releases/{release}", machine_download),
        Route("/api/integration/v1/jobs", machine_submit, methods=["POST"]), Route("/api/integration/v1/jobs/{job}", machine_job),
        Mount("/assets", StaticFiles(directory=assets))],
        exception_handlers={ValueError: error, RuntimeError: error, OSError: error, KeyError: error})
    app.add_middleware(LocalRequests)
    app.state.queue = queue
    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    parser.add_argument("--config")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root, config = load_runtime(args.workspace, args.config)
    uvicorn.run(create_app(root, config), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
