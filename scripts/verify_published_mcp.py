"""Check published reviews through the saved official MCP stdio configuration.

Only list_pcb_revisions, inspect_pcb_revision and inspect_pcb_inventory are called.
Existing native evidence is authenticated locally, never regenerated. This checks
publication visibility and the client contract, not whole-board phase acceptance.
"""
import argparse
import asyncio
from contextlib import closing, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pcb_weaver
from pcb_weaver import catalog
from pcb_weaver.inventory import build_inventory
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import Store, canonical, write_json


_spec = importlib.util.spec_from_file_location("publication_preservation", Path(__file__).with_name("publish_completion_review.py"))
publication_preservation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publication_preservation)


READ_TOOLS = ("list_pcb_revisions", "inspect_pcb_revision", "inspect_pcb_inventory")
COMPLETION = "submit_pcb_completion"
TIMEOUT = 120


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checksum(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def watch(path, observed):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    require(path not in observed or observed[path] == sha, "Evidence/code/config changed: " + str(path))
    observed[path] = sha
    return raw, sha


def tree_hashes(folder, observed, pattern="*"):
    return {p.relative_to(folder).as_posix(): watch(p, observed)[1]
            for p in sorted(folder.rglob(pattern)) if p.is_file() and "__pycache__" not in p.parts}


class ReadOnlyStore(Store):
    """Reuse ledger authentication without Store's schema or WAL initialization."""
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        require((self.root / "ledger.sqlite3").is_file(), "Existing publication ledger is required")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect((self.root / "ledger.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            yield db


def local_engine(workspace):
    # These inspection methods need only a store; no native toolchain is constructed.
    engine = object.__new__(EngineeringService)
    engine.store = ReadOnlyStore(workspace)
    return engine


def native_summary(check, data):
    require(check.get("status") == "passed" and check.get("revision") == data["id"]
            and check.get("revision_digest") == data["digest"], "Native catalog verification is not a bound pass")
    summary = {"status": check["status"], "verification_id": check["verification_id"]}
    for kind, fields in (("drc", ("errors", "unconnected", "warnings")), ("erc", ("errors", "warnings"))):
        require(check[kind].get("status") == "ok", "Native report unavailable: " + kind)
        summary[kind] = {field: check[kind].get(field) for field in fields}
        require(all(type(v) is int and v >= 0 for v in summary[kind].values()), "Invalid native counts")
    require(summary["drc"]["errors"] == summary["drc"]["unconnected"] == summary["erc"]["errors"] == 0,
            "Native errors or missing connections remain")
    require(check["drc"].get("source_sha256") == data["files"][data["board"]], "Native board SHA mismatch")
    require(check.get("connectivity", {}).get("status") == "passed", "Native connectivity did not pass")
    summary["connectivity_status"] = "passed"
    return summary


def local_publication(path, workspace, observed):
    raw, sha = watch(path, observed)
    record = json.loads(raw)
    require(record.get("status") == "published_review" and record.get("engineering_status") == "passed"
            and record.get("preservation_checks_passed") is True and record.get("source_immutable") is True,
            "Publication is not a preserved, passed review")
    require(record.get("manufacturing_authorized") is False and record.get("wholeboard_benchmark") is False,
            "Unexpected publication scope")
    require(Path(record["workspace"]).resolve(strict=True) == workspace, "Publication is not in default desktop data")
    project, revision = Store.identifier(record["project"]), Store.identifier(record["revision"])
    source_workspace = Path(record["source_workspace"]).resolve(strict=True)
    source = local_engine(source_workspace)
    source_data, source_folder = source._verified(record["source_project"], record["source_revision"])
    require(tree_hashes(source_folder, observed) == record["source_hashes"], "Published source checkpoint changed")
    require(watch(record["source_result"], observed)[1] == record["source_result_sha256"], "Source result changed")
    source_check = catalog.verification(source, record["source_project"], record["source_revision"])
    native_summary(source_check, source_data)
    require(source_check == record["source_verification"], "Publication source verification changed")
    engine = local_engine(workspace)
    data, folder = engine._verified(project, revision)
    require(data["project"] == project and data["id"] == revision, "Local target revision identity mismatch")
    files = tree_hashes(folder / "design", observed)
    require(files == data["files"] == record["target_files"] == record["source_files"] == source_data["files"],
            "Published target/source companion hashes differ")
    require(data["board"] == source_data["board"] and files[data["board"]] == record["target_sha256"]
            == record["source_sha256"], "Published target board SHA mismatch")
    constraints_raw, constraints_sha = watch(folder / "constraints.json", observed)
    require(constraints_sha == data["constraints_hash"] == source_data["constraints_hash"]
            == record["target_constraints_sha256"] == record["source_constraints_sha256"],
            "Published constraints SHA mismatch")
    check = catalog.verification(engine, project, revision)
    native = native_summary(check, data)
    require(check == record["target_verification"] == record["verification"], "Published target verification changed")
    for name, expected in check["evidence_hashes"].items():
        artifact = catalog.artifact_path(folder, folder / "verification" / check["verification_id"] / name)
        require(watch(artifact, observed)[1] == expected, "Native evidence artifact changed")
    require(canonical(publication_preservation.verification_outcome(source_check, source_data))
            == canonical(publication_preservation.verification_outcome(check, data)),
            "Published native finding counts/connectivity differ")
    source_fp = publication_preservation.finding_fingerprints(source, record["source_project"], record["source_revision"], source_check)
    target_fp = publication_preservation.finding_fingerprints(engine, project, revision, check)
    for side, fp in (("source", source_fp), ("target", target_fp)):
        field = side + "_finding_fingerprints"
        require((field not in record and record.get("schema_version", 1) == 1) or record.get(field) == fp,
                "Publication raw finding fingerprints differ from artifacts")
    proof = None
    if source_fp != target_fp:
        require(record.get("schema_version") == 3 and isinstance(record.get("finding_equivalence"), dict),
                "Raw finding mismatch requires a v3 representative proof")
        proof = publication_preservation.finding_equivalence(source_folder, folder, source_check, check, source_fp, target_fp)
    require(record.get("finding_equivalence") == proof, "Publication representative proof mismatch")
    inspection = engine.inspect_revision(project, revision)
    require(inspection["revision"] == data and inspection["constraints"] == json.loads(constraints_raw),
            "Local revision inspection changed")
    inventory = build_inventory(engine, project, revision)
    source_inventory = build_inventory(source, record["source_project"], record["source_revision"])
    require(inventory["summary"] == source_inventory["summary"], "Published source/target inventory counts differ")
    inventory = {k: inventory[k] for k in ("schema_version", "project", "revision", "revision_digest",
                                         "units", "summary", "coverage", "sources")}
    return {"publication": str(Path(path).resolve()), "publication_sha256": sha, "project": project,
            "revision": revision, "source_workspace": str(source_workspace), "data": data,
            "finding_comparison": "raw_equal" if proof is None else proof["rule"],
            "finding_equivalence": proof,
            "inspection": inspection, "inventory": inventory, "native": native,
            "source_sha256": record["source_sha256"], "target_sha256": record["target_sha256"],
            "constraints_sha256": constraints_sha, "source_result_sha256": record["source_result_sha256"]}


def validate_contract(contracts):
    for name in READ_TOOLS:
        require(contracts[name].get("annotations", {}).get("readOnlyHint") is True,
                "Tool is not advertised as read-only: " + name)
    schema = contracts[COMPLETION]["inputSchema"]
    options = schema["properties"]["options"]
    choices = options.get("anyOf", [options])
    require(any(c.get("$ref") == "#/$defs/CompletionOptions" for c in choices), "Completion options schema is not linked")
    definition = schema["$defs"]["CompletionOptions"]
    field = definition["properties"]["repair_cycles"]
    require(field.get("type") == "integer" and type(field.get("default")) is int
            and field["default"] == field.get("minimum") == 1 and field.get("maximum") == 3
            and "repair_cycles" not in definition.get("required", []), "Unexpected repair_cycles contract")
    return field


async def call_read(client, name, arguments, calls):
    require(name in READ_TOOLS, "Only the three publication inspection tools may be called")
    response = await asyncio.wait_for(client.call_tool(name, arguments), TIMEOUT)
    require(not response.isError, "MCP tool error: " + name)
    value = getattr(response, "structuredContent", None)
    if value is not None:
        if name == "list_pcb_revisions" and isinstance(value, dict) and set(value) == {"result"}:
            value = value["result"]
    else:
        texts = [json.loads(c.text) for c in response.content if c.type == "text"]
        require(bool(texts), "MCP tool returned no JSON: " + name)
        value = texts[0] if len(texts) == 1 else texts
        if name == "list_pcb_revisions" and isinstance(value, dict):
            value = [value]
    calls.append({"tool": name, "arguments": arguments, "response_sha256": checksum(value)})
    return value


async def run(args):
    root = Path(args.root).resolve(strict=True)
    output = Path(args.output).absolute()
    require(not output.exists() and not output.is_symlink(), "Use a NEW output path; never overwrite evidence")
    observed = {}
    raw, _ = watch(root / ".mcp.json", observed)
    config = json.loads(raw)["mcpServers"]["pcb-weaver"]
    cwd = (root / config.get("cwd", ".")).resolve(strict=True)
    env = {**os.environ, **config.get("env", {})}
    workspace = (cwd / env["PCB_WEAVER_WORKSPACE"]).resolve(strict=True)
    require(workspace == (root / "data").resolve(strict=True), "MCP config must select existing default data")
    config_path = (cwd / env["PCB_WEAVER_CONFIG"]).resolve(strict=True)
    watch(config_path, observed)
    watch(Path(__file__), observed)
    watch(Path(publication_preservation.__file__), observed)
    watch(cwd / config["command"], observed)
    code_roots = {"local": Path(pcb_weaver.__file__).resolve().parent, "desktop": root / "src/pcb_weaver"}
    code_hashes = {name: tree_hashes(folder, observed, "*.py") for name, folder in code_roots.items()}
    require(all(code_hashes.values()), "Implementation code inventory is missing")
    publications = [local_publication(path, workspace, observed) for path in args.publication]
    require(bool(publications), "At least one publication is required")
    require(len({(p["project"], p["revision"]) for p in publications}) == len(publications), "Duplicate publication target")
    for folder in [workspace, *(Path(p["source_workspace"]) for p in publications)]:
        require(not output.resolve().is_relative_to(folder), "Output must be outside publication data workspaces")
    result = {"schema_version": 1, "status": "running", "scope": __doc__, "transport": "Official MCP SDK stdio",
              "wholeboard_phase": "not_evaluated", "manufacturing_authorized": False,
              "native_verification_executed": False, "job_writes_requested": False, "calls": [],
              "launch": {"command": config["command"], "args": config.get("args", []), "cwd": str(cwd),
                         "configured_environment_sha256": checksum(config.get("env", {}))},
              "implementation_sha256": code_hashes, "publications": []}
    for publication in publications:
        row = {k: v for k, v in publication.items() if k not in ("data", "inspection", "inventory")}
        result["publications"].append({**row, "status": "pending"})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
    try:
        parameters = StdioServerParameters(command=config["command"], args=config.get("args", []), env=env, cwd=cwd)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await asyncio.wait_for(client.initialize(), TIMEOUT)
                result["initialize"] = initialized.model_dump(mode="json")
                contracts, cursor, cursors = {}, None, set()
                while True:
                    page = await asyncio.wait_for(client.list_tools(cursor=cursor), TIMEOUT)
                    for tool in page.tools:
                        require(tool.name not in contracts, "Duplicate advertised tool: " + tool.name)
                        contracts[tool.name] = tool.model_dump(mode="json")
                    cursor = page.nextCursor
                    if not cursor:
                        break
                    require(cursor not in cursors, "Repeated MCP tool cursor")
                    cursors.add(cursor)
                result["tool_contracts"] = {name: contracts[name] for name in (*READ_TOOLS, COMPLETION)}
                result["repair_cycles_schema"] = validate_contract(result["tool_contracts"])
                for expected, row in zip(publications, result["publications"]):
                    row["status"] = "checking"
                    arguments = {"project": expected["project"], "revision": expected["revision"]}
                    revisions = await call_read(client, "list_pcb_revisions", {"project": arguments["project"]}, result["calls"])
                    require(isinstance(revisions, list), "Revision listing is not an array")
                    matches = [r for r in revisions if r.get("id") == arguments["revision"]]
                    require(len(matches) == 1 and canonical(matches[0]) == canonical(expected["data"]), "MCP revision listing mismatch")
                    row["revision_count"] = len(revisions)
                    inspection = await call_read(client, "inspect_pcb_revision", arguments, result["calls"])
                    require(checksum(inspection) == checksum(expected["inspection"]), "MCP revision/board/constraints mismatch")
                    inventory = await call_read(client, "inspect_pcb_inventory", {**arguments, "section": "summary"}, result["calls"])
                    require(checksum(inventory) == checksum(expected["inventory"]), "MCP inventory counts or evidence binding mismatch")
                    row.update(status="passed", inventory_summary=inventory["summary"], inventory_sources=inventory["sources"])
        for expected in publications:
            require(local_publication(expected["publication"], workspace, observed) == expected, "Publication changed during MCP inspection")
        for name, folder in code_roots.items():
            require(tree_hashes(folder, observed, "*.py") == code_hashes[name], "Implementation file set changed")
        for path in list(observed):
            watch(path, observed)
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", error=type(error).__name__ + ": " + str(error))
        for row in result["publications"]:
            if row["status"] == "checking":
                row["status"] = "failed"
        raise
    finally:
        result["observed_sha256"] = {str(path): sha for path, sha in observed.items()}
        write_json(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--publication", type=Path, nargs="+", action="extend", required=True)
    parser.add_argument("--output", type=Path, required=True)
    result = asyncio.run(run(parser.parse_args(argv)))
    print(json.dumps({"status": result["status"], "publications": len(result["publications"]),
                      "wholeboard_phase": result["wholeboard_phase"]}))


if __name__ == "__main__":
    main()
