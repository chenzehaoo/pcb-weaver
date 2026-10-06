"""Check deployment v2 bytes and preserved revisions, retaining historical MCP checks.

Whole-board phase acceptance is evaluated separately by summarize_wholeboard_benchmark.py.
"""
import argparse
from pathlib import Path
from pcb_weaver import catalog
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, read_json, write_json


FILES = ["src/pcb_weaver/" + name for name in (
    "models.py", "completion.py", "completion_cleanup.py", "clearance.py", "planning.py", "netlist.py", "service.py", "jobs.py",
    "server.py", "integration.py", "web/app.js", "web/index.html", "web/style.css")]
FILES += ["skills/pcb-engineering/SKILL.md", "README.md", "docs/COMPLETION-VALIDATION.md",
          "tests/test_completion.py", "tests/test_completion_geometry.py", "tests/test_completion_cleanup.py", "tests/test_clearance.py"]
FILES += ["scripts/" + name for name in ("accept_completion.py", "accept_completion_mcp.py",
    "prepare_completion_benchmark.py", "prepare_completion_checkpoint.py", "check_completion_ui.cjs",
    "verify_completion_deployment.py", "probe_completion_widths.py", "probe_completion_clearance.py", "probe_completion_connections.py",
    "publish_completion_review.py")]
FILES += ["src/pcb_weaver/auto_repair.py", "src/pcb_weaver/auto_proposal.py", "src/pcb_weaver/joint_escape.py",
          "tests/test_auto_proposal.py", "tests/test_joint_escape.py", "docs/AUTHORIZED-REPAIR-VALIDATION.md",
          "scripts/resume_authorized_repair.py", "scripts/verify_authorized_repair.py",
          "scripts/probe_authorized_proposals.py", "scripts/check_auto_repair_ui.cjs"]
FILES += ["src/pcb_weaver/reference_repair.py", "src/pcb_weaver/ripup_planning.py",
          "src/pcb_weaver/negotiated_reroute.py", "tests/test_reference_repair.py",
          "tests/test_negotiated_reroute.py", "tests/test_repair_protocol.py",
          "scripts/verify_multinet_acceptance.py", "scripts/probe_multinet_repair.py",
          "scripts/probe_negotiated_repair.py", "docs/MULTINET-REPAIR-VALIDATION.md"]
FILES += ["tests/test_auto_repair.py", "tests/test_autonomous_contract.py",
          "scripts/accept_autonomous_probe.py", "scripts/verify_autonomous_acceptance.py",
          "docs/AUTONOMOUS-REPAIR-VALIDATION.md"]
FILES += ["src/pcb_weaver/grid_route.py", "tests/test_grid_route.py"]
FILES += ["src/pcb_weaver/controlled_neckdown.py", "tests/test_controlled_neckdown.py", "tests/test_jobs.py",
          "src/pcb_weaver/board.py", "src/pcb_weaver/repair_geometry.py", "tests/test_board_cache.py",
          "scripts/benchmark_repair_checkpoint.py", "scripts/benchmark_board_parse.py"]
FILES += ["scripts/wholeboard_benchmark.py", "scripts/summarize_wholeboard_benchmark.py",
          "scripts/verify_published_mcp.py", "tests/test_verify_published_mcp.py",
          "tests/test_wholeboard_benchmark.py", "tests/test_wholeboard_summary.py",
          "tests/test_wholeboard_worker.py", "tests/test_completion_local_rules.py",
          "tests/test_completion_deployment.py", "tests/test_job_runtime.py", "tests/test_job_idempotency.py",
          "tests/test_runtime.py", "tests/test_toolchain.py", "tests/test_drc_findings.py",
          "tests/test_persisted_track_minima.py", "tests/test_ses_widths.py",
          "tests/test_connection_geometry.py", "tests/test_placement_large.py",
          "benchmarks/fixed-completion-options.json", "toolchain.fixed-benchmark.json",
          "docs/FIXED-WHOLEBOARD-VALIDATION.md"]
FILES += ["benchmarks/fixed-completion-options-v3.json", "benchmarks/fixed-completion-options-v4.json", "tests/test_completion_cycles.py",
          "tests/test_publish_completion_review.py", "tests/test_completion_via_cleanup.py"]

FIXTURES = ["examples/fixed-" + name + "-20260913" for name in ("system", "cpld", "programmer")]
PRESERVED_SYSTEM = ("completion-system-review", "r-08ee661571804502",
                    "18f8d0a04d0eee735c95c7d023263301549f066726e6d370daadd737baa8289d")


def fixture_files(folder):
    return {p.relative_to(folder).as_posix(): digest(p)
            for p in sorted(folder.rglob("*")) if p.is_file()}


def verify_deployed_files(source, root, result):
    manifests = {}
    names = set(FILES)
    # Include every current Python module used by the frozen harness, including helpers.
    names.update(p.relative_to(source).as_posix() for p in (source / "src/pcb_weaver").glob("*.py"))
    for name in FIXTURES:
        manifest = read_json(source / name / "BENCHMARK.json")
        expected = dict(manifest["files"])
        assert expected[manifest["board"]] == manifest["input_board_sha256"], name
        expected["BENCHMARK.json"] = digest(source / name / "BENCHMARK.json")
        assert fixture_files(source / name) == expected, "Source fixture changed: " + name
        assert fixture_files(root / name) == expected, "Deployed fixture changed: " + name
        names.update(name + "/" + relative for relative in expected)
        manifests[name] = manifest
    for name in sorted(names):
        sha = digest(source / name)
        assert digest(root / name) == sha, name
        result["files"][name] = sha
    return manifests


