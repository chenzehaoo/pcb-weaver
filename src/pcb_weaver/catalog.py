"""Read models for the engineering workbench; artifacts retain ledger checks."""
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath

from .storage import canonical, digest


def artifact_path(root, path):
    """Reject aliases before reading or writing workspace evidence."""
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError("Artifact path escapes its evidence directory")
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError("Artifact path contains a link or junction")
        if item == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Artifact path escapes its evidence directory")
    return path


def _read(root, path):
    path = artifact_path(root, path)
    if not path.is_file():
        raise ValueError("Recorded artifact is missing")
    return path.read_bytes()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def projects(engine):
    with engine.store.connect() as db:
        rows = db.execute("SELECT project,MAX(created),COUNT(*) FROM revisions GROUP BY project ORDER BY MAX(created) DESC").fetchall()
    return [{"project": p, "updated": c, "revision_count": n,
             "latest_revision": engine.store.list_revisions(p)[-1]["id"]} for p,c,n in rows]


def verification(engine, project, revision):
    data, folder = engine._verified(project, revision)
    records = [event["payload"] for event in engine.store.history(project)["events"]
               if event["kind"] == "verification_completed" and event["payload"].get("revision") == revision]
    try:
        directory = artifact_path(engine.store.root, folder / "verification")
        files = []
        if directory.exists():
            for run in directory.iterdir():
                artifact_path(engine.store.root, run)
                if run.is_dir() and (run / "result.json").exists():
                    files.append(run / "result.json")
        if not files and not records:
            return {"status": "not_verified"}
        by_id = {r["verification_id"]: r for r in records}
        results = {}
        for path in files:
            raw = _read(engine.store.root, path)
            result = json.loads(raw)
            run_id = result["verification_id"]
            record = by_id.get(run_id)
            if (not record or path.parent.name != run_id or record["sha256"] != _sha(raw)
                    or result["revision"] != revision or result["revision_digest"] != data["digest"]
                    or result["status"] != record["status"] or not isinstance(result["created"], str)):
                raise ValueError("Verification is not bound to this revision and ledger record")
            if not isinstance(result["evidence_hashes"], dict):
                raise ValueError("Invalid verification evidence manifest")
            for name, checksum in result["evidence_hashes"].items():
                relative = Path(name)
                windows = PureWindowsPath(name)
                if not name or relative.is_absolute() or windows.drive or windows.root or ".." in windows.parts or ":" in name:
                    raise ValueError("External verification evidence path")
                artifact = artifact_path(path.parent, path.parent / relative)
                if _sha(_read(engine.store.root, artifact)) != checksum:
                    raise ValueError("Verification evidence bytes changed")
            results[run_id] = result
        if set(results) != set(by_id):
            raise ValueError("Recorded verification is missing")
        latest = results[records[-1]["verification_id"]]
        # The existing report renderer selects by created; reject any disagreement.
        if max(results.values(), key=lambda r: r["created"])["verification_id"] != latest["verification_id"]:
            raise ValueError("Verification chronology disagrees with ledger")
        return latest
    except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
        return {"status": "invalid_evidence", "reasons": [str(error)]}


