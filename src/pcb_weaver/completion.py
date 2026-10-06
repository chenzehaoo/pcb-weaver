"""Bounded multi-layout routing with native evidence and explicit review states."""
from copy import deepcopy
import math
import time
from uuid import uuid4

from . import catalog
from .board import read_board, _load, _child, _children, _tag
from .models import CompletionOptions
from .repair import (_report, _report_issues, quality_issues, _violations,
                     _erc_report, _erc_violations)
from .storage import digest, read_json, write_json


def classify(result, stage):
    if result.get("timed_out"):
        return {"category":"route_timeout", "action":"try_next_layout", "stage":stage}
    if stage == "plan":
        return {"category":"placement_infeasible", "action":"review_constraints", "stage":stage}
    if stage == "route":
        return {"category":"router_failure", "action":"try_next_layout", "stage":stage,
                "failed_stage":result.get("failed_stage")}
    if result.get("erc",{}).get("errors",1) or result.get("connectivity",{}).get("status") != "passed":
        return {"category":"electrical_input", "action":"review_schematic", "stage":stage}
    drc = result.get("drc",{})
    if drc.get("status") != "ok":
        return {"category":"native_unavailable", "action":"review_toolchain", "stage":stage}
    if drc.get("errors",0) > drc.get("unconnected",0):
        return {"category":"design_rule_conflict", "action":"try_next_layout", "stage":stage}
    return {"category":"connections_incomplete", "action":"bounded_repair_then_next_layout", "stage":stage}


def metrics(engine, project, revision, check, baseline_board, initial_missing):
    data,folder = engine._verified(project,revision)
    board = read_board(folder / "design" / data["board"])
    poses = {fp["reference"]:fp for fp in baseline_board["footprints"]}
    displacement = [math.dist([fp["x"],fp["y"]],[poses[fp["reference"]]["x"],poses[fp["reference"]]["y"]])
                    for fp in board["footprints"]]
    missing = check["drc"].get("unconnected")
    return {"revision":revision,"board_sha256":digest(folder / "design" / data["board"]),
            "verification_id":check["verification_id"],"status":check["status"],
            "components":len(board["footprints"]),"nets":len(board["nets"]),"layers":len(board["copper_layers"]),
            "tracks":board["tracks"],"vias":board["vias"],"routed_length_mm":board.get("routed_length_mm"),
            "moved_components":sum(d > 1e-6 for d in displacement),
            "total_displacement_mm":sum(displacement),"maximum_displacement_mm":max(displacement,default=0),
            "unconnected":missing,"drc_errors":check["drc"].get("errors"),"drc_warnings":check["drc"].get("warnings"),
            "erc_errors":check["erc"].get("errors"),"erc_warnings":check["erc"].get("warnings"),
            "connection_item_completion_percent":max(0,100*(1-missing/initial_missing)) if initial_missing and missing is not None else (100 if missing == 0 else None),
            "metric_scope":"Native missing-connection items relative to unrouted input, not a signal-integrity or net-count percentage"}


def _preserved_board_ast(ast):
    # Only root routing objects and generator labels may differ. Keep unknown
    # fields and every nested node, including footprint/pad rule overrides.
    return [node for node in ast
            if _tag(node) not in {"segment","via","generator","generator_version"}]


def _preserved_state(engine, project, revision):
    data, folder = engine._verified(project,revision)
    path = folder / "design" / data["board"]
    board, ast = read_board(path), _load(path)
    # Companion hashes include project/custom rules, schematics and libraries.
    return {"electrical_identity":engine._electrical_signature(board),
            "footprints":sorted(board["footprints"],key=lambda f:f["reference"]),
            "placement":sorted((f["reference"],f["uuid"],f["x"],f["y"],f["rotation"] % 360,
                                f["layer"],f["locked"]) for f in board["footprints"]),
            "outline":board["outline"],"copper_layers":board["copper_layers"],
            "board_rules":(_child(ast,"setup"),_children(ast,"net_class")),
            "nonrouting_ast":_preserved_board_ast(ast),
            "constraints":data["constraints_hash"],"board_name":data["board"],
            "companion_files":{name:sha for name,sha in data["files"].items() if name != data["board"]}}