def verify_preserved_system(engine, manifest):
    project, revision, sha = PRESERVED_SYSTEM
    assert (manifest["source_project"], manifest["source_revision"], manifest["source_board_sha256"]) == (
        project, revision, sha), "Frozen system origin changed"
    data, folder = engine._verified(project, revision)
    assert (data["project"], data["id"], data["board"]) == (project, revision, manifest["board"])
    # The fixture changed only routing on the board; all companions and constraints are original bytes.
    expected = dict(manifest["files"])
    constraints_sha = expected.pop("constraints.json")
    expected[manifest["board"]] = sha
    assert data["files"] == expected, "Preserved system design manifest changed"
    assert fixture_files(folder / "design") == expected, "Preserved system design bytes changed"
    assert data["constraints_hash"] == digest(folder / "constraints.json") == constraints_sha
    return {"project": project, "sha256": sha, "digest": data["digest"],
            "files": expected, "constraints_sha256": constraints_sha}


def run(args):
    source = Path(__file__).resolve().parents[1]
    root = args.root.resolve(strict=True)
    result = {"schema_version":2, "status":"running", "files":{}, "config":{},
              "preserved_revisions":{}, "mcp_runs":[],
              "wholeboard_phase":{"status":"not_evaluated", "aggregate_status":"pending",
                  "v3_repair_budget":{
                      "options_file":"benchmarks/fixed-completion-options-v3.json",
                      "outer_time_budget_seconds":7200, "max_total_repair_cycles":3,
                      "max_additional_repair_cycles":2, "per_cycle_max_attempts":24,
                      "per_cycle_time_budget_seconds":1800,
                      "scope":"Initial cycle plus up to two additional cycles, within the remaining outer "
                              "completion scheduling budget. Active native calls retain their own timeouts; "
                              "these limits are not a wall-clock guarantee."},
                  "scope":"Deployment bytes and historical MCP checks do not establish a whole-board phase pass. "
                          "Use summarize_wholeboard_benchmark.py with separate evidence inputs."}}
    # Keep the historical field intact for existing evidence consumers.
    result["wholeboard_phase"]["repair_budget"] = {
        **result["wholeboard_phase"]["v3_repair_budget"],
        "options_file": "benchmarks/fixed-completion-options-v4.json"}
    try:
        manifests = verify_deployed_files(source, root, result)
        for name,sha in {
            ".mcp.json":"23e3dca394903f0aa48564e7a153dee0471071768abbc63def928ad4435cdfa5",
            "toolchain.unified.json":"fd6ee5114bf647e1912d281743014694157ca2e1e73c7d059ef96b1be6e5641e"
        }.items():
            assert digest(root / name) == sha, name
            result["config"][name] = sha
        workspace,config = load_runtime(root / "data",root / "toolchain.unified.json")
        engine = EngineeringService(workspace,config)
        for revision,sha in {
            "r-b8fb758edb12488f":"1c84db8a3ecb559c33c0b01bd62f4ba06f7afa34116e99bca7c16b2e30b06064",
            "r-1bbf979cb3cc4206":"3f871730ae06e4c6c401555d0be4cb85effb43f7106385bb94387205c14584c5"
        }.items():
            data,folder = engine._verified("system-clearance-acceptance",revision)
            assert digest(folder / "design" / data["board"]) == sha
            result["preserved_revisions"][revision] = {"sha256":sha,"digest":data["digest"]}
        result["preserved_revisions"][PRESERVED_SYSTEM[1]] = verify_preserved_system(
            engine, manifests[FIXTURES[0]])
        previous = catalog.verification(engine,"system-clearance-acceptance","r-1bbf979cb3cc4206")
        assert previous["status"] == "passed"
        assert (previous["drc"]["errors"],previous["drc"]["unconnected"],previous["erc"]["errors"]) == (0,0,0)
        assert (previous["drc"]["warnings"],previous["erc"]["warnings"]) == (53,16)
        for path in args.mcp_evidence:
            evidence = read_json(path)
            assert evidence["status"] == "passed" and evidence["submitter_disconnected"]
            flow = evidence["job"]["result"]["steps"]["complete"]
            assert flow["status"] == "completed" and not flow["manufacturing_authorized"]
            assert flow["baseline"]["tracks"] == 0 and flow["baseline"]["vias"] == 0
            project,revision = evidence["project"],evidence["revision"]
            data,folder = engine._verified(project,revision)
            assert digest(folder / "design" / data["board"]) == flow["best"]["board_sha256"]
            check = catalog.verification(engine,project,revision)
            assert check["status"] == "passed" and check["drc"]["unconnected"] == 0
            assert check["drc"]["errors"] == 0 and check["erc"]["errors"] == 0
            assert check["verification_id"] == flow["best"]["verification_id"]
            result["mcp_runs"].append({"project":project,"revision":revision,"job":evidence["job_id"],
                "source_report_sha256":digest(path),"metrics":flow["best"]})
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed",error=str(error))
        raise
    finally:
        write_json(args.output,result)
    print(f"Deployment v2 passed: {len(result['files'])} files, preserved prior boards/configs, "
          f"{len(result['mcp_runs'])} historical MCP runs; whole-board phase not evaluated")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--mcp-evidence",type=Path,nargs="+",required=True)
    run(parser.parse_args())
