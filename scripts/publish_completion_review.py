"""Import an isolated checkpoint into a new default-workspace review project.

Publication preserves bytes and verification outcomes; it is not whole-board
benchmark acceptance. Blocked checkpoints require --allow-blocked. Failed attempts
retain their output record and any reserved project; retry with new names.
"""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import sexpdata
from pcb_weaver import catalog
from pcb_weaver.repair import _report, _erc_report, _violations, _erc_violations
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import Store, canonical, read_json, write_json, digest


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_hashes(folder):
    hashes = {}
    for path in sorted(folder.rglob("*")):
        require(not path.is_symlink() and not (hasattr(path, "is_junction") and path.is_junction()),
                "Linked snapshot files are not supported")
        if path.is_file():
            hashes[path.relative_to(folder).as_posix()] = digest(path)
    return hashes


def revision_snapshot(engine, project, revision):
    data, folder = engine._verified(project, revision)
    require(data["project"] == project and data["id"] == revision, "Revision identity mismatch")
    files = file_hashes(folder / "design")
    require(files == data["files"] and data["board"] in files, "Revision design bytes differ from manifest")
    constraints_sha = digest(folder / "constraints.json")
    require(constraints_sha == data["constraints_hash"], "Revision constraints differ from manifest")
    return data, folder, {"files": files, "constraints_sha256": constraints_sha,
                          "constraints": read_json(folder / "constraints.json")}


def verification_outcome(check, data):
    require(check.get("status") in ("passed", "blocked"), "Authentic passed or blocked verification is required")
    require(check.get("revision") == data["id"] and check.get("revision_digest") == data["digest"],
            "Verification revision binding mismatch")
    result = {"status": check["status"]}
    for kind, fields in (("drc", ("errors", "unconnected", "warnings")), ("erc", ("errors", "warnings"))):
        native = check.get(kind, {})
        require(native.get("status") == "ok", "Native " + kind.upper() + " report is unavailable")
        result[kind] = {}
        for field in fields:
            value = native.get(field)
            require(type(value) is int and value >= 0, "Missing or invalid native " + kind + "." + field)
            result[kind][field] = value
    require(check["drc"].get("source_sha256") == data["files"][data["board"]],
            "Native DRC board SHA does not match revision")
    connectivity = check.get("connectivity")
    require(isinstance(connectivity, dict) and connectivity.get("status") in ("passed", "failed", "blocked"),
            "Connectivity evidence is unavailable")
    # Netlist operation logs contain host paths/timings; retain all electrical comparison fields.
    result["connectivity"] = {k: v for k, v in connectivity.items() if k != "operation"}
    if result["status"] == "passed":
        require(result["drc"]["errors"] == result["drc"]["unconnected"] == result["erc"]["errors"] == 0
                and connectivity["status"] == "passed", "Passed verification has inconsistent native findings")
    return result


def finding_fingerprints(engine, project, revision, check):
    """Read authenticated artifacts and retain the core finding identities and multiplicity."""
    drc = _report(engine, project, revision, check)
    erc = _erc_report(engine, project, revision, check)

    def entries(counts):
        return [{"fingerprint": json.loads(canonical(key)), "count": counts[key]}
                for key in sorted(counts, key=canonical)]

    return {"drc": {section: entries(_violations({"violations": drc[section]}))
                    for section in ("violations", "unconnected_items", "schematic_parity")},
            "erc": entries(_erc_violations(erc))}


def _children(node, name):
    return [x for x in node if isinstance(x, list) and x and str(x[0]) == name]


def _one(node, name):
    matches = _children(node, name)
    require(len(matches) == 1, "Missing or ambiguous schematic field: " + name)
    return matches[0][1:]


def _walk(node):
    if isinstance(node, list):
        yield node
        for child in node:
            yield from _walk(child)


