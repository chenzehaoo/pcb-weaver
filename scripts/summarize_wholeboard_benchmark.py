"""Summarize frozen whole-board evidence; never execute or independently verify native tools.

Example: python scripts/summarize_wholeboard_benchmark.py --evidence a.json b.json
--evidence c.json --output new-phase-directory

Only local JSON is read. Paths in completion events resolve relative to their evidence
JSON when not absolute. Original JSON snapshots and SHA256s are retained in matrix.json.
Exit codes: 0 = phase passed, 1 = evidence does not qualify, 2 = invocation/output error.
"""

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import statistics
import sys


SCOPE = ("Evidence summary only; not independent native execution verification. "
         "No native tools were called. Synthetic unit tests are not native acceptance. "
         "Existing pending acceptance claims are not updated by this report.")
PATH_KEYS = {"java", "kicad_cli", "kicad_python", "freerouting_jar"}
TERMINAL_JOBS = ("completed", "failed", "blocked", "cancelled", "interrupted")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def checksum(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def is_sha(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def obj(value):
    return value if isinstance(value, dict) else {}


def text_id(value):
    return isinstance(value, str) and bool(value.strip())


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def zero(value):
    return number(value) and value == 0


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Nonfinite JSON number: " + value)


def local_path(value, base=None):
    raw = str(value)
    if raw.startswith(("\\\\", "//")) or "://" in raw:
        raise ValueError("Evidence must be a local filesystem path")
    path = Path(raw).expanduser()
    if PureWindowsPath(raw).is_absolute() and not path.is_absolute():
        raise ValueError("Recorded Windows evidence path is unavailable on this host")
    if not path.is_absolute() and base is not None:
        path = base / path
    path = path.resolve(strict=True)
    if str(path).startswith(("\\\\", "//")) or not path.is_file():
        raise ValueError("Evidence must be a local file")
    return path


def read_evidence(value, observed, base=None):
    path = local_path(value, base)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if path in observed and observed[path] != sha:
        raise ValueError("Local evidence changed during summary: " + str(path))
    observed[path] = sha
    data = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(data, dict):
        raise ValueError("Evidence JSON must be an object")
    # Reject overflowing JSON numbers such as 1e999 as well as literal NaN.
    canonical(data)
    return path, sha, data


def _portable_path(value):
    value = value.replace("\\", "/")
    path_type = PureWindowsPath if PureWindowsPath(value).drive else PurePosixPath
    return path_type(value)


def semantic_config(config, submission_cwd=None):
    """Normalize known executable locations, retaining flags and relative tool identity.

    The frozen harness places bundled tools under ROOT/tools. Other absolute paths
    remain significant unless they are beneath the recorded submission directory.
    In particular, comparing basenames alone would hide a different Java toolchain.
    """
    result = {k: v for k, v in config.items() if v is not None}
    roots = []
    jar = result.get("freerouting_jar")
    if isinstance(jar, str):
        path = _portable_path(jar)
        if path.is_absolute() and "tools" in path.parts:
            roots.append(type(path)(*path.parts[:path.parts.index("tools")]))
    if text_id(submission_cwd):
        roots.append(_portable_path(submission_cwd))
    for key in PATH_KEYS:
        value = result.get(key)
        if not isinstance(value, str):
            continue
        path = _portable_path(value)
        if ".." in path.parts:
            raise ValueError("Ambiguous parent-relative tool path: " + key)
        normalized = path.as_posix()
        if path.is_absolute():
            for root in roots:
                try:
                    normalized = "<root>/" + path.relative_to(root).as_posix()
                    break
                except ValueError:
                    continue
        elif "/" in value.replace("\\", "/") or key == "freerouting_jar":
            normalized = "<root>/" + normalized
        result[key] = normalized
    # Frozen Toolchain's effective defaults, without importing the live package.
    result.setdefault("kicad_cli", "kicad-cli")
    result.setdefault("java", "java")
    if result.get("wsl_distro"):
        result.setdefault("kicad_python", "python3")
    result.setdefault("route_timeout_seconds", result.get("timeout_seconds", 1800))
    result.setdefault("timeout_seconds", 300)
    result.setdefault("controlled_neckdown", False)
    result.setdefault("fanout", False)
    result.setdefault("router_optimizer_max_passes", 100)
    return result


def inspect_run(entry, document, source_path, observed):
    errors = []

    def require(condition, reason):
        if not condition:
            errors.append(reason)

    job = obj(entry.get("job"))
    result = obj(job.get("result"))
    flow = obj(obj(result.get("steps")).get("complete"))
    metrics = obj(entry.get("metrics"))
    check = obj(entry.get("independent_verification"))
    drc, erc = obj(check.get("drc")), obj(check.get("erc"))
    runtime = obj(result.get("runtime"))
    events = job.get("events") if isinstance(job.get("events"), list) else []
    events = [event for event in events if isinstance(event, dict)]
    row = {"reported_status": entry.get("status"), "job_status": job.get("status"),
           "project": entry.get("project"), "job_id": entry.get("job_id"),
           "input_revision": entry.get("input_revision"), "revision": entry.get("revision"),
           "repetition": entry.get("repetition"), "errors": errors,
           "terminal": entry.get("status") in ("passed", "failed")
                       and job.get("status") in TERMINAL_JOBS,
           "metrics": metrics, "elapsed_seconds": flow.get("elapsed_seconds"),
           "routed_length_mm": metrics.get("routed_length_mm"), "vias": metrics.get("vias"),
           "native_drc_errors": drc.get("errors"), "native_erc_errors": erc.get("errors"),
           "missing_connections": drc.get("unconnected"),
           "new_findings": entry.get("new_findings"), "error": entry.get("error"),
           "completion_reason": flow.get("reason"), "local_run_evidence": [],
           "effective_config": None, "jar_sha256": obj(runtime.get("jar")).get("sha256")}
    require(row["terminal"], "Run and job must both be terminal")
    require(entry.get("status") == "passed", "Harness run did not pass")
    require(job.get("status") == "completed", "Job did not complete")
    require(flow.get("status") == "completed", "Completion flow did not complete")
    require(not entry.get("error"), "Run contains an error")
    require(entry.get("submitter_disconnected") is True, "Submitter disconnection not recorded as true")
    for key in ("project", "job_id", "input_revision", "revision"):
        require(text_id(entry.get(key)), "Missing run identity: " + key)
    require(job.get("id") == entry.get("job_id") and job.get("project") == entry.get("project"),
            "Job identity differs from run")
    require(flow.get("project") == entry.get("project")
            and flow.get("source_revision") == entry.get("input_revision")
            and flow.get("revision") == entry.get("revision"), "Completion revision identity mismatch")
    require(flow.get("options") == document.get("options"), "Completion options differ from harness")
    request = obj(job.get("request"))
    require(request.get("operation") == "complete"
            and request.get("project") == entry.get("project")
            and request.get("revision") == entry.get("input_revision")
            and request.get("completion_options") == document.get("options"), "Job request mismatch")
    require(flow.get("best") == metrics and bool(metrics), "Run metrics differ from completion best")
    require(check.get("status") == "passed", "Final native verification did not pass")
    require(text_id(check.get("revision")) and check.get("revision") == entry.get("revision"),
            "Final native verification revision mismatch")
    require(is_sha(drc.get("source_sha256")) and drc.get("source_sha256") == entry.get("board_sha256"),
            "Final native DRC board SHA mismatch")
    require(drc.get("status") == "ok" and erc.get("status") == "ok", "Final native reports unavailable")
    for name, value in (("native DRC", drc.get("errors")), ("native ERC", erc.get("errors")),
                        ("missing connections", drc.get("unconnected")),
                        ("new DRC findings", obj(entry.get("new_findings")).get("drc")),
                        ("new ERC findings", obj(entry.get("new_findings")).get("erc"))):
        require(zero(value), "Nonzero or missing " + name)
    for key in ("moved_components", "drc_errors", "erc_errors", "unconnected"):
        require(zero(metrics.get(key)), "Nonzero or missing metrics." + key)
    require(metrics.get("revision") == entry.get("revision"), "Metrics revision mismatch")
    require(is_sha(entry.get("board_sha256"))
            and metrics.get("board_sha256") == entry.get("board_sha256"), "Final board SHA mismatch or missing")
    require(type(metrics.get("tracks")) is int and metrics["tracks"] > 0, "No final routing tracks")
    for key in ("elapsed_seconds", "routed_length_mm", "vias"):
        require(number(row[key]), "Invalid or missing " + key)
    require(type(row["vias"]) is int, "Via count must be an integer")
    require(flow.get("manufacturing_authorized") is False, "Unexpected manufacturing authorization")
    require(runtime.get("job") == entry.get("job_id")
            and runtime.get("project") == entry.get("project"), "Runtime identity mismatch")
    require(is_sha(runtime.get("sha256")) and runtime.get("sha256") == checksum(
        {k: v for k, v in runtime.items() if k != "sha256"}), "Runtime snapshot SHA mismatch or missing")
    require(runtime.get("request_sha256") == checksum(request), "Runtime request SHA mismatch")
    for key, hash_key in (("config", "submitted_config_sha256"), ("execution_config", "config_sha256")):
        require(isinstance(runtime.get(key), dict) and bool(runtime[key])
                and runtime.get(hash_key) == checksum(runtime[key]), "Runtime " + key + " SHA mismatch or missing")
    require(any(e.get("state") == "runtime_snapshotted"
                and e.get("runtime_sha256") == runtime.get("sha256")
                and e.get("config_sha256") == runtime.get("config_sha256") for e in events),
            "Runtime snapshot event missing or inconsistent")
    jar = obj(runtime.get("jar"))
    require(jar.get("status") == "host_file_hashed" and is_sha(jar.get("sha256")),
            "Job runtime JAR SHA missing")
    require(text_id(jar.get("path"))
            and jar.get("path") == obj(runtime.get("execution_config")).get("freerouting_jar"),
            "Runtime JAR path mismatch")
    try:
        row["effective_config"] = semantic_config(obj(runtime.get("execution_config")), runtime.get("submission_cwd"))
        require(row["effective_config"] == semantic_config(obj(runtime.get("config")), runtime.get("submission_cwd"))
                == semantic_config(obj(document.get("config")), runtime.get("submission_cwd")),
                "Effective job toolchain differs from submitted config")
    except ValueError as error:
        errors.append(str(error))
    proofs = [e for e in events if e.get("stage") == "complete" and e.get("state") == "finished"
              and ("evidence" in e or "sha256" in e)]
    require(len(proofs) == 1, "Expected exactly one immutable local completion evidence event")
    for proof in proofs:
        record = {"path": proof.get("evidence"), "expected_sha256": proof.get("sha256"), "verified": False}
        row["local_run_evidence"].append(record)
        try:
            if not text_id(proof.get("evidence")):
                raise ValueError("Missing local completion evidence path")
            path, sha, payload = read_evidence(proof["evidence"], observed, source_path.parent)
            record.update(path=str(path), sha256=sha)
            record["verified"] = (is_sha(proof.get("sha256")) and sha == proof["sha256"]
                                  and payload == flow and proof.get("status") == flow.get("status"))
            require(record["verified"], "Local completion evidence SHA/content/status mismatch")
        except (OSError, ValueError) as error:
            errors.append("Local completion evidence unavailable: " + str(error))
    return row


def statistics_for(rows):
    result = {}
    for field in ("elapsed_seconds", "routed_length_mm", "vias"):
        values = [row[field] for row in rows if number(row.get(field))]
        result[field] = {"count": len(values), "missing": len(rows) - len(values),
                         "min": min(values) if values else None, "max": max(values) if values else None,
                         "mean": statistics.mean(values) if values else None,
                         "median": statistics.median(values) if values else None}
    return result


def summarize(evidence_paths, required_repetitions=2, required_circuits=3):
    """Return an auditable phase matrix, retaining every supplied file and run."""
    if type(required_repetitions) is not int or required_repetitions < 1:
        raise ValueError("required repetitions must be positive")
    if type(required_circuits) is not int or required_circuits < 3:
        raise ValueError("required circuits must be at least 3 independent source projects")
    observed, sources, rows, groups, phase_errors = {}, [], [], {}, []
    for value in evidence_paths:
        source = {"path": str(value), "errors": [], "snapshot": None, "sha256": None}
        sources.append(source)
        index = len(sources) - 1
        try:
            path, sha, document = read_evidence(value, observed)
            source.update(path=str(path), sha256=sha, snapshot=document)
        except (OSError, ValueError) as error:
            source["errors"].append(str(error))
            rows.append({"source_index": index, "reported_status": "unreadable", "terminal": False,
                         "errors": source["errors"].copy()})
            continue
        fixture = obj(document.get("fixture"))
        origin, board_hash = fixture.get("source_project"), fixture.get("input_board_sha256")
        valid_fixture = text_id(origin) and is_sha(board_hash)
        key = (origin, board_hash) if valid_fixture else ("invalid evidence " + str(index), "")
        group = groups.setdefault(key, {"source_project": key[0], "input_board_sha256": key[1],
                                        "source_indices": [], "row_indices": [], "errors": []})
        group["source_indices"].append(index)
        source["circuit"] = list(key)
        problems = source["errors"]
        if not valid_fixture:
            problems.append("Missing frozen source_project/input_board_sha256")
        files = obj(fixture.get("files"))
        if (not files or not all(is_sha(v) for v in files.values())
                or files.get(str(fixture.get("board"))) != board_hash
                or not is_sha(files.get("constraints.json"))
                or fixture.get("placement_preserved") is not True or fixture.get("rules_preserved") is not True):
            problems.append("Frozen fixture hashes or preservation evidence missing")
        if document.get("status") != "passed":
            problems.append("Evidence document did not pass: " + str(document.get("status")))
        if document.get("error"):
            problems.append("Evidence error: " + str(document["error"]))
        if obj(document.get("options")).get("placement_mode") != "preserve":
            problems.append("Options must preserve placement")
        implementation = obj(document.get("implementation_sha256"))
        if not implementation or not all(is_sha(v) for v in implementation.values()):
            problems.append("Frozen implementation SHA manifest missing or invalid")
        if not is_sha(document.get("harness_sha256")) or not is_sha(document.get("config_sha256")):
            problems.append("Harness/config SHA missing or invalid")
        if not isinstance(document.get("config"), dict) or not document["config"]:
            problems.append("Toolchain config missing")
        entries = document.get("runs")
        if not isinstance(entries, list):
            problems.append("Runs must be an array")
            entries = []
        expected = document.get("expected_runs")
        if type(expected) is not int or expected < 1:
            problems.append("Invalid expected_runs")
            expected = len(entries)
        if len(entries) != expected:
            problems.append("Requested run count does not match submitted run count")
        repetitions = [obj(e).get("repetition") for e in entries]
        if (any(type(r) is not int for r in repetitions)
                or sorted(r for r in repetitions if type(r) is int) != list(range(1, expected + 1))):
            problems.append("Requested repetitions are missing, duplicated, or out of range")
        for run_index, entry in enumerate(entries):
            row = inspect_run(obj(entry), document, path, observed)
            row.update(source_index=index, run_index=run_index, circuit=list(key))
            rows.append(row)
            group["row_indices"].append(len(rows) - 1)
        for missing in range(max(0, expected - len(entries))):
            rows.append({"source_index": index, "circuit": list(key), "reported_status": "missing",
                         "terminal": False, "errors": ["Requested run not recorded"]})
            group["row_indices"].append(len(rows) - 1)

    # Any repeated identity excludes every occurrence, independent of input order.
    for field in ("job_id", "project", "revision"):
        identities = defaultdict(set)
        for index, row in enumerate(rows):
            values = [row.get(field)]
            if field == "revision":
                values.append(row.get("input_revision"))
            for value in values:
                if text_id(value):
                    identities[value].add(index)
        for value, indices in identities.items():
            if len(indices) > 1:
                for index in indices:
                    rows[index]["errors"].append("Duplicate " + field + ": " + value)

    documents = [s["snapshot"] for s in sources if s["snapshot"] is not None]
    for field in ("implementation_sha256", "harness_sha256"):
        if len({canonical(d.get(field)) for d in documents}) > 1:
            phase_errors.append("Inconsistent frozen " + field)
    run_consistency_errors = phase_errors.copy()
    origins = {g["source_project"] for g in groups.values() if g["input_board_sha256"]}
    hashes = {g["input_board_sha256"] for g in groups.values() if g["input_board_sha256"]}
    if len(origins) < required_circuits or len(hashes) < required_circuits:
        phase_errors.append("Insufficient independent source projects/frozen inputs")
    if len(origins) != len(groups) or len(hashes) != len(groups):
        phase_errors.append("Frozen input/source project aliases or changed input for one source")
    if not sources:
        phase_errors.append("No evidence submitted")

    for group in groups.values():
        docs = [sources[i]["snapshot"] for i in group["source_indices"]]
        members = [rows[i] for i in group["row_indices"]]
        variants = {}
        for doc in docs:
            try:
                config = semantic_config(obj(doc.get("config")))
            except ValueError as error:
                group["errors"].append(str(error))
                config = doc.get("config")
            setting = {"options": doc.get("options"), "config": config}
            variants[canonical(setting)] = setting
        group["configuration_variants"] = list(variants.values())
        if len(variants) != 1:
            group["errors"].append("Inconsistent options/toolchain within frozen circuit")
        if len({canonical(obj(d.get("fixture")).get("files")) for d in docs}) > 1:
            group["errors"].append("Frozen companion files or constraints differ within circuit")
        configs = {canonical(r.get("effective_config")) for r in members if r.get("effective_config")}
        jars = {r["jar_sha256"] for r in members if is_sha(r.get("jar_sha256"))}
        group["effective_config_variants"] = [json.loads(c) for c in sorted(configs)]
        group["jar_sha256_variants"] = sorted(jars)
        if len(configs) > 1 or len(jars) > 1:
            group["errors"].append("Inconsistent effective toolchain/JAR SHA within frozen circuit")
        for row in members:
            row["errors"].extend(group["errors"])

    for row in rows:
        row["errors"].extend(sources[row["source_index"]]["errors"])
        row["errors"].extend(run_consistency_errors)
        row["errors"] = list(dict.fromkeys(row["errors"]))
        row["qualified"] = not row["errors"] and row.get("reported_status") == "passed"
    for group in groups.values():
        members = [rows[i] for i in group["row_indices"]]
        passed = sum(r["qualified"] for r in members)
        group.update(total_runs=len(members), passed_runs=passed,
                     success_rate=passed / len(members) if members else 0,
                     statistics_all_runs=statistics_for(members),
                     statistics_passed_runs=statistics_for([r for r in members if r["qualified"]]))
        if passed < required_repetitions:
            group["errors"].append("Insufficient qualifying repetitions")
        group["status"] = "passed" if not group["errors"] and passed == len(members) else "failed"

    differences = []
    configurations = list(groups.values())
    for section in ("options", "config"):
        keys = set()
        for group in configurations:
            for variant in group["configuration_variants"]:
                keys.update(obj(variant.get(section)))
        for key in sorted(keys):
            values = [{"source_project": g["source_project"], "input_board_sha256": g["input_board_sha256"],
                       "variants": [{"present": key in obj(v.get(section)), "value": obj(v.get(section)).get(key)}
                                    for v in g["configuration_variants"]]} for g in configurations]
            if len({canonical(v["variants"]) for v in values}) > 1:
                differences.append({"field": section + "." + key, "circuits": values})
    if len({canonical(g["jar_sha256_variants"]) for g in configurations}) > 1:
        differences.append({"field": "job.runtime.jar.sha256", "circuits": [
            {"source_project": g["source_project"], "variants": g["jar_sha256_variants"]} for g in configurations]})
    for path, sha in observed.items():
        try:
            unchanged = hashlib.sha256(path.read_bytes()).hexdigest() == sha
        except OSError:
            unchanged = False
        if not unchanged:
            raise ValueError("Local evidence changed during summary: " + str(path))
    passed = sum(r["qualified"] for r in rows)
    accepted = (bool(rows) and not phase_errors and all(not s["errors"] for s in sources)
                and passed == len(rows) and all(g["status"] == "passed" for g in groups.values()))
    return {"schema_version": 1, "status": "passed" if accepted else "failed", "scope": SCOPE,
            "independent_execution_verification": False, "manufacturing_authorized": False,
            "required_repetitions": required_repetitions, "required_circuits": required_circuits,
            "independent_source_projects": sorted(origins), "total_runs": len(rows),
            "terminal_runs": sum(r["terminal"] for r in rows), "passed_runs": passed,
            "success_rate": passed / len(rows) if rows else 0, "errors": phase_errors,
            "evidence": sources, "circuits": list(groups.values()), "runs": rows,
            "cross_circuit_configuration_differences": differences,
            "statistics_all_runs": statistics_for(rows),
            "statistics_passed_runs": statistics_for([r for r in rows if r["qualified"]])}


def markdown(matrix):
    def cell(value):
        if value is None:
            return "-"
        if type(value) is float:
            return format(value, ".3f")
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(
            ">", "&gt;").replace("|", "&#124;").replace("\r", " ").replace("\n", " ")

    lines = ["# Whole-board Phase Summary", "", "Status: **" + matrix["status"] + "**", "", SCOPE, "",
             f"Qualifying: {matrix['passed_runs']}/{matrix['total_runs']} ({matrix['success_rate']:.1%}); "
             f"terminal: {matrix['terminal_runs']}/{matrix['total_runs']}; "
             f"independent source projects: {len(matrix['independent_source_projects'])}/{matrix['required_circuits']}; "
             f"required passes per circuit: {matrix['required_repetitions']}.", "",
             "| Circuit | Passed/total | Rate | Status |", "| --- | --- | --- | --- |"]
    for circuit in matrix["circuits"]:
        lines.append(f"| {cell(circuit['source_project'])} | {circuit['passed_runs']}/{circuit['total_runs']} "
                     f"| {circuit['success_rate']:.1%} | {circuit['status']} |")
    lines += ["", "All submitted runs, including failed, diagnostic, and missing entries:", "",
              "| Evidence/run | Project | Harness/job | Qualifies | Seconds | Length mm | Vias | DRC/ERC/missing | Reason |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in matrix["runs"]:
        identity = str(row["source_index"] + 1) + "/" + str(row.get("run_index", -1) + 1)
        reasons = list(filter(None, [row.get("error"), row.get("completion_reason"), *row["errors"]]))
        reason = "; ".join(reasons[:3])
        if len(reasons) > 3:
            reason += f"; +{len(reasons) - 3} details in matrix.json"
        values = [identity, row.get("project"), str(row.get("reported_status")) + "/" + str(row.get("job_status")),
                  row["qualified"], row.get("elapsed_seconds"), row.get("routed_length_mm"), row.get("vias"),
                  "/".join(cell(row.get(k)) for k in ("native_drc_errors", "native_erc_errors", "missing_connections")), reason]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    lines += ["", "Timing is completion-flow elapsed time. Statistics include all available run values; "
              "missing values are never zero-filled. Passed-only statistics are also in matrix.json.", "",
              "| Metric (all runs) | Count | Missing | Mean | Median | Min | Max |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, values in matrix["statistics_all_runs"].items():
        lines.append("| " + " | ".join(cell(v) for v in [name, *[values[k] for k in
                     ("count", "missing", "mean", "median", "min", "max")]]) + " |")
    lines += ["", "Cross-circuit configuration differences (allowed): " +
              (", ".join(d["field"] for d in matrix["cross_circuit_configuration_differences"]) or "none") + ".", ""]
    for error in matrix["errors"]:
        lines.append("- " + cell(error))
    for group in matrix["circuits"]:
        for error in group["errors"]:
            lines.append("- " + cell(group["source_project"]) + ": " + cell(error))
    lines += ["", "Source JSON snapshots and local run SHA checks are retained in matrix.json:", ""]
    for i, source in enumerate(matrix["evidence"], 1):
        lines.append(f"- {i}: {cell(source['path'])}; SHA256: {cell(source['sha256'])}")
        for error in source["errors"]:
            lines.append("- Evidence " + str(i) + ": " + cell(error))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, nargs="+", action="extend", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--required-repetitions", type=int, default=2)
    parser.add_argument("--required-circuits", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("Use a new output directory; never overwrite a summary")
        matrix = summarize(args.evidence, args.required_repetitions, args.required_circuits)
        encoded = json.dumps(matrix, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        report = markdown(matrix)
        args.output.mkdir(parents=True, exist_ok=False)
        with (args.output / "matrix.json").open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
        with (args.output / "summary.md").open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(report)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps({"status": matrix["status"], "passed_runs": matrix["passed_runs"],
                      "total_runs": matrix["total_runs"], "output": str(args.output)}))
    return 0 if matrix["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