def complete(engine, project, revision, options=None, *, checkpoint=lambda:None, progress=lambda value:None):
    options = options if isinstance(options,CompletionOptions) else CompletionOptions.model_validate(options or {})
    started = time.monotonic()
    original,folder = engine._verified(project,revision)
    work = catalog.artifact_path(folder,folder / "completion" / ("flow-"+uuid4().hex[:12]))
    work.mkdir(parents=True)
    board = read_board(folder / "design" / original["board"])
    intent = read_json(folder / "constraints.json")
    result = {"status":"running","project":project,"source_revision":revision,"revision":revision,
              "source_digest":original["digest"],"options":options.model_dump(),"attempts":[],
              "flow_id":work.name,"manufacturing_authorized":False,"manual_routing_interventions":0,
              "budget_scope":"No new stage after scheduling budget; active native/planning stages retain their configured bounds"}
    best_rank = (float("inf"),)
    preserve = options.placement_mode == "preserve"

    def save(stage):
        result.update(stage=stage,elapsed_seconds=round(time.monotonic()-started,3))
        write_json(work / "result.json",result)
        progress(deepcopy(result))

    def finish(reason=None):
        result["status"] = "blocked" if reason else "completed"
        if reason:
            result["reason"] = reason
        engine._verified(project,revision)
        engine._verified(project,result["revision"])
        save("finished")
        engine.store.event(project,"completion_finished",{"flow_id":work.name,"revision":result["revision"],
                           "status":result["status"],"sha256":digest(work / "result.json")})
        return result

    def boundary():
        checkpoint()
        return time.monotonic()-started < options.time_budget_seconds

    def assess(rev,check):
        nonlocal best_rank
        report = _report(engine,project,rev,check)
        issues = quality_issues(check) + _report_issues(report)
        if preserve:
            issues += preservation_issues(rev)
            if _violations(report) - baseline_drc:
                issues.append("New native DRC violations or warnings appeared")
            if _erc_violations(_erc_report(engine,project,rev,check)) - baseline_erc:
                issues.append("New native ERC findings appeared")
            for kind in ("drc","erc"):
                if check[kind].get("warnings",0) > baseline[kind].get("warnings",0):
                    issues.append("New " + kind.upper() + " warnings appeared")
            result.setdefault("candidate_assessments",{})[rev] = {"accepted":not issues,"reasons":issues}
        if issues:
            return False
        measured = metrics(engine,project,rev,check,board,result["baseline"]["unconnected"])
        rank = (measured["unconnected"],measured["routed_length_mm"] or 0,measured["vias"])
        if rank < best_rank:
            best_rank = rank
            result.update(revision=rev,best=measured)
            save("retained")
        return check["status"] == "passed"

    def preservation_issues(rev):
        current = _preserved_state(engine,project,rev)
        return ["Preserve mode changed " + key for key in preserved_state if current[key] != preserved_state[key]]

    try:
        save("preflight")
        if board["tracks"] or board["vias"]:
            return finish("Requires an unrouted input; existing copper is never removed automatically")
        if board["unsupported"] or not board["outline"]["supported"] or intent["critical_nets"] or not intent["release"]["require_erc"]:
            return finish("Geometry/critical-net review or full schematic verification is required")
        if not boundary():
            return finish("Scheduling budget exhausted")
        baseline = engine.verify_revision(project,revision)
        result["baseline"] = metrics(engine,project,revision,baseline,board,baseline["drc"].get("unconnected"))
        if baseline["drc"].get("status") != "ok" or baseline["erc"].get("status") != "ok" or baseline["erc"].get("errors",1) or baseline["connectivity"].get("status") != "passed":
            result["diagnosis"] = classify(baseline,"preflight")
            return finish("Schematic or native input checks require review")
        if preserve:
            report = _report(engine,project,revision,baseline)
            issues = quality_issues(baseline) + _report_issues(report)
            if issues:
                return finish("Fixed-placement input requires review: " + "; ".join(issues))
            preserved_state = _preserved_state(engine,project,revision)
            baseline_drc = _violations(report)
            baseline_erc = _erc_violations(_erc_report(engine,project,revision,baseline))
        if not boundary():
            return finish("Scheduling budget exhausted after preflight")
        if preserve:
            candidates = [{"id":"preserved","placements":[],"metrics":{}}]
        else:
            save("planning")
            plan = engine.plan_layout(project,revision,options.candidate_count,completion_spread_mm=options.placement_spread_mm)
            result["plan"] = plan
            if plan["status"] != "ok":
                result["diagnosis"] = classify(plan,"plan")
                return finish("No feasible layout; inspect recorded placement findings")
            candidates = [c for c in plan["candidates"] if c["feasible"]]
        seen = set()
        for candidate in candidates[:options.candidate_count]:
            signature = tuple(sorted((p["reference"],p["x"],p["y"],p["rotation"]) for p in candidate["placements"]))
            if signature in seen:
                continue
            seen.add(signature)
            if not boundary():
                return finish("Scheduling budget exhausted; best review revision retained")
            attempt = {"candidate_id":candidate["id"],"status":"running","placement_metrics":candidate["metrics"]}
            result["attempts"].append(attempt)
            if preserve:
                placed_rev = revision
                save("placement_preserved")
            else:
                save("placement")
                placed = engine.apply_layout(project,revision,plan["plan_id"],candidate["id"])
                placed_rev = placed["revision"]["id"]
            attempt["placement_revision"] = placed_rev
            if not boundary():
                return finish("Scheduling budget exhausted before routing")
            save("routing")
            routing_options = {"strict_widths":True} if options.routing_policy == "strict" else {"normalize_widths":True}
            routed = engine.route_revision(project,placed_rev,options.route_passes,**routing_options)
            attempt["routing"] = routed
            if routed["status"] != "routed_unverified":
                attempt.update(status="rejected",diagnosis=classify(routed,"route"))
                save("candidate_rejected")
                continue
            routed_rev = routed["revision"]["id"]
            attempt["routed_revision"] = routed_rev
            if preserve:
                issues = preservation_issues(routed_rev)
                if routed_rev == revision:
                    issues.append("Routing must create an immutable child revision")
                if issues:
                    attempt.update(status="rejected",preservation_issues=issues)
                    save("candidate_rejected")
                    continue
            if not boundary():
                return finish("Scheduling budget exhausted before native verification; unverified candidate not retained")
            save("verification")
            check = engine.verify_revision(project,routed_rev)
            if options.routing_policy == "normalize_widths" and check["status"] != "passed":
                from .clearance_repair import eligible, clearance_keys
                from .completion_cleanup import clearance_scopes
                cleanup = attempt["clearance_cleanup"] = []
                tried = set()
                for _ in range(6):
                    if not boundary():
                        break
                    report = _report(engine,project,routed_rev,check)
                    if eligible(check,report) or not clearance_keys(report):
                        break
                    data,child_folder = engine._verified(project,routed_rev)
                    scopes = [scope for margin in (.8,1.6) for scope in clearance_scopes(
                        child_folder / "design" / data["board"],report,margin)]
                    scope = next((s for s in scopes if (routed_rev,tuple(s["source_items"]),s["margin_mm"]) not in tried),None)
                    if scope is None:
                        break
                    tried.add((routed_rev,tuple(scope["source_items"]),scope["margin_mm"]))
                    save("clearance_cleanup")
                    repaired = engine.repair_clearance(project,routed_rev,scope["nets"],scope["region"])
                    cleanup.append({"scope":scope,"result":repaired})
                    if repaired["status"] == "improved":
                        routed_rev = repaired["revision"]
                        attempt["cleaned_revision"] = routed_rev
                    check = catalog.verification(engine,project,routed_rev)
                    save("clearance_cleanup")
            if preserve and options.routing_policy == "normalize_widths" and boundary():
                from .completion_cleanup import dangling_via_ids, cleanup_dangling_vias
                if dangling_via_ids(_report(engine,project,routed_rev,check)):
                    save("via_cleanup")
                    cleanup = cleanup_dangling_vias(engine,project,routed_rev,check,revision,baseline,
                                                   checkpoint=boundary)
                    attempt["via_cleanup"] = cleanup
                    if cleanup["status"] == "improved":
                        routed_rev,check = cleanup["revision"],cleanup["verification"]
                        attempt["via_cleaned_revision"] = routed_rev
                    save("via_cleanup")
            attempt["verification"] = check
            passed = assess(routed_rev,check)
            if preserve and not result["candidate_assessments"][routed_rev]["accepted"]:
                attempt["status"] = "rejected"
                save("candidate_rejected")
                continue
            attempt["metrics"] = metrics(engine,project,routed_rev,check,board,result["baseline"]["unconnected"])
            if passed:
                attempt["status"] = "passed"
                return finish()
            attempt["diagnosis"] = classify(check,"verify")
            remaining = int(options.time_budget_seconds-(time.monotonic()-started))
            if not quality_issues(check) and remaining >= 30 and boundary():
                repair_source = routed_rev
                stages = ("additive_repair","repair") if preserve and options.repair.allow_multinet else ("repair",)
                for cycle_index in range(options.repair_cycles):
                    if not boundary():
                        break
                    cycle_started = time.monotonic()
                    repair_deadline = min(started+options.time_budget_seconds,
                                          cycle_started+options.repair.time_budget_seconds)
                    if int(repair_deadline-cycle_started) < 30:
                        break
                    cycle_source = repair_source
                    before_missing = check["drc"]["unconnected"]
                    cycle = attempt
                    if cycle_index:
                        cycle = {"cycle":cycle_index+1,"source_revision":cycle_source,
                                 "revision":cycle_source,"status":"running",
                                 "before_unconnected":before_missing,"after_unconnected":before_missing,
                                 "max_attempts":options.repair.max_attempts,
                                 "time_budget_seconds":options.repair.time_budget_seconds,
                                 "effective_budget_seconds":repair_deadline-cycle_started}
                        attempt.setdefault("additional_repair_cycles",[]).append(cycle)
                    attempts_left = options.repair.max_attempts
                    invalid_candidate = False

                    def update_cycle():
                        if cycle_index:
                            cycle.update(elapsed_seconds=round(time.monotonic()-cycle_started,3),
                                         attempts_used=sum(len(cycle.get(stage,{}).get("attempts",[])) for stage in stages),
                                         revision=repair_source,after_unconnected=check["drc"]["unconnected"])

                    update_cycle()
                    save("repairing")
                    for repair_stage in stages:
                        if not boundary():
                            break
                        seconds_left = int(repair_deadline-time.monotonic())
                        if attempts_left < 1 or seconds_left < 30:
                            break
                        additive = repair_stage == "additive_repair"
                        stage_attempts = min(4,attempts_left-1) if additive else attempts_left
                        stage_seconds = min(300,seconds_left-30) if additive else seconds_left
                        if stage_attempts < 1 or stage_seconds < 30:
                            continue
                        repair_options = options.repair.model_copy(update={
                            "allow_multinet":False if additive else options.repair.allow_multinet,
                            "max_attempts":stage_attempts,"time_budget_seconds":stage_seconds})
                        def repaired_progress(value, stage=repair_stage, source=repair_source):
                            nonlocal invalid_candidate
                            cycle[stage] = value
                            update_cycle()
                            if value["revision"] != source:
                                checked = catalog.verification(engine,project,value["revision"])
                                assess(value["revision"],checked)
                                if preserve and not result["candidate_assessments"][value["revision"]]["accepted"]:
                                    invalid_candidate = True
                            save("repairing")
                        repair = engine.auto_repair_revision(project,repair_source,repair_options,
                            checkpoint=checkpoint,progress=repaired_progress)
                        cycle[repair_stage] = repair
                        attempts_left -= len(repair.get("attempts",[]))
                        update_cycle()
                        if invalid_candidate:
                            break
                        if repair["revision"] != repair_source:
                            checked = catalog.verification(engine,project,repair["revision"])
                            passed = assess(repair["revision"],checked)
                            if preserve and not result["candidate_assessments"][repair["revision"]]["accepted"]:
                                invalid_candidate = True
                                break
                            repair_source,check = repair["revision"],checked
                            update_cycle()
                            attempt["metrics"] = metrics(engine,project,repair_source,check,board,result["baseline"]["unconnected"])
                            if passed:
                                if cycle_index:
                                    cycle["status"] = "passed"
                                attempt["status"] = "passed"
                                return finish()
                    improved = repair_source != cycle_source and check["drc"]["unconnected"] < before_missing
                    if cycle_index:
                        cycle["status"] = "rejected" if invalid_candidate else "improved" if improved else "no_progress"
                        update_cycle()
                    save("repairing")
                    if invalid_candidate or not improved:
                        break
            attempt["status"] = "review_required"
            save("candidate_rejected")
        return finish("Fixed-placement routing and bounded repair exhausted; best review revision retained" if preserve
                      else "All distinct feasible layouts exhausted; best review revision retained")
    except (ValueError,OSError,KeyError,TypeError,IndexError) as error:
        return finish(type(error).__name__+": "+str(error))
    finally:
        if result["status"] == "running":
            result["status"] = "interrupted"
            save("interrupted")
