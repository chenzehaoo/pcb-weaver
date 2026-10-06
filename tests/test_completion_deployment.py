"""Synthetic deployment checks only; no native calls or desktop workspace writes."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from pcb_weaver.storage import digest, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("completion_deployment", ROOT / "scripts/verify_completion_deployment.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


@pytest.fixture
def copies(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    for name in ("scripts/wholeboard_benchmark.py", "src/pcb_weaver/extra_helper.py"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic only\n", encoding="utf-8")
    for name in deployment.FIXTURES:
        folder = source / name
        folder.mkdir(parents=True)
        for relative in ("board.kicad_pcb", "constraints.json", "libs/part.kicad_mod"):
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic " + relative, encoding="utf-8")
        write_json(folder / "BENCHMARK.json", {
            "board": "board.kicad_pcb", "input_board_sha256": digest(folder / "board.kicad_pcb"),
            "files": deployment.fixture_files(folder)})
    shutil.copytree(source, target)
    monkeypatch.setattr(deployment, "FILES", ["scripts/wholeboard_benchmark.py"])
    return source, target


def test_v2_preserves_historical_inventory_and_adds_existing_fixed_files():
    historic = read_json(ROOT / "docs/validation/fixed-deployment-preflight.json")["files"]
    assert set(historic) <= set(deployment.FILES)
    assert len(deployment.FILES) == len(set(deployment.FILES))
    assert all((ROOT / name).is_file() for name in deployment.FILES)
    assert {"scripts/wholeboard_benchmark.py", "scripts/summarize_wholeboard_benchmark.py",
            "tests/test_wholeboard_summary.py", "tests/test_job_runtime.py", "toolchain.fixed-benchmark.json",
            "benchmarks/fixed-completion-options.json", "benchmarks/fixed-completion-options-v3.json",
            "benchmarks/fixed-completion-options-v4.json", "tests/test_completion_cleanup.py",
            "tests/test_completion_cycles.py", "tests/test_publish_completion_review.py",
            "tests/test_completion_via_cleanup.py"} <= set(deployment.FILES)


def test_synthetic_bytes_include_helpers_and_full_fixture_trees(copies):
    result = {"files": {}}
    manifests = deployment.verify_deployed_files(*copies, result)
    assert set(manifests) == set(deployment.FIXTURES)
    assert "src/pcb_weaver/extra_helper.py" in result["files"]
    for name in deployment.FIXTURES:
        assert name + "/BENCHMARK.json" in result["files"]
        assert name + "/libs/part.kicad_mod" in result["files"]


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("change", ["modify", "remove", "extra"])
def test_synthetic_fixture_changes_fail_even_with_matching_deployment(copies, side, change):
    path = copies[side] / deployment.FIXTURES[0] / "libs/part.kicad_mod"
    if change == "remove":
        path.unlink()
    elif change == "extra":
        path.with_name("extra.kicad_mod").write_text("extra", encoding="utf-8")
    else:
        path.write_text("changed", encoding="utf-8")
    with pytest.raises(AssertionError, match="fixture changed"):
        deployment.verify_deployed_files(*copies, {"files": {}})


def test_synthetic_module_byte_mismatch_fails(copies):
    (copies[1] / "src/pcb_weaver/extra_helper.py").write_text("changed", encoding="utf-8")
    with pytest.raises(AssertionError, match="extra_helper"):
        deployment.verify_deployed_files(*copies, {"files": {}})


@pytest.mark.parametrize("change", ["unchanged", "modified", "missing"])
def test_synthetic_v4_options_require_deployed_byte_match(copies, monkeypatch, change):
    name = "benchmarks/fixed-completion-options-v4.json"
    for root in copies:
        write_json(root / name, {"synthetic": True, "repair_cycles": 3})
    monkeypatch.setattr(deployment, "FILES", [*deployment.FILES, name])
    if change == "modified":
        write_json(copies[1] / name, {"synthetic": True, "repair_cycles": 2})
    elif change == "missing":
        (copies[1] / name).unlink()
    result = {"files": {}}
    if change == "unchanged":
        deployment.verify_deployed_files(*copies, result)
        assert result["files"][name] == digest(copies[0] / name)
    else:
        with pytest.raises((AssertionError, FileNotFoundError)):
            deployment.verify_deployed_files(*copies, result)


@pytest.fixture
def preserved(tmp_path, monkeypatch):
    folder = tmp_path / "revision"
    design = folder / "design"
    design.mkdir(parents=True)
    (design / "board.kicad_pcb").write_text("original routed synthetic board", encoding="utf-8")
    (design / "companion.kicad_sch").write_text("original schematic", encoding="utf-8")
    write_json(folder / "constraints.json", {"synthetic": True})
    sha = digest(design / "board.kicad_pcb")
    project, revision = deployment.PRESERVED_SYSTEM[:2]
    monkeypatch.setattr(deployment, "PRESERVED_SYSTEM", (project, revision, sha))
    files = deployment.fixture_files(design)
    constraint = digest(folder / "constraints.json")
    data = {"id": revision, "project": project, "board": "board.kicad_pcb", "digest": "synthetic",
            "files": files, "constraints_hash": constraint}
    manifest = {"source_project": project, "source_revision": revision, "source_board_sha256": sha,
                "board": "board.kicad_pcb", "files": {**files, "constraints.json": constraint}}
    manifest["files"]["board.kicad_pcb"] = "unrouted fixture hash"
    calls = []

    def verified(p, r):
        calls.append((p, r))
        return data, folder

    return SimpleNamespace(_verified=verified), manifest, folder, data, calls


def test_synthetic_preservation_uses_original_board_and_all_companions(preserved):
    engine, manifest, _, data, calls = preserved
    result = deployment.verify_preserved_system(engine, manifest)
    assert calls == [deployment.PRESERVED_SYSTEM[:2]]
    assert result["sha256"] == manifest["source_board_sha256"]
    assert result["sha256"] != manifest["files"][manifest["board"]]
    assert result["files"] == data["files"]
    assert result["constraints_sha256"] == data["constraints_hash"]


@pytest.mark.parametrize("change", ["board", "companion", "constraints", "manifest", "origin", "extra"])
def test_synthetic_preserved_revision_tampering_fails(preserved, change):
    engine, manifest, folder, data, _ = preserved
    if change == "origin":
        manifest["source_board_sha256"] = "wrong"
    elif change == "manifest":
        data["files"] = {**data["files"], "companion.kicad_sch": "wrong"}
    else:
        name = {"board": "design/board.kicad_pcb", "companion": "design/companion.kicad_sch",
                "constraints": "constraints.json", "extra": "design/extra.kicad_sch"}[change]
        (folder / name).write_text("changed", encoding="utf-8")
    with pytest.raises(AssertionError):
        deployment.verify_preserved_system(engine, manifest)


def test_synthetic_deployment_pass_keeps_wholeboard_phase_unevaluated(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "verify_deployed_files", lambda *a: {deployment.FIXTURES[0]: {}})
    monkeypatch.setattr(deployment, "verify_preserved_system", lambda *a: {"sha256": "synthetic"})
    hashes = {".mcp.json": "23e3dca394903f0aa48564e7a153dee0471071768abbc63def928ad4435cdfa5",
              "toolchain.unified.json": "fd6ee5114bf647e1912d281743014694157ca2e1e73c7d059ef96b1be6e5641e",
              "r-b8fb758edb12488f": "1c84db8a3ecb559c33c0b01bd62f4ba06f7afa34116e99bca7c16b2e30b06064",
              "r-1bbf979cb3cc4206": "3f871730ae06e4c6c401555d0be4cb85effb43f7106385bb94387205c14584c5"}
    monkeypatch.setattr(deployment, "digest", lambda p: hashes.get(p.name, "synthetic"))
    monkeypatch.setattr(deployment, "load_runtime", lambda *a: (tmp_path, {}))
    calls = []

    def verified(project, revision):
        calls.append((project, revision))
        return {"board": revision, "digest": "synthetic"}, tmp_path

    monkeypatch.setattr(deployment, "EngineeringService", lambda *a: SimpleNamespace(_verified=verified))
    check = {"status": "passed", "drc": {"errors": 0, "unconnected": 0, "warnings": 53},
             "erc": {"errors": 0, "warnings": 16}, "verification_id": "synthetic-check"}
    monkeypatch.setattr(deployment.catalog, "verification", lambda *a: deepcopy(check))
    evidence = tmp_path / "synthetic-mcp.json"
    write_json(evidence, {"status": "passed", "submitter_disconnected": True,
        "project": "synthetic-project", "revision": "synthetic-revision", "job_id": "synthetic-job",
        "job": {"result": {"steps": {"complete": {"status": "completed", "manufacturing_authorized": False,
            "baseline": {"tracks": 0, "vias": 0}, "best": {"board_sha256": "synthetic",
            "verification_id": "synthetic-check"}}}}}})
    output = tmp_path / "result.json"
    args = SimpleNamespace(root=tmp_path, output=output, mcp_evidence=[evidence])
    deployment.run(args)
    result = read_json(output)
    assert result["status"] == "passed" and result["schema_version"] == 2
    assert result["wholeboard_phase"]["status"] == "not_evaluated"
    assert result["wholeboard_phase"]["aggregate_status"] == "pending"
    budget = result["wholeboard_phase"]["v3_repair_budget"]
    options = read_json(ROOT / budget["options_file"])
    assert budget["outer_time_budget_seconds"] == options["time_budget_seconds"] == 7200
    assert budget["max_total_repair_cycles"] == options["repair_cycles"] == 3
    assert budget["max_additional_repair_cycles"] == options["repair_cycles"] - 1 == 2
    assert budget["per_cycle_max_attempts"] == options["repair"]["max_attempts"] == 24
    assert budget["per_cycle_time_budget_seconds"] == options["repair"]["time_budget_seconds"] == 1800
    assert "not a wall-clock guarantee" in budget["scope"]
    current = result["wholeboard_phase"]["repair_budget"]
    assert budget["options_file"] == "benchmarks/fixed-completion-options-v3.json"
    assert current == {**budget, "options_file": "benchmarks/fixed-completion-options-v4.json"}
    v4_options = read_json(ROOT / current["options_file"])
    assert current["outer_time_budget_seconds"] == v4_options["time_budget_seconds"]
    assert current["max_total_repair_cycles"] == v4_options["repair_cycles"]
    assert current["max_additional_repair_cycles"] == v4_options["repair_cycles"] - 1
    assert current["per_cycle_max_attempts"] == v4_options["repair"]["max_attempts"]
    assert current["per_cycle_time_budget_seconds"] == v4_options["repair"]["time_budget_seconds"]
    assert len(result["mcp_runs"]) == 1 and len(calls) == 3
    check["drc"]["errors"] = 1
    with pytest.raises(AssertionError):
        deployment.run(args)
    assert read_json(output)["status"] == "failed"
