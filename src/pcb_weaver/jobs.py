"""Persistent, single-worker engineering jobs shared by HTTP and MCP clients."""
from pathlib import Path, PureWindowsPath
from contextlib import ExitStack
import hashlib
import json
import math
import re
import threading
from typing import Literal
import uuid

from pydantic import Field, model_validator

from .models import Constraints, StrictModel, ToolchainConfig, AutoRepairOptions, CompletionOptions
from .service import EngineeringService, design_files
from .storage import Store, canonical, digest, now, write_json
from .toolchain import Toolchain
from . import catalog


class JobRequest(StrictModel):
    project: str
    operation: Literal["pipeline", "complete", "plan", "apply", "route", "repair", "auto_repair", "reference_repair", "clearance", "verify", "release", "report", "constraints"] = "pipeline"
    revision: str | None = None
    board_path: str | None = None
    constraints: Constraints | None = None
    candidate_count: int = Field(default=3, ge=1, le=5)
    passes: int = Field(default=10, ge=1, le=100)
    route: bool = True
    release: bool = False
    plan_id: str | None = None
    candidate_id: str | None = None
    repair_nets: list[str] | None = None
    repair_region: list[float] | None = None
    repair_remove_ids: list[str] = Field(default_factory=list)
    auto_options: AutoRepairOptions | None = None
    completion_options: CompletionOptions | None = None
    reference_project: str | None = None
    reference_revision: str | None = None

    @model_validator(mode="after")
    def valid_target(self):
        if self.operation == "reference_repair":
            if not self.reference_project or not self.reference_revision or self.release:
                raise ValueError("Reference repair requires an explicit saved reference and cannot manufacture")
            Store.identifier(self.reference_project)
            Store.identifier(self.reference_revision)
        elif self.reference_project is not None or self.reference_revision is not None:
            raise ValueError("Reference fields require reference_repair operation")
        if self.operation == "complete":
            self.completion_options = self.completion_options or CompletionOptions()
            if self.release or not self.route:
                raise ValueError("Completion requires routing and cannot authorize manufacturing")
        elif self.completion_options is not None:
            raise ValueError("Completion options require complete operation")
        if self.operation == "auto_repair":
            self.auto_options = self.auto_options or AutoRepairOptions()
            if self.release:
                raise ValueError("Automatic repair cannot authorize a manufacturing release")
        elif self.auto_options is not None:
            raise ValueError("Automatic repair options require auto_repair operation")
        Store.identifier(self.project)
        if self.project.lower() == "platform-worker":
            raise ValueError("Project name is reserved for the queue worker lock")
        if self.revision:
            Store.identifier(self.revision)
        if self.operation == "pipeline":
            if bool(self.revision) == bool(self.board_path):
                raise ValueError("Pipeline requires either a saved revision or a board path, not both")
            if self.revision and self.constraints is not None:
                raise ValueError("Create a constraints revision first; pipeline cannot silently replace saved intent")
        elif not self.revision or self.board_path:
            raise ValueError("This operation requires a saved revision and no board path")
        if self.operation == "apply" and not (self.plan_id and self.candidate_id):
            raise ValueError("Apply requires recorded plan and candidate IDs")
        if self.operation == "constraints" and self.constraints is None:
            raise ValueError("Constraint update requires structured constraints")
        if self.operation not in {"pipeline", "constraints"} and self.constraints is not None:
            raise ValueError("This operation cannot replace revision constraints")
        if self.operation in {"repair", "clearance"}:
            if (not self.repair_nets or not 1 <= len(self.repair_nets) <= 8
                    or any(not net.strip() for net in self.repair_nets)
                    or len(set(self.repair_nets)) != len(self.repair_nets)):
                raise ValueError("Repair requires 1-8 unique nonempty nets")
            region = self.repair_region
            if (region is None or len(region) != 4 or not all(math.isfinite(v) for v in region)
                    or region[0] >= region[2] or region[1] >= region[3]):
                raise ValueError("Repair requires four finite region coordinates ordered xmin, ymin, xmax, ymax")
            if (len(self.repair_remove_ids) > 1000
                    or len(set(self.repair_remove_ids)) != len(self.repair_remove_ids)):
                raise ValueError("Repair allows at most 1000 unique removal IDs")
            if self.passes > 10:
                raise ValueError("Repair passes must be between 1 and 10")
            if self.operation == "clearance" and self.repair_remove_ids:
                raise ValueError("Clearance repair never removes copper")
        elif self.repair_nets is not None or self.repair_region is not None or self.repair_remove_ids:
            raise ValueError("Repair fields are only accepted for repair operations")
        return self


