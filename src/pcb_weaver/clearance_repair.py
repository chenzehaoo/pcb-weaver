"""Revision-bound clearance repair; geometric proposals never authorize manufacture."""
from collections import Counter
from copy import deepcopy
import shutil
import uuid

from . import catalog
from .clearance import propose
from .repair import (_report, _erc_report, _erc_violations, _violations, _net_index,
                     connection_counts, quality_issues, validate_scope)
from .storage import digest, read_json, write_json


def clearance_keys(report):
    return Counter(tuple(sorted(str(i.get("uuid")) for i in v["items"]))
                   for v in report["violations"] if v["type"] == "clearance" and v["severity"] == "error")


def eligible(check, report):
    adjusted = deepcopy(check)
    adjusted["drc"]["errors"] -= sum(clearance_keys(report).values())
    reasons = quality_issues(adjusted)
    if any(v["severity"] not in {"error", "warning"} for v in report["violations"]):
        reasons.append("Unsupported or excluded DRC severity")
    if any(v["severity"] != "error" or v["type"] != "unconnected_items" for v in report["unconnected_items"]):
        reasons.append("Unconnected findings must retain native error severity")
    raw_errors = len(report["unconnected_items"]) + sum(v["severity"] == "error" for v in report["violations"])
    if raw_errors != check["drc"]["errors"] or len(report["unconnected_items"]) != check["drc"]["unconnected"]:
        reasons.append("Native summary disagrees with actual DRC report")
    if any(v["severity"] == "error" and v["type"] != "clearance" for v in report["violations"]):
        reasons.append("Non-clearance copper errors require separate repair")
    return reasons


def compare(before, after, old_report, new_report, old_index, new_index, nets):
    reasons = eligible(before, old_report) + eligible(after, new_report)
    old_clear, new_clear = clearance_keys(old_report), clearance_keys(new_report)
    old = connection_counts(old_report, old_index)
    new = connection_counts(new_report, new_index)
    if sum(new_clear.values()) >= sum(old_clear.values()):
        reasons.append("No strict reduction in native clearance errors")
    if new_clear - old_clear:
        reasons.append("New clearance item pairs appeared")
    for net in old.keys() | new.keys():
        if new[net] > old[net] or (net not in nets and new[net] != old[net]):
            reasons.append("Connection regression or out-of-scope change on " + net)
    other = lambda raw: {**raw, "violations": [v for v in raw["violations"]
                         if not (v["type"] == "clearance" and v["severity"] == "error")]}
    if _violations(other(new_report)) - _violations(other(old_report)):
        reasons.append("New native DRC findings or warnings appeared")
    return {"accepted": not reasons, "reasons": reasons,
            "before_clearance": sum(old_clear.values()), "after_clearance": sum(new_clear.values()),
            "before_connections": dict(old), "after_connections": dict(new), "manufacturing_authorized": False}


def repair(engine, project, revision, nets, region):
    validate_scope(nets, region, [], 1)
    with engine.store.lock(project):
        data, parent = engine._verified(project, revision)
        intent = read_json(parent / "constraints.json")
        if intent["critical_nets"]:
            return engine._blocked(project, revision, "Clearance repair does not bypass critical-net review")
        attempt = "clearance-" + uuid.uuid4().hex[:12]
        folder = catalog.artifact_path(parent, parent / "repairs" / attempt)
        folder.mkdir(parents=True)
        result = {"status": "blocked", "project": project, "revision": revision, "attempt_id": attempt,
                  "source_digest": data["digest"], "manufacturing_authorized": False,
                  "request": {"nets": nets, "region": list(region), "layer": "F.Cu", "max_axis_move_mm": .08}}

        def finish(reason=None):
            if reason:
                result["reason"] = reason
            engine._verified(project, revision)
            path = folder / "result.json"
            write_json(path, result)
            engine.store.event(project, "clearance_repair_completed", {"revision": revision, "attempt_id": attempt,
                               "status": result["status"], "sha256": digest(path)})
            return result

        before = engine._verify(project, revision)
        old_report = _report(engine, project, revision, before)
        result["baseline_verification"] = before["verification_id"]
        issues = eligible(before, old_report)
        if issues or not clearance_keys(old_report):
            return finish("Baseline is not eligible: " + "; ".join(issues or ["No clearance errors"]))
        old_erc = _erc_violations(_erc_report(engine, project, revision, before))
        source = parent / "design" / data["board"]
        old_index = _net_index(source)
        target = folder / "proposal.kicad_pcb"
        # Project-specific rules remain authoritative in native DRC; this is only a proposal floor.
        clearance = intent["fabrication"]["min_clearance_mm"]
        pro_path = source.with_suffix(".kicad_pro")
        if pro_path.exists():
            project_rules = read_json(pro_path).get("board", {}).get("design_settings", {}).get("rules", {})
            clearance = max(clearance, project_rules.get("min_clearance", 0))
        try:
            proposal = propose(source, target, nets, list(region), clearance_mm=clearance)
        except ValueError as error:
            return finish(str(error))
        result["proposal"] = proposal
        if proposal["status"] != "proposed":
            return finish("Bounded joint optimization found no feasible proposal")
        engine._verified(project, revision)
        if digest(target) != proposal["output_sha256"] or digest(source) != proposal["source_sha256"]:
            return finish("Proposal or source changed before candidate snapshot")
        child, child_folder, old = engine._new(project, revision)
        destination = child_folder / "design" / old["board"]
        shutil.copy2(target, destination)
        if digest(destination) != proposal["output_sha256"]:
            return finish("Proposal changed while copying")
        engine._verified(project, revision)
        engine._seal(project, child, child_folder, old["board"], revision, "repair_candidate",
                     {"repair_method": "bounded_clearance_joints", "attempt_id": attempt, "request": result["request"]})
        result["candidate_revision"] = child
        after = engine._verify(project, child)
        new_report = _report(engine, project, child, after)
        comparison = compare(before, after, old_report, new_report, old_index, _net_index(destination), nets)
        if _erc_violations(_erc_report(engine, project, child, after)) - old_erc:
            comparison["reasons"].append("New native ERC findings appeared")
            comparison["accepted"] = False
        # Rebind both reports after optimization and verification to detect stale/tampered evidence.
        _report(engine, project, revision, before)
        _report(engine, project, child, after)
        _erc_report(engine, project, revision, before)
        result.update(comparison=comparison, verification=after)
        if comparison["accepted"]:
            result.update(status="improved", revision=child)
        return finish(None if comparison["accepted"] else "; ".join(comparison["reasons"]))
