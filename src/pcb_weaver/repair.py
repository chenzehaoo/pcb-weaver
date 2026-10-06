"""Bounded local copper repair using Freerouting and revision-bound native gates."""
from collections import Counter
import math
from pathlib import Path
import shutil
import uuid

import sexpdata

from . import board as pcb, catalog
from .compiler import report_rule_issues
from .storage import digest, read_json, write_json
from .toolchain import Toolchain


def validate_scope(nets, region, remove_ids, passes):
    if (not isinstance(nets, list) or not 1 <= len(nets) <= 8
            or any(not isinstance(n, str) or not n or len(n) > 1024 for n in nets)
            or len(set(nets)) != len(nets)):
        raise ValueError("Repair needs 1-8 distinct nonempty net names")
    if (not isinstance(region, (list, tuple)) or len(region) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in region)
            or region[0] >= region[2] or region[1] >= region[3]):
        raise ValueError("Repair region must be four finite ordered coordinates in mm")
    if (not isinstance(remove_ids, list) or len(remove_ids) > 1000
            or any(not isinstance(i, str) or not i or len(i) > 128 for i in remove_ids)
            or len(set(remove_ids)) != len(remove_ids)):
        raise ValueError("Explicit unique removal UUIDs are required, at most 1000")
    if type(passes) is not int or not 1 <= passes <= 10:
        raise ValueError("Repair passes must be an integer from 1 to 10")


def _net_index(path):
    ast = sexpdata.loads(Path(path).read_text(encoding="utf-8-sig"), nil=None, true=None, false=None)
    codes = {str(n[1]): str(n[2]) for n in pcb._children(ast, "net")}
    result = {}
    nodes = [*pcb._children(ast, "segment"), *pcb._children(ast, "via")]
    for footprint in pcb._children(ast, "footprint"):
        nodes.extend(pcb._children(footprint, "pad"))
    for node in nodes:
        key = pcb._child(node, "uuid", pcb._child(node, "tstamp", [None, ""]))[1]
        raw = pcb._child(node, "net", [None, 0])[1]
        net = codes.get(str(raw), raw if isinstance(raw, str) else "")
        if key:
            if str(key) in result:
                raise ValueError("Ambiguous copper/pad UUID")
            result[str(key)] = net
    return result


def _report(engine, project, revision, check):
    if catalog.verification(engine, project, revision) != check:
        raise ValueError("Repair verification changed or is no longer authentic")
    findings = catalog.drc_findings(engine, project, revision)
    if findings["status"] != "ok" or findings["verification_id"] != check["verification_id"]:
        raise ValueError("Repair requires authenticated native DRC evidence")
    _, folder = engine._verified(project, revision)
    path = catalog.artifact_path(folder, folder / "verification" / check["verification_id"] / "drc.json")
    raw = read_json(path)
    if digest(path) != check["evidence_hashes"].get("drc.json"):
        raise ValueError("Repair DRC evidence changed during read")
    return raw


def _erc_report(engine, project, revision, check):
    _, folder = engine._verified(project, revision)
    path = catalog.artifact_path(folder, folder / "verification" / check["verification_id"] / "erc.json")
    raw = read_json(path)
    sha = digest(path)
    if sha != check["evidence_hashes"].get("erc.json") or sha != check["erc"].get("report_sha256"):
        raise ValueError("Repair ERC evidence is not bound to its native report")
    return raw


def connection_counts(report, index):
    counts = Counter()
    for finding in report["unconnected_items"]:
        items = finding["items"]
        names = {index.get(item.get("uuid")) for item in items}
        if len(items) < 2 or len(names) != 1 or None in names or "" in names:
            raise ValueError("Cannot resolve a native missing connection to one actual net")
        counts[next(iter(names))] += 1
    return counts


def quality_issues(check):
    reasons = []
    reasons.extend(r for r in check.get("reasons", [])
                   if r != "DRC unavailable, errors present, or connections incomplete")
    drc, erc = check.get("drc", {}), check.get("erc", {})
    reasons.extend(report_rule_issues(drc, "DRC"))
    reasons.extend(report_rule_issues(erc, "ERC"))
    if (drc.get("status") != "ok" or drc.get("report_valid") is not True
            or type(drc.get("unconnected")) is not int or drc.get("unconnected", -1) < 0
            or drc.get("errors") != drc.get("unconnected")):
        reasons.append("DRC unavailable or non-connectivity errors present")
    if erc.get("status") != "ok" or erc.get("errors") != 0:
        reasons.append("ERC must be available and error-free")
    if check.get("connectivity", {}).get("status") != "passed":
        reasons.append("Schematic parity is not verified")
    for key in ("constraints", "persisted_track_minima"):
        if check.get(key, {}).get("passed") is not True:
            reasons.append(key + " does not pass")
    return reasons


def _violations(report):
    return Counter((v["type"], v["severity"], v["description"],
                    tuple(sorted(str(i.get("uuid")) for i in v["items"])))
                   for v in report["violations"])


