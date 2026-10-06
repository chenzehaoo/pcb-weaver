"""Retry the recorded placement, preserving the failed job and all design rules."""
import json
from pathlib import Path
import time

from pcb_weaver.jobs import JobQueue, JobRequest
from pcb_weaver.runtime import load_runtime
from pcb_weaver.storage import write_json


def main():
    root = Path(__file__).resolve().parents[1]
    workspace, config = load_runtime(root / "data", root / "toolchain.system-retry.json")
    queue = JobQueue(workspace, config)
    evidence = {"project": "system-controller", "source_revision": "r-48eb2d3661aa4575",
                "original_job": "job-5f665ee28c454daa", "manufacturing_authorized": False,
                "strategy": "existing placement; 8 route passes; 1 optimizer pass; unchanged electrical rules"}
    for operation in ("route", "verify"):
        job = queue.submit(JobRequest(project=evidence["project"], operation=operation,
                           revision=evidence.get("revision", evidence["source_revision"]), passes=8))
        identifier = job["id"]
        print(json.dumps({"operation": operation, "job": identifier}), flush=True)
        while True:
            job = queue.get(identifier)
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(10)
        evidence[operation] = job
        evidence["revision"] = (job.get("result") or {}).get("revision", evidence["source_revision"])
        print(json.dumps({"operation": operation, "status": job["status"], "revision": evidence["revision"]}), flush=True)
        if job["status"] != "completed":
            break
    output = root / "docs" / "validation" / ("system-retry-" + identifier + ".json")
    write_json(output, evidence)
    print(json.dumps({"evidence": str(output)}), flush=True)


if __name__ == "__main__":
    main()
