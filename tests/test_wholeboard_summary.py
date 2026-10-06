"""SYNTHETIC ONLY: aggregator tests, not native routing or acceptance verification."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/summarize_wholeboard_benchmark.py"
spec = importlib.util.spec_from_file_location("wholeboard_summary", SCRIPT)
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def object_sha(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return sha(raw)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, indent=2, allow_nan=False).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def document(origin, run_numbers=(1, 2), root="C:/synthetic/root", policy="normalize_widths", passes=8):
    options = {"placement_mode": "preserve", "routing_policy": policy, "route_passes": passes,
               "candidate_count": 1, "time_budget_seconds": 5400, "placement_spread_mm": 0.0,
               "repair": {"max_attempts": 24, "allow_multinet": True}}
    config = {"kicad_cli": "kicad-cli", "kicad_python": "python3", "java": root + "/tools/jre25-linux/bin/java",
              "freerouting_jar": root + "/tools/freerouting-2.4.1.jar", "wsl_distro": "Ubuntu",
              "timeout_seconds": 300, "route_timeout_seconds": 3600, "controlled_neckdown": True,
              "fanout": True, "router_optimizer_max_passes": 1, "router_copper_to_edge_clearance_mm": 0.5}
    result = {"status": "passed", "expected_runs": len(run_numbers), "options": options, "config": config,
              "fixture": {"schema_version": 1, "source_project": origin, "source_revision": "source-" + origin,
                          "input_board_sha256": sha(origin), "board": "synthetic.kicad_pcb",
                          "files": {"synthetic.kicad_pcb": sha(origin), "constraints.json": sha("rules"),
                                    "synthetic.kicad_sch": sha(origin + "schematic")},
                          "placement_preserved": True, "rules_preserved": True},
              "implementation_sha256": {"completion.py": sha("synthetic implementation")},
              "harness_sha256": sha("synthetic harness"), "config_sha256": sha(json.dumps(config)),
              "manufacturing_authorized": False, "runs": [], "test_scope": "SYNTHETIC ONLY; not native"}
    for repetition, number in enumerate(run_numbers, 1):
        project = f"synthetic-{origin}-{number}"
        revision, initial = project + "-final", project + "-input"
        metrics = {"revision": revision, "board_sha256": sha(revision), "moved_components": 0,
                   "drc_errors": 0, "erc_errors": 0, "unconnected": 0, "vias": number,
                   "routed_length_mm": 100.0 + number, "tracks": 50, "status": "passed"}
        flow = {"status": "completed", "project": project, "source_revision": initial, "revision": revision,
                "options": deepcopy(options), "best": deepcopy(metrics), "manufacturing_authorized": False,
                "elapsed_seconds": float(number * 10)}
        request = {"operation": "complete", "project": project, "revision": initial,
                   "completion_options": deepcopy(options)}
        job_id = "job-" + project
        runtime = {"schema_version": 1, "job": job_id, "project": project,
                   "request_sha256": object_sha(request), "config": deepcopy(config),
                   "execution_config": deepcopy(config), "submission_cwd": root,
                   "submitted_config_sha256": object_sha(config), "config_sha256": object_sha(config),
                   "jar": {"status": "host_file_hashed", "path": config["freerouting_jar"],
                           "sha256": sha("synthetic jar")}}
        runtime["sha256"] = object_sha(runtime)
        job = {"id": job_id, "project": project, "status": "completed", "request": request,
               "result": {"runtime": runtime, "steps": {"complete": flow}},
               "events": [{"state": "runtime_snapshotted", "runtime_sha256": runtime["sha256"],
                           "config_sha256": runtime["config_sha256"]}]}
        result["runs"].append({"repetition": repetition, "status": "passed", "project": project,
                               "input_revision": initial, "revision": revision, "job_id": job_id,
                               "submitter_disconnected": True, "job": job, "metrics": metrics,
                               "board_sha256": sha(revision), "new_findings": {"drc": 0, "erc": 0},
                               "independent_verification": {"status": "passed", "revision": revision,
                                   "drc": {"status": "ok", "errors": 0, "unconnected": 0,
                                           "source_sha256": sha(revision)},
                                   "erc": {"status": "ok", "errors": 0}}})
    return result


def seal(tmp_path, docs):
    paths = []
    for i, doc in enumerate(docs):
        for run in doc["runs"]:
            if not isinstance(run, dict) or not isinstance(run.get("job"), dict):
                continue
            job = run["job"]
            flow = job.get("result", {}).get("steps", {}).get("complete")
            if not isinstance(flow, dict):
                continue
            path = tmp_path / "local-runs" / (job["id"] + ".json")
            digest = write(path, flow)
            job["events"] = [e for e in job.get("events", []) if "evidence" not in e]
            job["events"].append({"state": "finished", "stage": "complete", "status": flow["status"],
                                  "evidence": str(path.relative_to(tmp_path)), "sha256": digest})
        path = tmp_path / f"synthetic-{i}.json"
        write(path, doc)
        paths.append(path)
    return paths


@pytest.fixture
def phase(tmp_path):
    docs = [document("circuit-a"), document("circuit-b"), document("circuit-c")]
    return docs, seal(tmp_path, docs)


def errors(matrix):
    return "\n".join(matrix["errors"] + [e for row in matrix["runs"] for e in row["errors"]]
                     + [e for group in matrix["circuits"] for e in group["errors"]])


def reseal_runtime(run):
    runtime = run["job"]["result"]["runtime"]
    runtime["request_sha256"] = object_sha(run["job"]["request"])
    runtime["submitted_config_sha256"] = object_sha(runtime["config"])
    runtime["config_sha256"] = object_sha(runtime["execution_config"])
    runtime["sha256"] = object_sha({k: v for k, v in runtime.items() if k != "sha256"})
    run["job"]["events"][0].update(runtime_sha256=runtime["sha256"], config_sha256=runtime["config_sha256"])


def test_synthetic_three_circuits_twice_pass_with_auditable_statistics(phase):
    docs, paths = phase
    matrix = summary.summarize(paths)
    assert matrix["status"] == "passed", errors(matrix)
    assert matrix["total_runs"] == matrix["passed_runs"] == matrix["terminal_runs"] == 6
    assert matrix["success_rate"] == 1
    assert all(c["passed_runs"] == 2 for c in matrix["circuits"])
    assert matrix["statistics_all_runs"]["elapsed_seconds"]["mean"] == 15
    assert matrix["statistics_all_runs"]["routed_length_mm"]["mean"] == 101.5
    assert matrix["statistics_all_runs"]["vias"]["mean"] == 1.5
    assert matrix["evidence"][0]["snapshot"] == docs[0]
    assert matrix["evidence"][0]["sha256"] == hashlib.sha256(paths[0].read_bytes()).hexdigest()
    assert all(r["local_run_evidence"][0]["verified"] for r in matrix["runs"])
    assert matrix["independent_execution_verification"] is False
    assert "not independent native execution verification" in summary.markdown(matrix)


def test_synthetic_cli_writes_only_two_new_outputs_and_never_overwrites(phase, tmp_path):
    _, paths = phase
    target = tmp_path / "new-summary"
    args = ["--evidence", str(paths[0]), str(paths[1]), "--evidence", str(paths[2]), "--output", str(target)]
    assert summary.main(args) == 0
    assert {p.name for p in target.iterdir()} == {"matrix.json", "summary.md"}
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    assert summary.main(args) == 2
    assert before == {p.name: p.read_bytes() for p in target.iterdir()}
    empty = tmp_path / "already-exists"
    empty.mkdir()
    assert summary.main(["--evidence", str(paths[0]), "--output", str(empty)]) == 2
    assert not list(empty.iterdir())


def test_synthetic_different_circuit_options_allowed_and_explicit(tmp_path):
    docs = [document("a"), document("b", policy="strict", passes=20), document("c")]
    matrix = summary.summarize(seal(tmp_path, docs))
    assert matrix["status"] == "passed", errors(matrix)
    differences = {d["field"]: d for d in matrix["cross_circuit_configuration_differences"]}
    assert set(differences) == {"options.routing_policy", "options.route_passes"}
    values = differences["options.routing_policy"]["circuits"]
    assert values[1]["variants"][0]["value"] == "strict"


def test_synthetic_relocated_roots_and_split_evidence_are_equivalent(tmp_path):
    docs = [document("a", (1,), root="C:\\source\\checkout"),
            document("a", (2,), root="D:/desktop/copy"), document("b"), document("c")]
    matrix = summary.summarize(seal(tmp_path, docs))
    assert matrix["status"] == "passed", errors(matrix)
    assert len(matrix["circuits"]) == 3
    assert matrix["circuits"][0]["passed_runs"] == 2
    assert matrix["cross_circuit_configuration_differences"] == []


@pytest.mark.parametrize("kind", ["policy", "passes", "optimizer", "flag", "jar", "java"])
def test_synthetic_per_circuit_inconsistency_rejected(tmp_path, kind):
    docs = [document("a", (1,)), document("a", (2,)), document("b"), document("c")]
    changed = docs[1]
    if kind in ("policy", "passes"):
        key, value = ("routing_policy", "strict") if kind == "policy" else ("route_passes", 20)
        changed["options"][key] = value
        for run in changed["runs"]:
            run["job"]["request"]["completion_options"][key] = value
            run["job"]["result"]["steps"]["complete"]["options"][key] = value
            reseal_runtime(run)
    else:
        key, value = {"optimizer": ("router_optimizer_max_passes", 2), "flag": ("fanout", False),
                      "java": ("java", "C:/synthetic/root/tools/jre99/bin/java"),
                      "jar": ("freerouting_jar", changed["config"]["freerouting_jar"])}[kind]
        changed["config"][key] = value
        for run in changed["runs"]:
            runtime = run["job"]["result"]["runtime"]
            runtime["config"][key] = runtime["execution_config"][key] = value
            if kind == "jar":
                runtime["jar"]["sha256"] = sha("other jar")
            reseal_runtime(run)
    matrix = summary.summarize(seal(tmp_path, docs))
    assert matrix["status"] == "failed"
    assert "within frozen circuit" in errors(matrix)
    assert matrix["circuits"][0]["passed_runs"] == 0


@pytest.mark.parametrize("field", ["implementation_sha256", "harness_sha256"])
def test_synthetic_implementation_is_frozen_across_all_circuits(phase, field):
    docs, paths = phase
    docs[1][field] = {"completion.py": sha("different")} if field == "implementation_sha256" else sha("different")
    write(paths[1], docs[1])
    matrix = summary.summarize(paths)
    assert matrix["status"] == "failed"
    assert "Inconsistent frozen " + field in errors(matrix)


@pytest.mark.parametrize("field", ["job_id", "project", "revision", "input_revision"])
def test_synthetic_duplicate_identities_exclude_both_rows(phase, field):
    docs, paths = phase
    docs[0]["runs"][1][field] = docs[0]["runs"][0][field]
    write(paths[0], docs[0])
    matrix = summary.summarize(paths)
    assert matrix["status"] == "failed"
    assert all(not row["qualified"] for row in matrix["runs"][:2])
    assert all("Duplicate" in " ".join(row["errors"]) for row in matrix["runs"][:2])


def test_synthetic_duplicate_file_and_copied_file_cannot_inflate_count(phase, tmp_path):
    _, paths = phase
    copy = tmp_path / "copy.json"
    copy.write_bytes(paths[0].read_bytes())
    for duplicate in (paths[0], copy):
        matrix = summary.summarize([*paths, duplicate])
        assert matrix["status"] == "failed"
        assert len(matrix["runs"]) == 8
        assert matrix["passed_runs"] == 4


def test_synthetic_source_project_and_input_aliases_do_not_establish_independence(phase):
    docs, paths = phase
    docs[2]["fixture"]["source_project"] = docs[1]["fixture"]["source_project"]
    write(paths[2], docs[2])
    matrix = summary.summarize(paths)
    assert matrix["status"] == "failed"
    assert len(matrix["independent_source_projects"]) == 2
    docs[2]["fixture"]["source_project"] = "circuit-c"
    docs[2]["fixture"]["input_board_sha256"] = docs[1]["fixture"]["input_board_sha256"]
    docs[2]["fixture"]["files"]["synthetic.kicad_pcb"] = docs[1]["fixture"]["input_board_sha256"]
    write(paths[2], docs[2])
    assert summary.summarize(paths)["status"] == "failed"


def test_synthetic_reference_flags_are_not_required_or_used(phase):
    docs, paths = phase
    for doc, path in zip(docs, paths):
        doc["fixture"]["reference"] = True
        doc["fixture"]["reference_only"] = True
        write(path, doc)
    assert summary.summarize(paths)["status"] == "passed"


@pytest.mark.parametrize("mutation,reason", [
    (("status", "failed"), "Harness run did not pass"),
    (("status", "diagnostic"), "Harness run did not pass"),
    (("status", "running"), "terminal"),
    (("submitter_disconnected", False), "disconnection"),
    (("submitter_disconnected", 1), "disconnection"),
    (("metrics.moved_components", 1), "metrics.moved_components"),
    (("metrics.moved_components", False), "metrics.moved_components"),
    (("independent_verification.drc.errors", 1), "native DRC"),
    (("independent_verification.erc.errors", 1), "native ERC"),
    (("independent_verification.drc.unconnected", 1), "missing connections"),
    (("independent_verification.drc.errors", None), "native DRC"),
    (("independent_verification.status", "blocked"), "verification did not pass"),
    (("independent_verification.revision", "other-revision"), "verification revision mismatch"),
    (("independent_verification.drc.source_sha256", "0" * 64), "native DRC board SHA mismatch"),
    (("new_findings.drc", 1), "new DRC"),
    (("new_findings.erc", 1), "new ERC"),
    (("new_findings.drc", False), "new DRC"),
    (("job.status", "blocked"), "Job did not complete"),
    (("job.status", "cancelled"), "Job did not complete"),
    (("job.status", "running"), "terminal"),
    (("job.result.runtime.sha256", "0" * 64), "Runtime snapshot SHA"),
    (("job.result.runtime.jar.sha256", None), "JAR SHA missing"),
    (("job.result.runtime.execution_config.fanout", False), "Effective job toolchain"),
    (("board_sha256", "0" * 64), "Final board SHA"),
])
def test_synthetic_failed_or_inconsistent_run_never_becomes_passed(phase, mutation, reason):
    docs, paths = phase
    dotted, value = mutation
    target = docs[0]["runs"][0]
    keys = dotted.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    write(paths[0], docs[0])
    matrix = summary.summarize(paths)
    assert matrix["status"] == "failed"
    assert not matrix["runs"][0]["qualified"]
    assert reason in errors(matrix)
    assert matrix["total_runs"] == 6


@pytest.mark.parametrize("status", ["failed", "running", "diagnostic"])
def test_synthetic_parent_status_cannot_be_overruled_by_green_metrics(phase, status):
    docs, paths = phase
    docs[0]["status"] = status
    write(paths[0], docs[0])
    assert summary.summarize(paths)["status"] == "failed"


@pytest.mark.parametrize("change", ["alter", "missing", "wrong_sha", "different_payload", "remote", "no_proof"])
def test_synthetic_immutable_local_completion_evidence_required(phase, change):
    docs, paths = phase
    run = docs[0]["runs"][0]
    event = run["job"]["events"][-1]
    local = paths[0].parent / event["evidence"]
    if change == "alter":
        local.write_bytes(local.read_bytes() + b" ")
    elif change == "missing":
        local.unlink()
    elif change == "wrong_sha":
        event["sha256"] = "0" * 64
    elif change == "different_payload":
        event["sha256"] = write(local, {"status": "completed", "test_scope": "SYNTHETIC ONLY"})
    elif change == "remote":
        event["evidence"] = "https://example.invalid/evidence.json"
    else:
        run["job"]["events"].pop()
    write(paths[0], docs[0])
    matrix = summary.summarize(paths)
    assert matrix["status"] == "failed"
    assert not matrix["runs"][0]["qualified"]
    assert "evidence" in errors(matrix)


def test_synthetic_missing_requested_run_cannot_disappear_from_denominator(phase):
    docs, paths = phase
    docs[0]["status"] = "running"
    docs[0]["runs"].pop()
    write(paths[0], docs[0])
    matrix = summary.summarize(paths)
    assert matrix["status"] == "failed"
    assert matrix["total_runs"] == 6
    assert any(r["reported_status"] == "missing" for r in matrix["runs"])
    assert matrix["statistics_all_runs"]["elapsed_seconds"]["missing"] == 1


def test_synthetic_insufficient_repetitions_and_custom_threshold(tmp_path):
    docs = [document("a", (1,)), document("b"), document("c")]
    paths = seal(tmp_path, docs)
    matrix = summary.summarize(paths)
    assert matrix["status"] == "failed"
    assert matrix["passed_runs"] == 5
    assert "Insufficient qualifying repetitions" in errors(matrix)
    assert summary.summarize(paths, required_repetitions=1)["status"] == "passed"
    assert summary.summarize(paths, required_circuits=4)["status"] == "failed"


def test_synthetic_insufficient_circuits_preserves_qualifying_run_counts(phase):
    _, paths = phase
    matrix = summary.summarize(paths[:1])
    assert matrix["status"] == "failed"
    assert matrix["passed_runs"] == matrix["total_runs"] == 2
    assert matrix["success_rate"] == 1
    assert "Insufficient independent source projects" in errors(matrix)


def test_synthetic_cross_circuit_toolchain_differences_allowed_and_listed(phase, tmp_path):
    docs, _ = phase
    docs[1]["config"]["router_optimizer_max_passes"] = 2
    for run in docs[1]["runs"]:
        runtime = run["job"]["result"]["runtime"]
        runtime["config"]["router_optimizer_max_passes"] = 2
        runtime["execution_config"]["router_optimizer_max_passes"] = 2
        runtime["jar"]["sha256"] = sha("another synthetic jar")
        reseal_runtime(run)
    matrix = summary.summarize(seal(tmp_path, docs))
    assert matrix["status"] == "passed", errors(matrix)
    fields = {d["field"] for d in matrix["cross_circuit_configuration_differences"]}
    assert "config.router_optimizer_max_passes" in fields
    assert "job.runtime.jar.sha256" in fields


def test_synthetic_effective_defaults_and_path_identity():
    config = document("a")["config"]
    other = deepcopy(config)
    config.pop("router_optimizer_max_passes")
    other["router_optimizer_max_passes"] = 100
    assert summary.semantic_config(config) == summary.semantic_config(other)
    other["java"] = "C:/elsewhere/jre25-linux/bin/java"
    assert summary.semantic_config(config) != summary.semantic_config(other)
    other["java"] = "tools/../java"
    with pytest.raises(ValueError, match="parent-relative"):
        summary.semantic_config(other)


def test_synthetic_extra_failed_diagnostic_blocks_otherwise_complete_phase(phase, tmp_path):
    docs, _ = phase
    diagnostic = document("circuit-a", (3,))
    diagnostic["status"] = diagnostic["runs"][0]["status"] = "failed"
    diagnostic["runs"][0]["error"] = "Synthetic diagnostic | error\nsecond line"
    docs.append(diagnostic)
    matrix = summary.summarize(seal(tmp_path, docs))
    assert matrix["status"] == "failed"
    assert matrix["passed_runs"] == 6 and matrix["total_runs"] == 7
    assert matrix["success_rate"] == 6 / 7
    report = summary.markdown(matrix)
    assert "synthetic-circuit-a-3" in report
    assert "Synthetic diagnostic &#124; error second line" in report
    assert matrix["statistics_all_runs"]["elapsed_seconds"]["count"] == 7
    assert matrix["statistics_passed_runs"]["elapsed_seconds"]["count"] == 6


@pytest.mark.parametrize("raw", ['{"status":"passed","status":"failed"}', '{"x":NaN}',
                                 '{"x":1e999}', "[]", "{unfinished"])
def test_synthetic_malformed_evidence_retained_as_failure(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_text(raw, encoding="utf-8")
    matrix = summary.summarize([path])
    assert matrix["status"] == "failed"
    assert matrix["total_runs"] == 1
    assert matrix["runs"][0]["reported_status"] == "unreadable"


@pytest.mark.parametrize("value", [None, [], "bad", 42])
def test_synthetic_malformed_run_is_a_diagnostic_entry(phase, value):
    docs, paths = phase
    docs[0]["runs"][0] = value
    write(paths[0], docs[0])
    matrix = summary.summarize(paths)
    assert matrix["status"] == "failed" and matrix["total_runs"] == 6


def test_synthetic_file_change_during_summary_rejected_before_output(phase, tmp_path, monkeypatch):
    _, paths = phase
    original = summary.inspect_run
    mutated = False

    def inspect(*args):
        nonlocal mutated
        result = original(*args)
        if not mutated:
            paths[0].write_bytes(paths[0].read_bytes() + b"\n")
            mutated = True
        return result

    monkeypatch.setattr(summary, "inspect_run", inspect)
    output = tmp_path / "summary"
    assert summary.main(["--evidence", *map(str, paths), "--output", str(output)]) == 2
    assert not output.exists()


@pytest.mark.parametrize("kwargs", [{"required_repetitions": 0}, {"required_repetitions": True},
                                    {"required_circuits": 2}, {"required_circuits": -1}])
def test_synthetic_invalid_thresholds_rejected(kwargs):
    with pytest.raises(ValueError):
        summary.summarize([], **kwargs)


def test_synthetic_cli_failed_phase_exit_code_and_no_native_calls(phase, tmp_path, monkeypatch):
    import builtins
    import subprocess

    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(("pcb_weaver", "mcp")), "Summary must not import the frozen implementation"
        return original(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Synthetic summary must never start native processes")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    _, paths = phase
    assert summary.main(["--evidence", str(paths[0]), "--output", str(tmp_path / "incomplete")]) == 1