def _proof_sheet(design, files, root_sha, sheet_path):
    roots = [name for name, sha in files.items() if name.endswith(".kicad_sch") and sha == root_sha]
    require(len(roots) == 1, "ERC source SHA must resolve one frozen root schematic")
    parts = sheet_path.split("/")
    require(parts[0] == "" and len(parts) >= 2 and all(parts[1:]), "Invalid ERC sheet instance path")
    name, chain = roots[0], []
    for depth, uuid in enumerate(parts[1:]):
        require(name in files, "Sheet is outside the frozen design manifest")
        tree = sexpdata.loads((design / name).read_text(encoding="utf-8"))
        require(str(tree[0]) == "kicad_sch" and _one(tree, "version") == [20250114],
                "Unsupported schematic format for global power proof")
        if depth == 0:
            require(_one(tree, "uuid") == [uuid], "ERC root sheet UUID mismatch")
        chain.append({"file": name, "sha256": files[name]})
        if depth + 2 == len(parts):
            return tree, chain
        sheets = [s for s in _children(tree, "sheet") if _one(s, "uuid") == [parts[depth + 2]]]
        require(len(sheets) == 1, "Ambiguous sheet instance")
        names = [p[2] for p in _children(sheets[0], "property") if p[1] == "Sheetfile"]
        require(len(names) == 1, "Missing or ambiguous sheet filename")
        path = (design / name).parent / names[0]
        require(path.resolve().is_relative_to(design.resolve()), "Sheet escapes frozen design")
        chain[-1]["child_instance"] = sexpdata.dumps(sheets[0])
        name = path.resolve().relative_to(design.resolve()).as_posix()


def _global_power_pin(tree, uuid, sheet_path):
    # Only a single hidden origin pin on a literal, embedded KiCad global power
    # symbol is supported. Ordinary hidden pins, local power, inheritance and
    # multi-unit/conversion definitions intentionally have no fallback.
    hits = [n for n in _walk(tree) if _children(n, "uuid") and _one(n, "uuid") == [uuid]]
    require(len(hits) == 1 and str(hits[0][0]) == "pin", "Representative UUID is not a unique pin")
    owners = [s for s in _children(tree, "symbol") if any(p is hits[0] for p in _children(s, "pin"))]
    require(len(owners) == 1, "Representative pin has no unique symbol owner")
    symbol = owners[0]
    require(_one(symbol, "unit") == [1] and len(_children(symbol, "pin")) == 1
            and not _children(symbol, "convert") and not _children(symbol, "extends")
            and not _children(symbol, "net_name"), "Unsupported power instance")
    for flag in ("dnp", "exclude_from_sim"):
        require(not _children(symbol, flag) or _one(symbol, flag) == [sexpdata.Symbol("no")],
                "Inactive power instance")
    lib_id = _one(symbol, "lib_id")[0]
    libraries = [s for s in _children(_one(tree, "lib_symbols"), "symbol") if s[1] == lib_id]
    require(len(libraries) == 1, "Missing or ambiguous embedded power definition")
    lib = libraries[0]
    require(_children(lib, "power") == [[sexpdata.Symbol("power")]] and not _children(lib, "extends"),
            "Symbol does not prove implicit global power semantics")
    pins = [p for n in _children(lib, "symbol") for p in _children(n, "pin")]
    require(len(pins) == 1, "Only single-pin power definitions are supported")
    pin = pins[0]
    units = [n for n in _children(lib, "symbol") if _children(n, "pin")]
    require(units[0][1] == lib_id.split(":")[-1] + "_1_1", "Unsupported power unit/conversion")
    require(pin[1:3] == [sexpdata.Symbol("power_in"), sexpdata.Symbol("line")]
            and _one(pin, "hide") == [sexpdata.Symbol("yes")]
            and _one(pin, "at")[:2] == [0, 0] and _one(pin, "length") == [0],
            "Pin does not prove hidden global power semantics")
    number, name = _one(pin, "number")[0], _one(pin, "name")[0]
    require(hits[0][1] == number and isinstance(name, str) and name not in ("", "~")
            and "${" not in name, "Ambiguous power pin name/number")
    values = [p[2] for p in _children(symbol, "property") if p[1] == "Value"]
    refs = [p[2] for p in _children(symbol, "property") if p[1] == "Reference"]
    require(values == [name] and len(refs) == 1, "Power value/name or reference mismatch")
    paths = [p for n in _children(symbol, "instances") for p in _walk(n)
             if p and str(p[0]) == "path" and p[1] == sheet_path]
    require(len(paths) == 1 and _one(paths[0], "unit") == [1]
            and _one(paths[0], "reference") == refs, "Power sheet instance binding is ambiguous")
    definition = sexpdata.dumps(lib)
    return {"uuid": uuid, "symbol_uuid": _one(symbol, "uuid")[0], "reference": refs[0],
            "lib_id": lib_id, "pin_number": number, "global_net_name": name,
            "definition_sha256": hashlib.sha256(definition.encode("utf-8")).hexdigest(),
            "definition": definition, "pin_definition": sexpdata.dumps(pin), "instance": sexpdata.dumps(symbol),
            "semantics": "embedded global power; single hidden power_in origin pin; literal value equals pin name"}


