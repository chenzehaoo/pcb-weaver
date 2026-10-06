"""Exercise the local bridge over real TCP, not a corporate certification test."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import secrets
import socket
import threading
import time
import uuid
import zipfile

import httpx
import uvicorn

from pcb_weaver.integration import IntegrationAccess
from pcb_weaver.platform import create_app
from pcb_weaver.runtime import load_runtime
from pcb_weaver.storage import write_json


def run(args):
    root = Path(__file__).resolve().parents[1]
    identifier = "integration-" + uuid.uuid4().hex[:8]
    workspace = root / "validation" / identifier
    _, config = load_runtime(workspace, args.config)
    access = IntegrationAccess(secrets.token_urlsafe(36), (identifier,), True)
    app = create_app(workspace, config, integration_access=access)
    queue = app.state.queue
    constraints = json.loads(Path(args.constraints).read_text(encoding="utf-8-sig"))
    imported = queue.engine.import_project(identifier, str(Path(args.board).resolve(strict=True)), constraints)
    revision = imported["revision"]["id"]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]))
    thread.start()
    evidence = {"scope": "Real local HTTP, immutable import and numerical placement; no third-party enterprise or manufacturing signoff",
                "project": identifier, "revision": revision, "transport": "loopback TCP", "status": "running"}
    output = root / "docs/validation" / (identifier + ".json")
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local bridge startup failed")
            time.sleep(0.05)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=90) as client:
            base = "/api/integration/v1"
            headers = {"Authorization": "Bearer " + access.token}
            assert client.get(base + "/projects").status_code == 401
            assert client.get(base + "/projects/not-allowed/revisions", headers=headers).status_code == 403
            assert client.get(base + "/projects", headers={**headers, "Origin": "https://untrusted.invalid"}).status_code == 403
            projects = client.get(base + "/projects", headers=headers).raise_for_status().json()
            assert [p["project"] for p in projects] == [identifier]
            path = f"{base}/projects/{identifier}/revisions/{revision}"
            inventory = client.get(path + "/inventory", headers=headers).raise_for_status().json()
            assert inventory["revision_digest"] == imported["revision"]["digest"]
            evidence["inventory_summary"] = inventory["summary"]
            raw = client.get(path + "/exchange", headers=headers).raise_for_status().content
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                assert manifest["manufacturing_authorized"] is False
                assert set(archive.namelist()) == {"manifest.json", *manifest["files"]}
                assert all(hashlib.sha256(archive.read(name)).hexdigest() == sha for name, sha in manifest["files"].items())
            body = {"project": identifier, "revision": revision, "operation": "plan", "candidate_count": 1}
            headers["Idempotency-Key"] = "tcp-acceptance-plan"
            first = client.post(base + "/jobs", headers=headers, json=body).raise_for_status().json()
            second = client.post(base + "/jobs", headers=headers, json=body).raise_for_status().json()
            assert first["id"] == second["id"]
            assert client.post(base + "/jobs", headers=headers, json={**body, "candidate_count": 2}).status_code == 409
            deadline = time.monotonic() + 180
            while True:
                job = client.get(base + "/jobs/" + first["id"], headers=headers).raise_for_status().json()
                if job["status"] not in {"queued", "running"}:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("Placement job timed out")
                time.sleep(0.2)
            assert job["status"] == "completed", job
            assert len(queue.list(identifier)) == 1
            evidence.update(status="passed", job=job, scope_auth=True, csrf=True, idempotency=True,
                            exchange_integrity=True, exchange_sha256=hashlib.sha256(raw).hexdigest())
    except BaseException as error:
        evidence.update(status="failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        if queue.thread:
            queue.thread.join(timeout=300)
        sock.close()
        if thread.is_alive() or (queue.thread and queue.thread.is_alive()):
            evidence.update(status="failed", shutdown="Worker did not stop")
        write_json(output, evidence)
        print(json.dumps({"status": evidence["status"], "evidence": str(output)}, ensure_ascii=False))
    if evidence["status"] != "passed":
        raise RuntimeError("Integration acceptance failed")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default=str(root / "examples/system-controller/kit-dev-coldfire-xilinx_5213.kicad_pcb"))
    parser.add_argument("--constraints", default=str(root / "profiles/system-controller.json"))
    parser.add_argument("--config", default=str(root / "toolchain.unified.json"))
    run(parser.parse_args())