class JobCancelled(Exception):
    pass


class IdempotencyConflict(ValueError):
    """A client's submission key already belongs to another request/runtime."""


class JobInterrupted(Exception):
    pass


class JobBlocked(Exception):
    def __init__(self, result):
        self.result = result


def _checksum(data):
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


def _request_payload(request):
    # Preserve existing request hashes and idempotency keys across the schema extension.
    excluded = {"repair_nets", "repair_region", "repair_remove_ids"} if request.operation not in {"repair", "clearance"} else set()
    if request.operation != "auto_repair":
        excluded.add("auto_options")
    if request.operation != "complete":
        excluded.add("completion_options")
    if request.operation != "reference_repair":
        excluded.update({"reference_project","reference_revision"})
    payload = request.model_dump(mode="json", exclude=excluded)
    completion = payload.get("completion_options")
    if completion is not None and completion.get("placement_mode") == "optimize":
        completion.pop("placement_mode")
    if completion is not None and completion.get("repair_cycles") == 1:
        completion.pop("repair_cycles")
    for options in (payload.get("auto_options"), (payload.get("completion_options") or {}).get("repair")):
        if options is not None and options.get("allow_multinet") is False:
            options.pop("allow_multinet")
    if payload.get("constraints") is not None and not payload["constraints"].get("edge_overhang_references"):
        payload["constraints"].pop("edge_overhang_references",None)
    return payload


def _result_summary(result):
    """Bounded navigation/diagnostic fields, never native output or evidence."""
    if result is None:
        return None
    if not isinstance(result, dict):
        return {"error": "Stored result is not a JSON object; inspect job detail"}

    def fields(source, limits):
        return {key: value[:limit] for key, limit in limits.items()
                if isinstance(value := source.get(key), str)}

    summary = fields(result, {"project": 256, "revision": 256, "error": 1024,
                              "status": 64, "reason": 1024})
    blocking = result.get("blocking")
    if isinstance(blocking, dict):
        summary["blocking"] = fields(blocking, {"status": 64, "reason": 1024, "error": 1024})
    runtime = result.get("runtime")
    if isinstance(runtime, dict):
        summary["runtime"] = fields(runtime, {"sha256": 64, "config_sha256": 64})
    return summary


_JOB_COLUMNS = "id,project,created,updated,status,stage,request,result,cancel_requested"
_JOB_LIST_COLUMNS = "id,project,created,updated,status,stage,request,result_summary AS result,cancel_requested"


def _toolchain_config(engine):
    config = json.loads(canonical(engine.toolchain.config))
    if not isinstance(config, dict):
        raise ValueError("Toolchain config must be a JSON object")
    # Validate without filling model defaults: Toolchain owns execution defaults.
    ToolchainConfig.model_validate(config, strict=True)
    return config


def _execution_config(config, cwd):
    result = ToolchainConfig.model_validate(config, strict=True).model_dump(exclude_none=True)
    native = Toolchain(config)
    # Include effective defaults, including Toolchain's dependent routing timeout.
    for key, attr in {"kicad_cli": "cli", "kicad_python": "python", "java": "java",
                      "freerouting_jar": "jar", "wsl_distro": "distro", "timeout_seconds": "timeout",
                      "route_timeout_seconds": "route_timeout", "controlled_neckdown": "controlled_neckdown",
                      "fanout": "fanout"}.items():
        value = getattr(native, attr)
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    for key in ("freerouting_jar", "kicad_cli", "kicad_python", "java"):
        value = result.get(key)
        if not value:
            continue
        if config.get("wsl_distro") and value.startswith("/"):
            continue
        # Bare executable names retain PATH lookup; JARs are always file paths.
        if key == "freerouting_jar" or "/" in value or "\\" in value:
            path = Path(value)
            if not path.is_absolute() and not PureWindowsPath(value).drive:
                result[key] = str((Path(cwd) / path).absolute())
    ToolchainConfig.model_validate(result, strict=True)
    return result