def finding_equivalence(source_folder, target_folder, source_check, target_check, source_fp, target_fp):
    """Recompute a narrow proof from immutable bytes and already authenticated checks.

    Callers must authenticate both checks against their catalogs. Exact matches
    need no exception. Different or unsupported findings remain failures.
    """
    if source_fp == target_fp:
        return None
    message = "Target native DRC/ERC finding fingerprints differ from source"
    require(source_fp["drc"] == target_fp["drc"], message)
    left = [x for x in source_fp["erc"] if x not in target_fp["erc"]]
    right = [x for x in target_fp["erc"] if x not in source_fp["erc"]]
    require(len(left) == len(right) == 1, message)
    a, b = left[0]["fingerprint"], right[0]["fingerprint"]
    require(a[:4] == b[:4] and a[1:3] == ["multiple_net_names", "warning"]
            and type(left[0]["count"]) is int and left[0]["count"] > 0
            and left[0]["count"] == right[0]["count"] and len(a[4]) == len(b[4]) == 2,
            message)
    common, removed, added = set(a[4]) & set(b[4]), set(a[4]) - set(b[4]), set(b[4]) - set(a[4])
    require(len(common) == len(removed) == len(added) == 1, message)
    require(all(c["status"] == "passed" and c["connectivity"]["status"] == "passed"
                and c["drc"]["errors"] == c["drc"]["unconnected"] == c["erc"]["errors"] == 0
                for c in (source_check, target_check)), "Representative proof requires native passed checks")
    source_files, target_files = file_hashes(source_folder / "design"), file_hashes(target_folder / "design")
    require(source_files == target_files
            and digest(source_folder / "constraints.json") == digest(target_folder / "constraints.json"),
            "Representative proof requires identical frozen design and constraints")
    root_sha = source_check["erc"].get("source_sha256")
    require(root_sha and root_sha == target_check["erc"].get("source_sha256"), "Representative ERC binding mismatch")
    tree, chain = _proof_sheet(source_folder / "design", source_files, root_sha, a[0])
    source_pin = _global_power_pin(tree, next(iter(removed)), a[0])
    target_pin = _global_power_pin(tree, next(iter(added)), a[0])
    require(all(source_pin[k] == target_pin[k] for k in ("lib_id", "pin_number", "global_net_name", "definition_sha256")),
            "Representative global power definitions differ")
    stable_uuid = next(iter(common))
    stable_objects = [n for n in _walk(tree) if _children(n, "uuid") and _one(n, "uuid") == [stable_uuid]]
    require(len(stable_objects) == 1, "Stable item UUID is ambiguous or missing")

    def artifact(folder, check, name):
        path = catalog.artifact_path(folder, folder / "verification" / check["verification_id"] / name)
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        require(sha == check["evidence_hashes"].get(name), "Representative artifact SHA mismatch: " + name)
        return raw, sha

    nets, artifacts, raw_findings = [], [], []
    for folder, check, fp, difference in ((source_folder, source_check, source_fp, a), (target_folder, target_check, target_fp, b)):
        erc_raw, erc_sha = artifact(folder, check, "erc.json")
        require(erc_sha == check["erc"].get("report_sha256"),
                "Representative ERC binding mismatch")
        report = json.loads(erc_raw)
        counts = _erc_violations(report)
        require(fp["erc"] == [{"fingerprint": json.loads(canonical(k)), "count": counts[k]}
                              for k in sorted(counts, key=canonical)], "Raw ERC fingerprint mismatch")
        matches = [v for sheet in report["sheets"] if sheet["uuid_path"] == a[0]
                   for v in sheet["violations"] if [v["type"], v["severity"], v["description"],
                       sorted(i["uuid"] for i in v["items"])] == difference[1:]]
        require(len(matches) == left[0]["count"], "Ambiguous representative warning")
        raw_findings.append(matches)
        xml, net_sha = artifact(folder, check, "netlist.xml")
        root = ET.fromstring(xml)
        net_nodes = root.findall("./nets/net")
        mapping = {net.get("name"): sorted([dict(sorted(n.attrib.items())) for n in net.findall("node")],
                                           key=canonical) for net in net_nodes}
        require(root.tag == "export" and bool(mapping) and len(mapping) == len(net_nodes)
                and all(name and nodes and all(n.get("ref") and n.get("pin") for n in nodes)
                        for name, nodes in mapping.items()),
                "Ambiguous native netlist")
        nets.append(mapping)
        artifacts.append({"verification_id": check["verification_id"], "erc_sha256": erc_sha,
                          "netlist_sha256": net_sha})
    require(nets[0] == nets[1], "Native net memberships differ")
    net_name = source_pin["global_net_name"]
    require(net_name in nets[0], "Proven global power net is absent from native netlist")
    stable = [sorted([i for v in group for i in v["items"] if i["uuid"] == stable_uuid], key=canonical)
              for group in raw_findings]
    require(stable[0] == stable[1], "Stable item changed")
    return {"rule": "embedded-global-power-representative-v1", "sheet_chain": chain, "sheet_uuid_path": a[0],
            "source_difference": left, "target_difference": right, "raw_findings": raw_findings,
            "artifacts": artifacts, "net_count": len(nets[0]), "global_net_nodes": nets[0][net_name],
            "net_memberships_sha256": hashlib.sha256(canonical(nets[0]).encode("utf-8")).hexdigest(),
            "source_pin": source_pin, "target_pin": target_pin, "stable_item": sexpdata.dumps(stable_objects[0])}