def drc_findings(engine, project, revision):
    """Read at most 200 revision-bound native findings, with positions in mm.

    ``ok`` means authentic diagnostic data, not an electrical or release pass.
    The total covers unconnected_items and violations, including warnings and
    exclusions. Missing positions stay null; invalid coordinates reject the
    report even when their finding would lie beyond the returned-item limit.
    """
    response = {"status": "not_verified", "verification_id": None, "revision": revision,
                "total": None, "truncated": 0, "items": []}
    try:
        data, folder = engine._verified(project, revision)
        artifact_path(engine.store.root, folder)
        check = verification(engine, project, revision)
        if check["status"] in {"not_verified", "invalid_evidence"}:
            return {**response, "status": check["status"], "reasons": check.get("reasons", [])}
        run_id = engine.store.identifier(check["verification_id"])
        response["verification_id"] = run_id
        if check["revision"] != revision or check["revision_digest"] != data["digest"]:
            raise ValueError("DRC verification is stale")
        drc = check.get("drc")
        if not isinstance(drc, dict) or drc.get("status") != "ok" or drc.get("report_valid") is not True:
            return {**response, "status": "unavailable", "reasons": ["No validated native DRC report"]}
        directory = artifact_path(engine.store.root, folder / "verification" / run_id)
        # Never select a file through a report-supplied path. The service writes
        # this fixed filename, and both recorded hashes must cover these bytes.
        path = artifact_path(directory, directory / "drc.json")
        recorded_path = drc.get("report_path")
        if not isinstance(recorded_path, str) or not recorded_path:
            raise ValueError("DRC report path unavailable")
        windows = PureWindowsPath(recorded_path)
        drive_letter = len(windows.drive) == 2 and windows.drive[0] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" and windows.drive[1] == ":"
        remainder = recorded_path[2:] if drive_letter else recorded_path
        if (".." in windows.parts or ":" in remainder or any(ord(c) < 32 for c in recorded_path)
                or recorded_path.startswith(("\\\\?\\", "\\\\.\\"))):
            raise ValueError("Invalid DRC report path metadata")
        # An immutable ledger can retain its old absolute path after relocation.
        # Validate only its run/filename suffix; never stat, resolve or read it.
        absolute = windows.is_absolute() or PurePosixPath(recorded_path).is_absolute()
        if absolute:
            matches_run = windows.name == "drc.json" and windows.parent.name == run_id
        else:
            matches_run = not windows.drive and not windows.root and windows.parts == ("drc.json",)
        if not matches_run:
            raise ValueError("DRC report path disagrees with the recorded run")
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("DRC report exceeds diagnostic size limit")
        raw = _read(engine.store.root, path)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("DRC report exceeds diagnostic size limit")
        if _sha(raw) != check["evidence_hashes"].get("drc.json") or _sha(raw) != drc.get("report_sha256"):
            raise ValueError("DRC report bytes disagree with recorded evidence")
        board_name = data["board"]
        relative = Path(board_name)
        windows = PureWindowsPath(board_name)
        if (not board_name or relative.is_absolute() or windows.drive or windows.root
                or ".." in windows.parts or ":" in board_name):
            raise ValueError("External DRC source path")
        source = artifact_path(folder / "design", folder / "design" / relative)
        snapshot = artifact_path(directory / "design", directory / "design" / relative)
        expected_source = drc.get("source_sha256")
        if (data["files"].get(board_name) != expected_source
                or _sha(_read(engine.store.root, source)) != expected_source
                or _sha(_read(engine.store.root, snapshot)) != expected_source):
            raise ValueError("DRC source bytes disagree with the revision and recorded input")
        report = json.loads(raw)
        if not isinstance(report, dict) or report.get("$schema") != "https://schemas.kicad.org/drc.v1.json":
            raise ValueError("Unsupported native DRC schema")
        if report.get("source") != relative.name:
            raise ValueError("Native DRC source name disagrees with the board")
        scales = {"mm": 1.0, "in": 25.4, "mils": 0.0254}
        units = report.get("coordinate_units")
        if not isinstance(units, str) or units not in scales:
            raise ValueError("Unknown native DRC coordinate units")
        scale = scales[units]

        def string(value, name, limit=4096):
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"Invalid or oversized DRC {name}")
            return value

        def position(value):
            if value is None:
                return None
            if not isinstance(value, dict) or not {"x", "y"} <= value.keys():
                raise ValueError("Incomplete native DRC coordinates")
            converted = {}
            for axis in ("x", "y"):
                number = value[axis]
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                    raise ValueError("DRC coordinates must be finite numbers")
                converted[axis] = number * scale
                if not math.isfinite(converted[axis]):
                    raise ValueError("DRC coordinate conversion overflow")
            return converted

        found, total = [], 0
        for category in ("unconnected_items", "violations"):
            findings = report.get(category)
            if not isinstance(findings, list):
                raise ValueError(f"Missing native DRC {category} array")
            total += len(findings)
            for index, finding in enumerate(findings):
                if not isinstance(finding, dict):
                    raise ValueError("Invalid native DRC finding")
                kind = string(finding.get("type"), "finding type", 128)
                severity = string(finding.get("severity"), "severity", 32)
                if not kind or severity not in {"error", "warning", "exclusion", "ignore", "info"}:
                    raise ValueError("Unknown native DRC finding type or severity")
                description = string(finding.get("description"), "description")
                entries = finding.get("items")
                if not isinstance(entries, list) or len(entries) > 64:
                    raise ValueError("Invalid or oversized native DRC item list")
                entries_out = []
                for item in entries:
                    if not isinstance(item, dict):
                        raise ValueError("Invalid native DRC item")
                    uuid = item.get("uuid")
                    if uuid is not None:
                        uuid = string(uuid, "UUID", 128)
                    entries_out.append({"description": string(item.get("description"), "item description"),
                                        "uuid": uuid, "pos": position(item.get("pos"))})
                if len(found) < 200:
                    found.append({"id": f"{run_id}:{category}:{index}", "type": kind, "severity": severity,
                                  "description": description, "items": entries_out})
        current, _ = engine._verified(project, revision)
        if current["digest"] != data["digest"] or verification(engine, project, revision) != check:
            raise ValueError("Verification changed while reading DRC findings")
        return {**response, "status": "ok", "total": total, "truncated": total - len(found), "items": found}
    except (ValueError, OSError, KeyError, TypeError, IndexError, OverflowError, RecursionError) as error:
        return {**response, "status": "invalid_evidence", "reasons": [str(error)]}


