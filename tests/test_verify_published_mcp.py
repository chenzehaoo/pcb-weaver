"""MOCK/SYNTHETIC ONLY: no MCP server, native job, or real client acceptance run.

Transport, catalog authentication and board inventory are mocked. File hashes,
publication comparisons, read-only SQLite access and protocol gates run normally.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from pcb_weaver.storage import digest, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_published_mcp", ROOT / "scripts/verify_published_mcp.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class Model:
    def __init__(self, data):
        self.data = data
        self.__dict__.update(data)

    def model_dump(self, **kwargs):
        return deepcopy(self.data)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No real MCP server or native process may start in mock tests")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    root, isolated = tmp_path / "desktop", tmp_path / "isolated"
    workspace = root / "data"
    workspace.mkdir(parents=True)
    isolated.mkdir()
    command = root / "mock-python.exe"
    command.write_bytes(b"synthetic interpreter; never executed")
    write_json(root / "toolchain.unified.json", {"synthetic": True})
    config = {"command": str(command), "args": ["-m", "pcb_weaver.server"],
              "env": {"PCB_WEAVER_WORKSPACE": str(workspace), "PCB_WEAVER_CONFIG": str(root / "toolchain.unified.json")}}
    write_json(root / ".mcp.json", {"mcpServers": {"pcb-weaver": config}})
    for code_root in (tmp_path / "local-code", root / "src/pcb_weaver"):
        code_root.mkdir(parents=True)
        (code_root / "__init__.py").write_text("# synthetic code\n", encoding="utf-8")
    monkeypatch.setattr(verifier, "pcb_weaver", SimpleNamespace(__file__=str(tmp_path / "local-code/__init__.py")))
    state = SimpleNamespace(started=0, calls=[], data={}, folders={}, checks={}, inspections={}, inventories={},
                            publications=[], encoding="structured", hook=lambda name: None, tool_error=False)
    for i in range(2):
        records = {}
        for role, base in (("source", isolated), ("target", workspace)):
            project, revision = f"{role}-{i}", f"r-{role}-{i}"
            folder = base / "projects" / project / "revisions" / revision
            design = folder / "design"
            design.mkdir(parents=True)
            (design / "board.kicad_pcb").write_text(f"synthetic board {i}", encoding="utf-8")
            (design / "board.kicad_sch").write_text(f"synthetic schematic {i}", encoding="utf-8")
            write_json(folder / "constraints.json", {"synthetic": i})
            files = {p.name: digest(p) for p in design.iterdir()}
            data = {"id": revision, "project": project, "board": "board.kicad_pcb", "files": files,
                    "constraints_hash": digest(folder / "constraints.json"), "digest": f"digest-{i}"}
            write_json(folder / "revision.json", data)
            verification_id = f"v-{role}-{i}"
            artifacts = folder / "verification" / verification_id
            finding = {"type": "synthetic", "severity": "warning", "description": "mock only", "items": [{"uuid": "pin"}]}
            write_json(artifacts / "drc.json", {"violations": [finding, finding], "unconnected_items": [], "schematic_parity": []})
            write_json(artifacts / "erc.json", {"$schema": "https://schemas.kicad.org/erc.v1.json",
                       "sheets": [{"uuid_path": "/synthetic", "violations": [finding]}]})
            check = {"status": "passed", "revision": revision, "revision_digest": data["digest"],
                     "verification_id": verification_id, "connectivity": {"status": "passed"},
                     "drc": {"status": "ok", "errors": 0, "unconnected": 0, "warnings": 2,
                             "source_sha256": files["board.kicad_pcb"]},
                     "erc": {"status": "ok", "errors": 0, "warnings": 1},
                     "evidence_hashes": {p.name: digest(p) for p in artifacts.iterdir()}}
            inspection = {"revision": data, "constraints": {"synthetic": i},
                          "board": {"footprints": [{"reference": "U1"}], "tracks": 10, "vias": 2}}
            inventory = {"schema_version": "1.0", "project": project, "revision": revision,
                         "revision_digest": data["digest"], "units": "mm", "coverage": {},
                         "summary": {"component_count": 1, "pad_count": 8, "net_count": 4, "track_count": 10,
                                     "via_count": 2, "layer_count": 2, "package_count": 1},
                         "sources": {"board": {"path": "design/board.kicad_pcb", "sha256": files["board.kicad_pcb"]},
                                     "constraints": {"path": "constraints.json", "sha256": data["constraints_hash"]},
                                     "verification": {"status": "passed", "verification_id": verification_id,
                                                      "connectivity_status": "passed"}, "external_resources_fetched": False}}
            state.data[revision], state.folders[revision], state.checks[revision] = data, folder, check
            state.inspections[revision], state.inventories[revision] = inspection, inventory
            records[role] = data
        source, target = records["source"], records["target"]
        source_folder = state.folders[source["id"]]
        pointer = source_folder / "completion/result.json"
        write_json(pointer, {"project": source["project"], "revision": source["id"]})
        publication = {"status": "published_review", "engineering_status": "passed", "source_immutable": True,
                       "preservation_checks_passed": True, "manufacturing_authorized": False, "wholeboard_benchmark": False,
                       "workspace": str(workspace), "source_workspace": str(isolated),
                       "project": target["project"], "revision": target["id"],
                       "source_project": source["project"], "source_revision": source["id"],
                       "source_result": str(pointer), "source_result_sha256": digest(pointer),
                       "source_hashes": verifier.tree_hashes(source_folder, {}), "source_files": source["files"],
                       "target_files": target["files"], "source_sha256": source["files"][source["board"]],
                       "target_sha256": target["files"][target["board"]],
                       "source_constraints_sha256": source["constraints_hash"], "target_constraints_sha256": target["constraints_hash"],
                       "source_verification": state.checks[source["id"]], "target_verification": state.checks[target["id"]],
                       "verification": state.checks[target["id"]]}
        path = tmp_path / f"publication-{i}.json"
        write_json(path, publication)
        state.publications.append(path)
    engine = SimpleNamespace(_verified=lambda p, r: (deepcopy(state.data[r]), state.folders[r]),
                             inspect_revision=lambda p, r: deepcopy(state.inspections[r]))
    monkeypatch.setattr(verifier, "local_engine", lambda workspace: engine)
    monkeypatch.setattr(verifier.catalog, "verification", lambda e, p, r: deepcopy(state.checks[r]))
    monkeypatch.setattr(verifier, "build_inventory", lambda e, p, r: deepcopy(state.inventories[r]))
    for helper, name in (("_report", "drc.json"), ("_erc_report", "erc.json")):
        monkeypatch.setattr(verifier.publication_preservation, helper,
                            lambda e, p, r, c, name=name: read_json(state.folders[r] / "verification" / c["verification_id"] / name))
    schema = {"properties": {"options": {"anyOf": [{"$ref": "#/$defs/CompletionOptions"}, {"type": "null"}]}},
              "$defs": {"CompletionOptions": {"properties": {"repair_cycles": {
                  "type": "integer", "default": 1, "minimum": 1, "maximum": 3}}}}}
    state.tools = [Model({"name": name, "inputSchema": {}, "annotations": {"readOnlyHint": True}})
                   for name in verifier.READ_TOOLS]
    state.tools.append(Model({"name": verifier.COMPLETION, "inputSchema": schema, "annotations": {"readOnlyHint": False}}))

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            return Model({"protocolVersion": "mock", "serverInfo": {"name": "synthetic"}})

        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=state.tools[:2] if cursor is None else state.tools[2:],
                                   nextCursor="page2" if cursor is None else None)

        async def call_tool(self, name, arguments):
            state.calls.append((name, arguments))
            state.hook(name)
            if name == "list_pcb_revisions":
                value = [deepcopy(d) for d in state.data.values() if d["project"] == arguments["project"]]
            elif name == "inspect_pcb_revision":
                value = deepcopy(state.inspections[arguments["revision"]])
            elif name == "inspect_pcb_inventory":
                value = deepcopy(state.inventories[arguments["revision"]])
            else:
                pytest.fail("Unapproved MCP call: " + name)
            state.response_hook(name, value)
            if state.encoding == "structured":
                structured = {"result": value} if name == "list_pcb_revisions" else value
                return SimpleNamespace(isError=state.tool_error, structuredContent=structured, content=[])
            text_values = value if state.encoding == "text-items" and isinstance(value, list) else [value]
            return SimpleNamespace(isError=state.tool_error, structuredContent=None,
                                   content=[SimpleNamespace(type="text", text=json.dumps(v)) for v in text_values])

    state.response_hook = lambda name, value: None
    client = Client()

    @asynccontextmanager
    async def stdio(parameters):
        state.started += 1
        assert parameters.command == config["command"] and parameters.args == config["args"]
        assert Path(parameters.cwd) == root
        assert parameters.env["PCB_WEAVER_WORKSPACE"] == str(workspace)
        yield object(), object()

    monkeypatch.setattr(verifier, "stdio_client", stdio)
    monkeypatch.setattr(verifier, "ClientSession", lambda *a: client)
    return SimpleNamespace(state=state, root=root, client=client, args=SimpleNamespace(
        root=root, publication=state.publications, output=tmp_path / "new-mcp-result.json"))


@pytest.mark.parametrize("encoding", ["structured", "text", "text-items"])
def test_mock_multiple_publications_read_only_contract_and_hash_record(setup, encoding):
    setup.state.encoding = encoding
    config_before = (setup.root / ".mcp.json").read_bytes()
    result = asyncio.run(verifier.run(setup.args))
    assert result["status"] == "passed" and setup.state.started == 1
    assert [name for name, args in setup.state.calls] == list(verifier.READ_TOOLS) * 2
    assert len(result["calls"]) == 6 and all(c["response_sha256"] for c in result["calls"])
    assert all(p["status"] == "passed" and p["inventory_summary"]["component_count"] == 1 for p in result["publications"])
    assert result["wholeboard_phase"] == "not_evaluated" and result["native_verification_executed"] is False
    assert result["job_writes_requested"] is False and result["repair_cycles_schema"]["maximum"] == 3
    assert str(setup.root / ".mcp.json") in result["observed_sha256"]
    assert (setup.root / ".mcp.json").read_bytes() == config_before
    assert read_json(setup.args.output) == result


@pytest.mark.parametrize("mode", ["valid", "missing", "tampered", "old_schema", "raw_forged", "recompute_fails"])
def test_mock_v3_proof_must_be_recomputed_before_mcp(setup, monkeypatch, mode):
    """Mock proof calculator here; real byte-pinned proof has separate synthetic tests."""
    state = setup.state
    path = state.publications[0]
    record = read_json(path)
    source_fp, target_fp = {"erc": "synthetic-source"}, {"erc": "synthetic-target"}
    proof = {"rule": "synthetic-proof", "structural_objects": ["synthetic witness"], "artifacts": ["bound"]}
    record.update(schema_version=3, source_finding_fingerprints=source_fp,
                  target_finding_fingerprints=target_fp, finding_equivalence=deepcopy(proof))
    calls = []
    def fingerprints(engine, project, revision, check):
        return source_fp if revision.startswith("r-source") else target_fp
    def recompute(*args):
        calls.append(args)
        assert args[0] == state.folders[record["source_revision"]]
        assert args[1] == state.folders[record["revision"]]
        assert args[2] == state.checks[record["source_revision"]]
        assert args[3] == state.checks[record["revision"]]
        if mode == "recompute_fails":
            raise ValueError("Synthetic structural proof failed")
        return deepcopy(proof)
    monkeypatch.setattr(verifier.publication_preservation, "finding_fingerprints", fingerprints)
    monkeypatch.setattr(verifier.publication_preservation, "finding_equivalence", recompute)
    if mode == "missing":
        record.pop("finding_equivalence")
    elif mode == "tampered":
        record["finding_equivalence"]["structural_objects"] = ["forged witness"]
    elif mode == "old_schema":
        record["schema_version"] = 2
    elif mode == "raw_forged":
        record["target_finding_fingerprints"] = source_fp
    write_json(path, record)
    if mode == "valid":
        result = verifier.local_publication(path, setup.root / "data", {})
        assert result["finding_equivalence"] == proof and len(calls) == 1
    else:
        with pytest.raises(ValueError):
            verifier.local_publication(path, setup.root / "data", {})
    assert state.started == 0 and state.calls == []


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_mock_exact_fingerprint_records_remain_compatible(setup, schema):
    path = setup.state.publications[0]
    r = read_json(path)
    helper = verifier.publication_preservation
    fp = helper.finding_fingerprints(None, r["source_project"], r["source_revision"], r["source_verification"])
    r.update(schema_version=schema, source_finding_fingerprints=fp, target_finding_fingerprints=fp)
    write_json(path, r)
    result = verifier.local_publication(path, setup.root / "data", {})
    assert result["finding_comparison"] == "raw_equal" and result["finding_equivalence"] is None


def test_mock_legacy_record_cannot_hide_same_count_different_findings(setup):
    state = setup.state
    r = read_json(state.publications[0])
    check = state.checks[r["revision"]]
    artifact = state.folders[r["revision"]] / "verification" / check["verification_id"] / "erc.json"
    report = read_json(artifact)
    report["sheets"][0]["violations"][0]["description"] = "different warning, same count"
    write_json(artifact, report)
    check["evidence_hashes"]["erc.json"] = digest(artifact)
    r["target_verification"] = r["verification"] = check
    write_json(state.publications[0], r)
    with pytest.raises(ValueError, match="requires a v3"):
        verifier.local_publication(state.publications[0], setup.root / "data", {})
    assert state.started == 0


@pytest.mark.parametrize("tool,change", [
    ("list_pcb_revisions", "missing"), ("list_pcb_revisions", "duplicate"),
    ("inspect_pcb_revision", "board"), ("inspect_pcb_revision", "constraints"),
    ("inspect_pcb_inventory", "count"), ("inspect_pcb_inventory", "sha"),
    ("inspect_pcb_inventory", "verification"),
])
def test_mock_remote_mismatch_fails_without_write_tool_calls(setup, tool, change):
    def mutate(name, value):
        if name != tool:
            return
        if change == "missing":
            value.clear()
        elif change == "duplicate":
            value.append(deepcopy(value[0]))
        elif change == "board":
            value["board"]["tracks"] = 9
        elif change == "constraints":
            value["constraints"]["synthetic"] = "wrong"
        elif change == "count":
            value["summary"]["component_count"] = 999
        elif change == "sha":
            value["sources"]["board"]["sha256"] = "wrong"
        else:
            value["sources"]["verification"]["status"] = "blocked"

    setup.state.response_hook = mutate
    with pytest.raises(ValueError, match="mismatch"):
        asyncio.run(verifier.run(setup.args))
    result = read_json(setup.args.output)
    assert result["status"] == "failed" and result["publications"][0]["status"] == "failed"
    assert all(name in verifier.READ_TOOLS for name, args in setup.state.calls)


@pytest.mark.parametrize("field,value", [("default", 2), ("maximum", 4), ("minimum", 0), ("type", "number")])
def test_mock_wrong_repair_cycles_schema_fails_before_tools(setup, field, value):
    setup.state.tools[-1].data["inputSchema"]["$defs"]["CompletionOptions"]["properties"]["repair_cycles"][field] = value
    with pytest.raises(ValueError, match="repair_cycles"):
        asyncio.run(verifier.run(setup.args))
    assert not setup.state.calls and read_json(setup.args.output)["status"] == "failed"


@pytest.mark.parametrize("change", ["blocked", "catalog", "source", "constraints", "inventory", "pointer"])
def test_mock_local_publication_must_validate_before_starting_client(setup, change):
    state = setup.state
    path = state.publications[0]
    publication = read_json(path)
    if change == "blocked":
        publication["engineering_status"] = "blocked"
        write_json(path, publication)
    elif change == "catalog":
        state.checks["r-target-0"]["status"] = "invalid_evidence"
    elif change == "source":
        (state.folders["r-source-0"] / "design/board.kicad_pcb").write_bytes(b"tampered")
    elif change == "constraints":
        (state.folders["r-target-0"] / "constraints.json").write_bytes(b"{}")
    elif change == "inventory":
        state.inventories["r-source-0"]["summary"]["net_count"] = 999
    else:
        Path(publication["source_result"]).write_bytes(b"changed pointer")
    with pytest.raises(ValueError):
        asyncio.run(verifier.run(setup.args))
    assert state.started == 0 and not state.calls


def test_mock_existing_output_is_never_overwritten(setup):
    setup.args.output.write_bytes(b"keep existing evidence")
    with pytest.raises(ValueError, match="NEW output"):
        asyncio.run(verifier.run(setup.args))
    assert setup.args.output.read_bytes() == b"keep existing evidence" and setup.state.started == 0


def test_mock_output_reservation_race_does_not_start_client(setup, monkeypatch):
    original = Path.open

    def raced(path, mode="r", *args, **kwargs):
        if path == setup.args.output and mode == "x":
            path.write_bytes(b"concurrent reservation")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", raced)
    with pytest.raises(FileExistsError):
        asyncio.run(verifier.run(setup.args))
    assert setup.state.started == 0 and setup.args.output.read_bytes() == b"concurrent reservation"


@pytest.mark.parametrize("name", ["verify_pcb_revision", "export_pcb_review_report", "submit_pcb_completion"])
def test_mock_call_allowlist_refuses_writes(setup, name):
    with pytest.raises(ValueError, match="three publication inspection tools"):
        asyncio.run(verifier.call_read(setup.client, name, {}, []))
    assert not setup.state.calls


@pytest.mark.parametrize("change", ["config", "code", "native_evidence"])
def test_mock_changes_during_client_inspection_fail_closed(setup, change):
    changed = False

    def hook(name):
        nonlocal changed
        if changed:
            return
        changed = True
        path = {"config": setup.root / ".mcp.json", "code": setup.root / "src/pcb_weaver/__init__.py",
                "native_evidence": setup.state.folders["r-target-0"] / "verification/v-target-0/drc.json"}[change]
        path.write_bytes(path.read_bytes() + b"\n")

    setup.state.hook = hook
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(verifier.run(setup.args))
    assert read_json(setup.args.output)["status"] == "failed"


def test_mock_duplicate_publications_do_not_inflate_acceptance(setup):
    setup.args.publication.append(setup.args.publication[0])
    with pytest.raises(ValueError, match="Duplicate publication"):
        asyncio.run(verifier.run(setup.args))
    assert setup.state.started == 0


def test_mock_tool_error_is_not_an_empty_success(setup):
    setup.state.tool_error = True
    with pytest.raises(ValueError, match="MCP tool error"):
        asyncio.run(verifier.run(setup.args))
    assert read_json(setup.args.output)["status"] == "failed"


def test_mock_cli_accepts_repeated_publication_arguments(setup):
    verifier.main(["--root", str(setup.root), "--publication", str(setup.args.publication[0]),
                   "--publication", str(setup.args.publication[1]), "--output", str(setup.args.output)])
    assert len(read_json(setup.args.output)["publications"]) == 2


def test_readonly_ledger_adapter_cannot_write_or_initialize(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE marker (value TEXT)")
        db.execute("INSERT INTO marker VALUES ('synthetic')")
    before = ledger.read_bytes()
    store = verifier.ReadOnlyStore(tmp_path)
    with store.connect() as db:
        assert db.execute("SELECT value FROM marker").fetchone()[0] == "synthetic"
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO marker VALUES ('forbidden')")
    assert ledger.read_bytes() == before
    missing = tmp_path / "empty"
    missing.mkdir()
    with pytest.raises(ValueError, match="Existing publication ledger"):
        verifier.ReadOnlyStore(missing)
    assert not list(missing.iterdir())