def _jar_snapshot(config):
    jar = config.get("freerouting_jar")
    if not jar:
        return {"status": "not_configured"}
    if (config.get("wsl_distro") and jar.startswith("/")) or jar.startswith(("\\\\", "//")) or "://" in jar:
        return {"status": "not_host_hashed", "path": jar}
    path = Path(jar)
    try:
        if path.is_file():
            return {"status": "host_file_hashed", "path": jar, "sha256": digest(path)}
    except OSError:
        pass
    return {"status": "unavailable_at_submission", "path": jar}


def _verify_jar(runtime):
    jar = runtime["jar"]
    if jar["status"] == "host_file_hashed":
        try:
            unchanged = digest(Path(jar["path"])) == jar["sha256"]
        except OSError:
            unchanged = False
        if not unchanged:
            raise ValueError("Submitted JAR bytes changed or disappeared; resubmit the job")


class JobQueue:
    def __init__(self, root, config=None, engine=None):
        self.engine = engine or EngineeringService(root, config)
        self.store = self.engine.store
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.lifecycle_lock = threading.Lock()
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, project TEXT NOT NULL, created TEXT NOT NULL,
                  updated TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                  request TEXT NOT NULL, result TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status,created);
                CREATE TABLE IF NOT EXISTS job_events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL,
                  created TEXT NOT NULL, stage TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS job_inputs (job TEXT PRIMARY KEY, files TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS job_runtime (
                  job TEXT PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS job_idempotency (
                  client_id TEXT NOT NULL, key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                  job TEXT NOT NULL, PRIMARY KEY(client_id,key));
                CREATE TRIGGER IF NOT EXISTS job_runtime_no_update BEFORE UPDATE ON job_runtime
                  BEGIN SELECT RAISE(ABORT, 'Job runtime is immutable; resubmit the job'); END;
                CREATE TRIGGER IF NOT EXISTS job_runtime_no_delete BEFORE DELETE ON job_runtime
                  BEGIN SELECT RAISE(ABORT, 'Job runtime is immutable; resubmit the job'); END;
            """)
            # Serialize schema inspection and backfill across independently starting workers.
            db.execute("BEGIN IMMEDIATE")
            if "result_summary" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN result_summary TEXT")
            for identifier, raw in db.execute("SELECT id,result FROM jobs WHERE result_summary IS NULL AND result IS NOT NULL"):
                try:
                    summary = _result_summary(json.loads(raw))
                except (ValueError, TypeError):
                    summary = {"error": "Stored result is invalid JSON; inspect job detail"}
                db.execute("UPDATE jobs SET result_summary=? WHERE id=?", (canonical(summary), identifier))

    def submit(self, request, *, idempotency_key=None, client_id=None):
        """Enqueue once per client/key and normalized request/effective config.

        Replays return the original job in any state; they never restart it.
        Callers supply an opaque client ID, not an authentication token.
        """
        if (idempotency_key is None) != (client_id is None):
            raise ValueError("idempotency_key and client_id must be provided together")
        if idempotency_key is not None:
            for name, value in (("idempotency_key", idempotency_key), ("client_id", client_id)):
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
                    raise ValueError(f"{name} must contain 1-128 ASCII letters, digits, or ._:-")
        request = JobRequest.model_validate(request)
        input_files = None
        if request.revision:
            self.engine.inspect_revision(request.project, request.revision)
        if request.board_path:
            if request.board_path.startswith(("\\\\", "//")) or "://" in request.board_path:
                raise ValueError("Input board must be a local filesystem path")
            source = Path(request.board_path).expanduser().resolve()
            if str(source).startswith(("\\\\", "//")) or not source.is_file():
                raise ValueError("Input board does not exist or is not local")
            request = request.model_copy(update={"board_path": str(source)})
            input_files = design_files(source.parent)
        identifier = "job-" + uuid.uuid4().hex[:16]
        created = now()
        config = _toolchain_config(self.engine)
        cwd = str(Path.cwd())
        execution = _execution_config(config, cwd)
        runtime = {"schema_version": 1, "job": identifier, "project": request.project,
                   "request_sha256": _checksum(_request_payload(request)),
                   "config": config, "submitted_config_sha256": _checksum(config),
                   "config_sha256": _checksum(execution), "submission_cwd": cwd,
                   "execution_config": execution, "jar": _jar_snapshot(execution)}
        runtime_hash = _checksum(runtime)
        submission_hash = _checksum({"request": _request_payload(request),
                                     "config_sha256": runtime["config_sha256"]})
        # Filesystem validation and runtime capture above must not hold the write lock.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT request_sha256,job FROM job_idempotency WHERE client_id=? AND key=?",
                                  (client_id, idempotency_key)).fetchone() if idempotency_key is not None else None
            if existing is not None:
                if existing[0] != submission_hash:
                    raise IdempotencyConflict("Idempotency key already belongs to a different request or effective toolchain config")
                identifier = existing[1]
            else:
                db.execute("INSERT INTO jobs(id,project,created,updated,status,stage,request) VALUES(?,?,?,?,?,?,?)",
                           (identifier, request.project, created, created, "queued", "queued", canonical(_request_payload(request))))
                if input_files is not None:
                    db.execute("INSERT INTO job_inputs(job,files) VALUES(?,?)", (identifier, canonical(input_files)))
                db.execute("INSERT INTO job_runtime(job,payload,sha256) VALUES(?,?,?)",
                           (identifier, canonical(runtime), runtime_hash))
                db.execute("INSERT INTO job_events(job,created,stage,payload) VALUES(?,?,?,?)",
                           (identifier, created, "queued", canonical({"state": "runtime_snapshotted",
                            "runtime_sha256": runtime_hash, "config_sha256": runtime["config_sha256"]})))
                if idempotency_key is not None:
                    db.execute("INSERT INTO job_idempotency(client_id,key,request_sha256,job) VALUES(?,?,?,?)",
                               (client_id, idempotency_key, submission_hash, identifier))
        self.wake.set()
        return self.get(identifier)

    @staticmethod
    def _decode(row):
        keys = ("id", "project", "created", "updated", "status", "stage", "request", "result", "cancel_requested")
        result = dict(zip(keys, row))
        result["request"] = json.loads(result["request"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    def get(self, identifier):
        self.store.identifier(identifier)
        with self.store.connect() as db:
            row = db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id=?", (identifier,)).fetchone()
            events = db.execute("SELECT seq,created,stage,payload FROM job_events WHERE job=? ORDER BY seq", (identifier,)).fetchall()
        if not row:
            raise ValueError("Unknown job")
        return {**self._decode(row), "events": [{**json.loads(p), "seq": s, "created": c, "stage": t} for s,c,t,p in events]}

    def list(self, project=None):
        with self.store.connect() as db:
            rows = db.execute(f"SELECT {_JOB_LIST_COLUMNS} FROM jobs " + ("WHERE project=? " if project else "") + "ORDER BY created DESC LIMIT 100",
                              (project,) if project else ()).fetchall()
        return [{**self._decode(row), "summary": True} for row in rows]

    def cancel(self, identifier):
        self.get(identifier)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET cancel_requested=1,updated=?,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END "
                       ",stage=CASE WHEN status='queued' THEN 'cancelled' ELSE stage END "
                       "WHERE id=? AND status IN ('queued','running')", (now(), identifier))
            if db.execute("SELECT changes()").fetchone()[0]:
                db.execute("INSERT INTO job_events(job,created,stage,payload) VALUES(?,?,?,?)",
                           (identifier, now(), "cancel", canonical({"state": "cancel_requested"})))
        return self.get(identifier)

    def _checkpoint(self, identifier):
        self.store.identifier(identifier)
        with self.store.connect() as db:
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Unknown job")
        if row[0]:
            raise JobCancelled()
        if self.stop_event.is_set():
            raise JobInterrupted()

    def _event(self, identifier, stage, payload):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET stage=?,updated=? WHERE id=?", (stage, now(), identifier))
            db.execute("INSERT INTO job_events(job,created,stage,payload) VALUES(?,?,?,?)", (identifier, now(), stage, canonical(payload)))

    def _finish(self, identifier, status, result):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if status == "completed" and db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (identifier,)).fetchone()[0]:
                status = "cancelled"
            db.execute("UPDATE jobs SET status=?,stage=?,updated=?,result=?,result_summary=? WHERE id=? AND status='running'",
                       (status, status, now(), canonical(result), canonical(_result_summary(result)), identifier))
            db.execute("INSERT INTO job_events(job,created,stage,payload) VALUES(?,?,?,?)",
                       (identifier, now(), status, canonical({"state": status})))

    def run_once(self):
        # All entry points, including synchronous MCP calls, share the OS lease.
        with ExitStack() as stack:
            catalog.artifact_path(self.store.root, self.store.project_dir("platform-worker") / ".lock")
            try:
                stack.enter_context(self.store.lock("platform-worker"))
            except RuntimeError:
                return False
            if self.stop_event.is_set():
                return False
            with self.store.connect() as db:
                interrupted = db.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
                for (identifier,) in interrupted:
                    db.execute("UPDATE jobs SET status='interrupted',stage='interrupted',updated=? WHERE id=?", (now(), identifier))
                    db.execute("INSERT INTO job_events(job,created,stage,payload) VALUES(?,?,?,?)",
                               (identifier, now(), "interrupted", canonical({"state": "worker_lost"})))
            return self._run_claimed()

    def _run_claimed(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,request FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return False
            db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (now(), row[0]))
        identifier, raw = row
        current = None
        results = {"revision": current, "steps": {}}

        def persist():
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET result=?,result_summary=?,updated=? WHERE id=? AND status='running'",
                           (canonical(results), canonical(_result_summary(results)), now(), identifier))

        def call(stage, operation):
            self._checkpoint(identifier)
            try:
                _verify_jar(runtime)
            except ValueError as error:
                raise JobBlocked({"status": "blocked", "reason": str(error)}) from error
            self._event(identifier, stage, {"state": "started"})
            path = catalog.artifact_path(self.store.root, self.store.root / "jobs" / identifier / (stage + ".json"))
            result = operation()
            if not isinstance(result, dict):
                raise ValueError("Stage result must be a JSON object")
            canonical(result)
            results["steps"][stage] = result
            use(result)
            write_json(path, result)
            persist()
            evidence = {"state": "finished", "status": result.get("status"), "evidence": str(path), "sha256": digest(path)}
            self.store.event(request.project, "job_stage_completed", {"job": identifier, "stage": stage,
                             "revision": current, "runtime_sha256": results["runtime"]["sha256"],
                             "config_sha256": runtime["config_sha256"], **evidence})
            self._event(identifier, stage, evidence)
            expected = {"import": "imported", "plan": "ok", "apply": "created", "route": "routed_unverified",
                        "verify": "passed", "release": "released", "constraints": "created", "report": "generated", "auto_repair":"repaired", "reference_repair":"repaired", "complete":"completed"}
            accepted = {"improved", "repaired"} if stage in {"repair", "clearance"} else {expected[stage]}
            if result.get("status") not in accepted:
                raise JobBlocked(result)
            return result

        def use(result):
            nonlocal current
            revision = result.get("revision")
            if isinstance(revision, dict):
                current = revision["id"]
                results["revision"] = current
            elif (request.operation in {"repair", "clearance"} and result.get("status") in {"improved", "repaired"}
                  and isinstance(revision, str)):
                current = revision
                results["revision"] = current
            return result

        def auto_progress(value, stage="auto_repair"):
            nonlocal current
            current = value["revision"]
            results["revision"] = current
            results["steps"][stage] = value
            persist()
            self._event(identifier,stage,{"state":value["stage"],"revision":current,
                        "attempts":len(value["attempts"]),"before_unconnected":value.get("before_unconnected"),
                        "after_unconnected":value.get("after_unconnected")})

        def completion_progress(value):
            nonlocal current
            current = value["revision"]
            results["revision"] = current
            results["steps"]["complete"] = value
            persist()
            self._event(identifier,"complete",{"state":value["stage"],"revision":current,
                        "attempts":len(value["attempts"]),"unconnected":value.get("best",{}).get("unconnected")})

        try:
            request = JobRequest.model_validate_json(raw)
            current = request.revision
            results.update(project=request.project, revision=current)
            persist()
            runtime, runtime_hash = self._load_runtime(identifier, request)
            results["runtime"] = {**runtime, "sha256": runtime_hash}
            persist()
            try:
                _verify_jar(runtime)
                own_config = _toolchain_config(self.engine)
                matching = _execution_config(own_config, str(Path.cwd())) == runtime["execution_config"]
                e = self.engine if matching else EngineeringService(self.store.root, runtime["execution_config"])
            except (ValueError, OSError, TypeError, AttributeError) as error:
                raise JobBlocked({"status": "blocked", "reason": "Recorded runtime cannot execute; resubmit the job: " + str(error)}) from error
            p = request.project
            self.store.event(p, "job_runtime_selected", {"job": identifier, "runtime_sha256": runtime_hash,
                             "config_sha256": runtime["config_sha256"], "reused_worker_engine": matching})
            if request.operation == "pipeline":
                if request.board_path:
                    with self.store.connect() as db:
                        source_record = db.execute("SELECT files FROM job_inputs WHERE job=?", (identifier,)).fetchone()
                    if source_record is None or json.loads(source_record[0]) != design_files(Path(request.board_path).parent):
                        raise JobBlocked({"status": "blocked", "reason": "Submitted input files changed or lack a recorded manifest; resubmit the job"})
                    use(call("import", lambda: e.import_project(p, request.board_path, request.constraints.model_dump() if request.constraints else None)))
                plan = call("plan", lambda: e.plan_layout(p, current, request.candidate_count))
                feasible = [c for c in plan["candidates"] if c["feasible"] is True
                            and isinstance(c["metrics"]["weighted_hpwl_mm"], (int, float))
                            and math.isfinite(c["metrics"]["weighted_hpwl_mm"])]
                if not feasible:
                    raise JobBlocked({"status": "blocked", "reason": "No feasible candidate; inspect constraints and candidate findings"})
                if plan.get("algorithm") == "legalize":
                    feasible = [c for c in feasible if isinstance(c["metrics"].get("squared_displacement_mm2"), (int, float))
                                and math.isfinite(c["metrics"]["squared_displacement_mm2"])
                                and c["metrics"]["squared_displacement_mm2"] >= 0]
                    if not feasible:
                        raise JobBlocked({"status": "blocked", "reason": "Legalization candidates lack finite displacement evidence"})
                    candidate = min(feasible, key=lambda c: (c["metrics"]["squared_displacement_mm2"], c["metrics"]["weighted_hpwl_mm"]))
                else:
                    candidate = min(feasible, key=lambda c: c["metrics"]["weighted_hpwl_mm"])
                use(call("apply", lambda: e.apply_layout(p, current, plan["plan_id"], candidate["id"])))
                if request.route:
                    use(call("route", lambda: e.route_revision(p, current, request.passes)))
                    call("verify", lambda: e.verify_revision(p, current))
                if request.release:
                    call("release", lambda: e.build_release(p, current))
            elif request.operation != "report":
                operations = {
                    "plan": lambda: e.plan_layout(p, current, request.candidate_count),
                    "apply": lambda: e.apply_layout(p, current, request.plan_id, request.candidate_id),
                    "route": lambda: e.route_revision(p, current, request.passes),
                    "repair": lambda: e.repair_revision(p, current, request.repair_nets, request.repair_region,
                                                        remove_ids=request.repair_remove_ids, passes=request.passes),
                    "clearance": lambda: e.repair_clearance(p, current, request.repair_nets, request.repair_region),
                    "auto_repair": lambda: e.auto_repair_revision(p,current,request.auto_options,
                        checkpoint=lambda:self._checkpoint(identifier),progress=auto_progress),
                    "reference_repair": lambda: e.reference_repair_revision(p,current,request.reference_project,request.reference_revision,
                        checkpoint=lambda:self._checkpoint(identifier),progress=lambda value:auto_progress(value,"reference_repair")),
                    "complete": lambda: e.complete_revision(p,current,request.completion_options,
                        checkpoint=lambda:self._checkpoint(identifier),progress=completion_progress),
                    "verify": lambda: e.verify_revision(p, current),
                    "release": lambda: e.build_release(p, current),
                    "constraints": lambda: e.update_constraints(p, current, request.constraints.model_dump()),
                }
                use(call(request.operation, operations[request.operation]))
            self._checkpoint(identifier)
            if current:
                call("report", lambda: catalog.generate_report(e, p, current))
            self._checkpoint(identifier)
            self._finish(identifier, "completed", results)
        except JobCancelled:
            self._event(identifier, "cancelled", {"state": "cancelled_at_stage_boundary"})
            self._finish(identifier, "cancelled", results)
        except JobInterrupted:
            self._finish(identifier, "interrupted", results)
        except JobBlocked as error:
            results["blocking"] = error.result
            self._finish(identifier, "blocked", results)
        except Exception as error:
            results["error"] = type(error).__name__ + ": " + str(error)
            self._event(identifier, "failed", {"state": "failed", "reason": results["error"]})
            self._finish(identifier, "failed", results)
        return True

    def _load_runtime(self, identifier, request):
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT payload,sha256 FROM job_runtime WHERE job=?", (identifier,)).fetchone()
                event = db.execute("SELECT payload FROM job_events WHERE job=? AND stage='queued' ORDER BY seq LIMIT 1",
                                   (identifier,)).fetchone()
            if row is None:
                raise ValueError("Legacy queued job has no runtime snapshot")
            runtime = json.loads(row[0])
            if canonical(runtime) != row[0] or _checksum(runtime) != row[1]:
                raise ValueError("Runtime snapshot hash/canonical JSON mismatch")
            if (type(runtime["schema_version"]) is not int or runtime["schema_version"] != 1
                    or runtime["job"] != identifier or runtime["project"] != request.project
                    or runtime["request_sha256"] != _checksum(_request_payload(request))):
                raise ValueError("Runtime snapshot belongs to a different job/request")
            config = runtime["config"]
            if not isinstance(config, dict):
                raise ValueError("Runtime config is not an object")
            ToolchainConfig.model_validate(config, strict=True)
            if runtime["submitted_config_sha256"] != _checksum(config):
                raise ValueError("Submitted config digest mismatch")
            if not isinstance(runtime["submission_cwd"], str) or not Path(runtime["submission_cwd"]).is_absolute():
                raise ValueError("Invalid submission directory")
            if _execution_config(config, runtime["submission_cwd"]) != runtime["execution_config"]:
                raise ValueError("Recorded execution paths disagree with submitted config")
            if runtime["config_sha256"] != _checksum(runtime["execution_config"]):
                raise ValueError("Effective config digest mismatch")
            proof = json.loads(event[0]) if event else {}
            if proof.get("runtime_sha256") != row[1] or proof.get("config_sha256") != runtime["config_sha256"]:
                raise ValueError("Runtime snapshot disagrees with its submission event")
            jar = runtime["jar"]
            if not isinstance(jar, dict) or jar.get("status") not in {
                    "not_configured", "not_host_hashed", "unavailable_at_submission", "host_file_hashed"}:
                raise ValueError("Invalid JAR snapshot")
            if jar["status"] != "not_configured" and jar.get("path") != runtime["execution_config"].get("freerouting_jar"):
                raise ValueError("JAR snapshot belongs to a different configured path")
            if (jar["status"] == "not_configured") != (not runtime["execution_config"].get("freerouting_jar")):
                raise ValueError("JAR snapshot availability disagrees with config")
            if jar["status"] == "host_file_hashed" and (not isinstance(jar.get("sha256"), str) or len(jar["sha256"]) != 64):
                raise ValueError("Invalid JAR digest")
            return runtime, row[1]
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
            raise JobBlocked({"status": "blocked", "reason": "Runtime snapshot missing or invalid; resubmit the job: " + str(error)}) from error

    def _loop(self):
        while not self.stop_event.is_set():
            if not self.run_once():
                self.wake.wait(1)
                self.wake.clear()

    def start(self):
        with self.lifecycle_lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._loop, name="pcb-engineering-worker", daemon=True)
            self.thread.start()

    def close(self):
        with self.lifecycle_lock:
            self.stop_event.set()
            self.wake.set()
            thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