def plans(engine, project, revision):
    data, folder = engine._verified(project, revision)
    records = {event["payload"]["plan_id"]: event["payload"]["sha256"] for event in engine.store.history(project)["events"]
               if event["kind"] == "layout_planned" and event["payload"]["revision"] == revision}
    result = []
    directory = artifact_path(engine.store.root, folder / "plans")
    for path in sorted(directory.glob("*.json"), reverse=True):
        raw = _read(engine.store.root, path)
        plan = json.loads(raw)
        if records.get(plan["plan_id"]) != _sha(raw) or plan["revision_digest"] != data["digest"] or path.stem != plan["plan_id"]:
            raise ValueError("Plan evidence changed or is stale")
        result.append(plan)
    return result


def releases(engine, project):
    return [{**event["payload"], "created": event["created"]} for event in engine.store.history(project)["events"]
            if event["kind"] == "release_created"]


def archive_path(engine, project, release_id):
    engine.store.identifier(release_id)
    record = next((r for r in releases(engine, project) if r["release_id"] == release_id), None)
    if record is None:
        raise ValueError("Unknown recorded release")
    path = artifact_path(engine.store.root, engine.store.project_dir(project) / "releases" / (release_id + ".zip"))
    if not path.is_file() or digest(path) != record["sha256"]:
        raise ValueError("Release archive changed")
    return path


def archive_bytes(engine, project, release_id):
    engine.store.identifier(release_id)
    record = next((r for r in releases(engine, project) if r["release_id"] == release_id), None)
    if record is None:
        raise ValueError("Unknown recorded release")
    path = engine.store.project_dir(project) / "releases" / (release_id + ".zip")
    raw = _read(engine.store.root, path)
    if _sha(raw) != record["sha256"]:
        raise ValueError("Release archive changed")
    return raw


def generate_report(engine, project, revision):
    from .report import build_report
    with engine.store.lock(project):
        data, folder = engine._verified(project, revision)
        artifact_path(engine.store.root, folder / "review.html")
        check = verification(engine, project, revision)
        if check["status"] == "invalid_evidence":
            raise ValueError("Cannot generate report from invalid verification evidence")
        result = build_report(engine, project, revision)
        if verification(engine, project, revision) != check or result["verification_status"] != check["status"]:
            raise ValueError("Verification changed during report generation")
        raw = _read(engine.store.root, folder / "review.html")
        engine.store.event(project, "report_generated", {"revision": revision, "revision_digest": data["digest"],
                           "sha256": _sha(raw), "verification_digest": _sha(canonical(check).encode())})
        return result


def report_bytes(engine, project, revision):
    data, folder = engine._verified(project, revision)
    records = [e["payload"] for e in engine.store.history(project)["events"]
               if e["kind"] == "report_generated" and e["payload"].get("revision") == revision]
    if not records:
        raise ValueError("Generate a recorded review report first")
    raw = _read(engine.store.root, folder / "review.html")
    record = records[-1]
    check = verification(engine, project, revision)
    if (record["sha256"] != _sha(raw) or record["revision_digest"] != data["digest"]
            or check["status"] == "invalid_evidence" or record["verification_digest"] != _sha(canonical(check).encode())):
        raise ValueError("Report evidence changed or is stale; regenerate the report")
    return raw