def publish_review(*, root, source_workspace, source_result, project, output, allow_blocked=False):
    """Publish a new review, preserving the source project/revision pointer contract."""
    Store.identifier(project)
    root = Path(root).resolve(strict=True)
    source_workspace = Path(source_workspace).resolve(strict=True)
    source_result = Path(source_result).resolve(strict=True)
    output = Path(output).expanduser().absolute()
    require(not output.exists() and not output.is_symlink(), "Use a new output path; never overwrite a publication")
    workspace, config = load_runtime(root / "data", root / "toolchain.unified.json")
    workspace = Path(workspace).resolve()
    require(workspace == (root / "data").resolve(), "Publication must use the root's default data workspace")
    require(not output.resolve().is_relative_to(source_workspace)
            and not output.resolve().is_relative_to(workspace / "projects"),
            "Publication output must be outside source data and target projects")
    project_dir = workspace / "projects" / project
    require(project_dir.resolve().is_relative_to(workspace), "Review project escapes default workspace")
    if project_dir.exists() or project_dir.is_symlink():
        raise ValueError("Use a new review project name")
    raw = source_result.read_bytes()
    record_sha = hashlib.sha256(raw).hexdigest()
    record = json.loads(raw)
    source_project, source_revision = Store.identifier(record["project"]), Store.identifier(record["revision"])
    result = {"schema_version": 2, "status": "publishing_review", "project": project,
              "workspace": str(workspace), "source_workspace": str(source_workspace),
              "source_project": source_project, "source_revision": source_revision,
              "source_result": str(source_result), "source_result_sha256": record_sha,
              "allow_blocked": allow_blocked, "manufacturing_authorized": False,
              "wholeboard_benchmark": False,
              "scope": "Imported review checkpoint only; not a whole-board benchmark run or phase acceptance."}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation reserves the record before any import/native work. Later writes are atomic.
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
    try:
        source = EngineeringService(source_workspace, config)
        data, folder, snapshot = revision_snapshot(source, source_project, source_revision)
        source_hashes = file_hashes(folder)
        before = catalog.verification(source, source_project, source_revision)
        result.update(source_sha256=snapshot["files"][data["board"]], source_hashes=source_hashes,
                      source_files=snapshot["files"], source_constraints_sha256=snapshot["constraints_sha256"],
                      source_verification=before)
        outcome = verification_outcome(before, data)
        require(before["status"] == "passed" or allow_blocked, "Blocked source review requires --allow-blocked")
        fingerprints = finding_fingerprints(source, source_project, source_revision, before)
        result["source_finding_fingerprints"] = fingerprints

        def unchanged_source():
            current, current_folder, current_snapshot = revision_snapshot(source, source_project, source_revision)
            require(current == data and current_folder == folder and current_snapshot == snapshot
                    and file_hashes(folder) == source_hashes and digest(source_result) == record_sha,
                    "Source checkpoint or source result changed during publication")
            require(catalog.verification(source, source_project, source_revision) == before,
                    "Source verification changed during publication")

        target = EngineeringService(workspace, config)
        require(not target.store.list_revisions(project), "Use a new review project name; ledger history exists")
        # mkdir is an atomic name reservation shared by concurrent publishers.
        project_dir.mkdir(parents=True, exist_ok=False)
        result["project_reserved"] = True
        write_json(output, result)
        imported = target.import_project(project, str(folder / "design" / data["board"]), snapshot["constraints"])
        revision = imported["revision"]["id"]
        result["revision"] = revision

        def unchanged_target():
            target_data, target_folder, target_snapshot = revision_snapshot(target, project, revision)
            target_sha = digest(target_folder / "design" / target_data["board"])
            result.update(target_sha256=target_sha, target_files=target_snapshot["files"],
                          target_constraints_sha256=target_snapshot["constraints_sha256"])
            require(target_data == imported["revision"], "Target revision manifest changed")
            require(target_data["board"] == data["board"] and target_snapshot == snapshot,
                    "Target board, companions or constraints differ from source")
            require(target_sha == result["source_sha256"],
                    "Target board SHA differs from source")
            return target_data

        target_data = unchanged_target()
        unchanged_source()
        check = target.verify_revision(project, revision)
        result.update(verification=check, target_verification=check, engineering_status=check.get("status"))
        require(canonical(verification_outcome(check, target_data)) == canonical(outcome),
                "Target native errors/missing/warnings/status/connectivity differ from source")
        result["target_finding_fingerprints"] = finding_fingerprints(target, project, revision, check)
        proof = finding_equivalence(folder, target._verified(project, revision)[1], before, check,
                                    fingerprints, result["target_finding_fingerprints"])
        if proof is not None:
            result.update(schema_version=3, finding_equivalence=proof)
        unchanged_target()
        require(catalog.verification(target, project, revision) == check, "Target verification is not ledger-bound")
        result["report"] = catalog.generate_report(target, project, revision)
        require(result["report"]["verification_status"] == check["status"], "Review report status mismatch")
        unchanged_target()
        unchanged_source()
        require(catalog.verification(target, project, revision) == check, "Target verification changed during report generation")
        result.update(status="published_review", source_immutable=True, preservation_checks_passed=True)
        write_json(output, result)
        return result
    except BaseException as error:
        result.update(status="failed", error=type(error).__name__ + ": " + str(error))
        write_json(output, result)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source_workspace", "source_result", "output"):
        parser.add_argument("--" + name.replace("_", "-"), type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--allow-blocked", action="store_true", help="Publish a blocked checkpoint as a blocked review only")
    result = publish_review(**vars(parser.parse_args(argv)))
    print({"project": result["project"], "revision": result["revision"],
           "engineering_status": result["engineering_status"], "unconnected": result["verification"]["drc"]["unconnected"]})
    return result


if __name__ == "__main__":
    main()