def _erc_violations(report):
    if report.get("$schema") != "https://schemas.kicad.org/erc.v1.json":
        raise ValueError("Unsupported native ERC report schema")
    result = Counter()
    for sheet in report["sheets"]:
        for finding, count in _violations(sheet).items():
            result[(sheet["uuid_path"], *finding)] += count
    return result


def _report_issues(report):
    reasons = []
    if any(v.get("severity") != "error" or v.get("type") != "unconnected_items"
           for v in report["unconnected_items"]):
        reasons.append("Missing connections must retain native error severity")
    if any(v.get("severity") == "error" for v in report["violations"]):
        reasons.append("Non-connectivity DRC errors present in the actual report")
    return reasons


def compare_checks(before, after, before_report, after_report, before_index, after_index, nets):
    reasons = quality_issues(before) + quality_issues(after)
    reasons += _report_issues(before_report) + _report_issues(after_report)
    if after.get("erc", {}).get("warnings", 0) > before.get("erc", {}).get("warnings", 0):
        reasons.append("New ERC warnings appeared")
    old = connection_counts(before_report, before_index)
    new = connection_counts(after_report, after_index)
    if sum(old.values()) != before["drc"]["unconnected"] or sum(new.values()) != after["drc"]["unconnected"]:
        reasons.append("Native connection summary disagrees with actual report")
    if _violations(after_report) - _violations(before_report):
        reasons.append("New native DRC violations or warnings appeared")
    for net in old.keys() | new.keys():
        if new[net] > old[net] or (net not in nets and new[net] != old[net]):
            reasons.append("Connection regression or out-of-scope change on " + net)
    if sum(new.values()) >= sum(old.values()):
        reasons.append("No strict reduction in native unconnected count")
    return {"accepted": not reasons, "reasons": reasons,
            "before_unconnected": sum(old.values()), "after_unconnected": sum(new.values()),
            "before_by_net": dict(old), "after_by_net": dict(new),
            "manufacturing_authorized": False}


def diagnose(engine, project, revision):
    from .connection_geometry import contacts
    data, folder = engine._verified(project, revision)
    check = catalog.verification(engine, project, revision)
    if check["status"] in {"not_verified", "invalid_evidence"}:
        return {"status": "blocked", "revision": revision, "reason": "Run native verification first"}
    report = _report(engine, project, revision, check)
    path = folder / "design" / data["board"]
    index, board = _net_index(path), pcb.read_board(path)
    counts = connection_counts(report, index)
    bounds = board["outline"]["bounds"]
    scale = {"mm": 1, "in": 25.4, "mils": .0254}[report["coordinate_units"]]
    proposals = []
    geometry = contacts(path, report["unconnected_items"])
    for finding, contact in zip(report["unconnected_items"], geometry):
        net = index[finding["items"][0]["uuid"]]
        points = [i.get("pos") for i in finding["items"]]
        region = None
        if all(p is not None for p in points):
            x, y = [p["x"] * scale for p in points], [p["y"] * scale for p in points]
            region = [max(bounds[0], min(x) - 3), max(bounds[1], min(y) - 3),
                      min(bounds[2], max(x) + 3), min(bounds[3], max(y) + 3)]
        proposals.append({"nets": [net], "region": region, "remove_ids": [],
                          "contact_geometry": contact,
                          "items": finding["items"], "region_basis": "DRC item origins plus 3 mm; not nearest connection points",
                          "mode": "additive_first", "feasibility": "not_evaluated"})
    issues = quality_issues(check)
    issues += _report_issues(report)
    return {"status": "blocked" if issues else "ok", "revision": revision, "revision_digest": data["digest"],
            "verification_id": check["verification_id"], "unconnected_by_net": dict(counts), "issues": issues,
            "proposals": proposals, "requires_scope_review": True, "manufacturing_authorized": False}


def repair(engine, project, revision, nets, region, remove_ids=None, passes=3):
    from .repair_dsn import protect_dsn
    from .repair_geometry import prepare_repair, merge_repair
    remove_ids = [] if remove_ids is None else remove_ids
    validate_scope(nets, region, remove_ids, passes)
    with engine.store.lock(project):
        data, parent = engine._verified(project, revision)
        source = parent / "design" / data["board"]
        board = pcb.read_board(source)
        intent = read_json(parent / "constraints.json")
        if board["unsupported"] or not board["outline"]["supported"] or intent["critical_nets"]:
            return engine._blocked(project, revision, "Local repair does not bypass unsupported geometry or critical-net review")
        attempt_id = "repair-" + uuid.uuid4().hex[:12]
        folder = catalog.artifact_path(parent, parent / "repairs" / attempt_id)
        folder.mkdir(parents=True)
        result = {"status": "blocked", "project": project, "revision": revision, "attempt_id": attempt_id,
                  "source_digest": data["digest"], "request": {"nets": nets, "region": list(region), "remove_ids": remove_ids, "passes": passes},
                  "operations": [], "manufacturing_authorized": False}

        def finish(reason=None):
            if reason:
                result["reason"] = reason
            engine._verified(project, revision)
            path = folder / "result.json"
            write_json(path, result)
            engine.store.event(project, "repair_completed", {"revision": revision, "attempt_id": attempt_id,
                               "status": result["status"], "sha256": digest(path)})
            return result

        try:
            working = folder / "working" / data["board"]
            empty = folder / "empty" / data["board"]
            working.parent.mkdir(parents=True)
            empty.parent.mkdir(parents=True)
            manifest = prepare_repair(source, working, empty, nets, list(region), remove_ids)
            write_json(folder / "scope.json", manifest)
            result["scope_sha256"] = digest(folder / "scope.json")
            before = engine._verify(project, revision)
            result["baseline_verification"] = before["verification_id"]
            issues = quality_issues(before)
            if issues:
                return finish("Baseline is not eligible: " + "; ".join(issues))
            before_report = _report(engine, project, revision, before)
            if _report_issues(before_report):
                return finish("Baseline native DRC severity does not qualify for repair")
            old_counts = connection_counts(before_report, _net_index(source))
            if not any(old_counts[n] for n in nets):
                return finish("Selected nets have no native missing connections")
            for output in (working, empty):
                for extension in (".kicad_pro", ".kicad_dru"):
                    path = source.with_suffix(extension)
                    if path.exists():
                        shutil.copy2(path, output.with_suffix(extension))
            # Isolate only run bounds, not electrical rules. The full-board
            # importer remains strict; this route imports into an empty staging
            # board then merges a checked patch into the untouched original AST.
            router = Toolchain({**engine.toolchain.config, "route_timeout_seconds": min(engine.toolchain.route_timeout, 300)})
            result["router_time_limit_seconds"] = router.route_timeout
            raw_dsn, protected, ses = folder / "raw.dsn", folder / "protected.dsn", folder / "repair.ses"
            imported = folder / "imported" / data["board"]
            inputs = {p: digest(p) for base in (working, empty) for p in
                      (base, base.with_suffix(".kicad_pro"), base.with_suffix(".kicad_dru")) if p.exists()}

            def assert_inputs():
                for path, expected in inputs.items():
                    if digest(catalog.artifact_path(folder, path)) != expected:
                        raise ValueError("Prepared repair artifact changed across native stages: " + path.name)

            for name, operation in (("export", lambda: router.export_dsn(working, raw_dsn)),
                                    ("protect", lambda: {"status": "ok", **protect_dsn(raw_dsn, protected)}),
                                    ("route", lambda: router.route(protected, ses, passes, **(
                                        {"nets": nets} if engine.toolchain.config.get("repair_route_scope") is True else {}))),
                                    ("import", lambda: router.import_ses(empty, ses, imported))):
                assert_inputs()
                item = operation()
                result["operations"].append({"stage": name, **item})
                write_json(folder / "operations.json", result["operations"])
                if item["status"] != "ok":
                    return finish("Local routing stage failed: " + name)
                assert_inputs()
                output = {"export": raw_dsn, "protect": protected, "route": ses, "import": imported}[name]
                catalog.artifact_path(folder, output)
                if any(output.samefile(path) for path in inputs):
                    raise ValueError("Native output aliases a prepared repair artifact")
                inputs[output] = digest(output)
            # Evidence and inputs are checked again after all native stages;
            # cached baseline dictionaries alone cannot authorize a candidate.
            before_report = _report(engine, project, revision, before)
            merged = folder / "merged.kicad_pcb"
            result["patch"] = merge_repair(source, imported, merged, manifest)
            if digest(folder / "scope.json") != result["scope_sha256"]:
                raise ValueError("Repair scope changed during native execution")
            if engine._electrical_signature(board) != engine._electrical_signature(pcb.read_board(merged)):
                raise ValueError("Local patch changed component placement or electrical identity")
            child, child_folder, _ = engine._new(project, revision)
            shutil.copy2(merged, child_folder / "design" / data["board"])
            candidate = engine._seal(project, child, child_folder, data["board"], revision, "repair_candidate",
                                     {"repair_attempt": attempt_id, "repair_scope_sha256": result["scope_sha256"]})
            result["candidate_revision"] = child
            after = engine._verify(project, child)
            result["candidate_verification"] = after["verification_id"]
            assert_inputs()
            before_report = _report(engine, project, revision, before)
            comparison = compare_checks(before, after, before_report, _report(engine, project, child, after),
                                        _net_index(source), _net_index(child_folder / "design" / data["board"]), nets)
            if _erc_violations(_erc_report(engine, project, child, after)) - _erc_violations(_erc_report(engine, project, revision, before)):
                comparison["accepted"] = False
                comparison["reasons"].append("New native ERC finding appeared")
            result["comparison"] = comparison
            if comparison["accepted"]:
                result.update(status="repaired" if after["status"] == "passed" else "improved", revision=child,
                              revision_digest=candidate["digest"], verification_status=after["status"])
                return finish()
            return finish("Candidate retained for inspection; acceptance gate rejected it")
        except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
            return finish(type(error).__name__ + ": " + str(error))
