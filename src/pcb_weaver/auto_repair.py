"""Automatic connection repair with immutable candidates and native adoption gates."""
from copy import deepcopy
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from . import catalog
from .board import read_board
from .models import AutoRepairOptions
from .repair import (_report, _report_issues, _erc_report, _erc_violations, _net_index,
                     connection_counts, quality_issues, compare_checks, _violations)
from .repair_geometry import prepare_repair, merge_repair
from .storage import canonical, digest, read_json, write_json


def _hypothesis_key(request):
    scope = request["ripup"]
    return canonical({"net":request["net"],"region":request["region"],
        "finding":request["finding"],"routing_rules":request["routing_rules"],
        "nets":sorted(scope["nets"]),"removed":sorted(scope["remove_ids"]),
        "preserve_cut_terminals":request.get("preserve_cut_terminals",False)})


def _untried_first(candidates, attempted):
    """Defer old hypotheses after an adoption; never reuse or omit their results."""
    deferred = []
    for request in candidates:
        if _hypothesis_key(request) in attempted:
            deferred.append(request)
        else:
            yield request
    yield from deferred


def compact_via_policy(source, native, net, rules):
    """Use explicit persisted limits only; never reinterpret custom DRC rules."""
    from decimal import Decimal
    project = source.with_suffix(".kicad_pro")
    if (not project.is_file() or source.with_suffix(".kicad_dru").is_file() or
            native.get("project_sha256") != digest(project)):
        return None
    settings = read_json(project)
    limits = settings.get("board", {}).get("design_settings", {}).get("rules", {})
    keys = ("min_via_diameter", "min_via_annular_width", "min_through_hole_diameter")
    if any(type(limits.get(k)) not in (int, float) or not math.isfinite(limits[k]) or limits[k] <= 0 for k in keys):
        return None
    drill = rules["via_drill"]
    if drill < limits["min_through_hole_diameter"]:
        return None
    owned = settings.get("pcb_weaver_net_rules", {}).get(net, {}).get("minimums", {})
    diameter = max(limits["min_via_diameter"], owned.get("via_diameter", 0),
                   float(Decimal(str(drill))+2*Decimal(str(limits["min_via_annular_width"]))))
    if diameter >= rules["via_diameter"]:
        return None
    return {"diameter_mm": diameter, "drill_mm": drill,
            "preferred_diameter_mm": rules["via_diameter"],
            "minimum_annular_width_mm": limits["min_via_annular_width"],
            "project_sha256": digest(project), "existing_via_dimensions_unchanged": True}


def adjustment_scope(source, region, removed, max_area):
    """Include retained far fragments of split tracks in the audited patch scope."""
    from .repair_geometry import _read, _board
    board = _board(_read(source)[0], source=True)
    bounds = [c["geometry"]["bounds"] for c in board["copper"] if c["id"] in removed]
    if len(bounds) != len(removed):
        raise ValueError("Adjustment contains unknown or duplicate original copper")
    scope = [min([region[i], *[b[i]-.001 for b in bounds]]) for i in (0,1)]
    scope += [max([region[i], *[b[i]+.001 for b in bounds]]) for i in (2,3)]
    if ((scope[2]-scope[0])*(scope[3]-scope[1]) > max_area or
            any(scope[i] < board["outline"][i] for i in (0,1)) or
            any(scope[i] > board["outline"][i] for i in (2,3))):
        raise ValueError("Split-track audit scope exceeds authorized bounds")
    return scope


def targets(source, report, native, check, options, edge):
    board, index = read_board(source), _net_index(source)
    outline = board["outline"]["bounds"]
    scale = {"mm":1,"in":25.4,"mils":.0254}[report["coordinate_units"]]
    findings = deepcopy(report["unconnected_items"])
    for finding in findings:
        for item in finding["items"]:
            item["pos"] = {axis:item["pos"][axis]*scale for axis in ("x","y")}
    findings.sort(key=lambda f:math.dist([f["items"][0]["pos"][a] for a in ("x","y")],
                                       [f["items"][1]["pos"][a] for a in ("x","y")]))
    if options.allow_multinet:
        from .ripup_planning import plans
        candidates = list(plans(source,findings,maximum_area=options.max_region_area_mm2,edge_clearance=edge))
        # Give each missing connection a small-scope attempt before larger retries.
        buckets = {}
        for plan in candidates:
            buckets.setdefault(plan["target_pad"],[]).append(plan)
        for index in range(max((len(v) for v in buckets.values()),default=0)):
            for bucket in buckets.values():
                if index >= len(bucket):
                    continue
                plan = bucket[index]
                routing = {}
                for net in plan["nets"]:
                    rules = dict(native["nets"][net])
                    minimum = max(check["persisted_track_minima"]["global_minimum_mm"],
                                  check["persisted_track_minima"]["per_net_minimum_mm"].get(net,0))
                    rules["preferred_width"] = max(rules["track_width"],minimum)
                    rules["track_width"] = minimum if options.allow_neckdown else rules["preferred_width"]
                    compact = compact_via_policy(source,native,net,rules)
                    if compact:
                        rules["via_diameter"] = compact["diameter_mm"]
                    routing[net] = rules
                yield {"strategy":"multinet","net":plan["target_net"],"region":plan["region"],
                    "preserve_cut_terminals":False,
                    "ripup":plan,"routing_rules":routing,"source":str(source),"source_sha256":digest(source),
                    "finding":next(f for f in findings if any(i["uuid"]==plan["target_pad"] for i in f["items"]))}
        return
    strategies = [("additive",3),("expanded",8)]
    if options.allow_neckdown:
        strategies.append(("neckdown",8))
    if options.allow_local_adjustment:
        strategies.append(("fanout",8))
    for strategy,margin in strategies:
        for finding in findings:
            if len(finding["items"]) != 2:
                continue
            net = index[finding["items"][0]["uuid"]]
            rules = dict(native["nets"][net])
            minimum = max(check["persisted_track_minima"]["global_minimum_mm"],
                          check["persisted_track_minima"]["per_net_minimum_mm"].get(net,0))
            preferred = max(rules["track_width"], minimum)
            rules["track_width"] = preferred
            if strategy == "neckdown" or (strategy == "fanout" and options.allow_neckdown):
                rules["track_width"] = minimum
            xy = [item["pos"] for item in finding["items"]]
            region = [max(outline[0]+edge,min(p["x"] for p in xy)-margin),
                      max(outline[1]+edge,min(p["y"] for p in xy)-margin),
                      min(outline[2]-edge,max(p["x"] for p in xy)+margin),
                      min(outline[3]-edge,max(p["y"] for p in xy)+margin)]
            if region[0] >= region[2] or region[1] >= region[3]:
                continue
            if (region[2]-region[0])*(region[3]-region[1]) > options.max_region_area_mm2:
                continue
            yield {"strategy":strategy,"net":net,"rules":rules,"preferred_width":preferred,
                   "compact_via_policy": compact_via_policy(source,native,net,rules) if strategy == "fanout" else None,
                   "minimum_width":minimum,"region":region,"finding":finding,
                   "source":str(source),"source_sha256":digest(source)}


def proposal(folder, request, timeout, checkpoint, *, worker_module="pcb_weaver.auto_proposal"):
    folder = Path(folder).resolve(strict=True)
    if worker_module not in {"pcb_weaver.auto_proposal", "pcb_weaver.negotiated_reroute"}:
        raise ValueError("Unknown isolated proposal worker")
    path = folder / "request.json"
    write_json(path,request)
    request_sha = digest(path)
    with (folder / "worker.log").open("wb") as log:
        process = subprocess.Popen([sys.executable,"-m",worker_module,str(path)],
                                   stdout=log,stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        deadline = time.monotonic()+timeout
        try:
            while process.poll() is None:
                checkpoint()
                if time.monotonic() >= deadline:
                    return {"status":"blocked","reason":"Proposal timed out","timed_out":True}
                time.sleep(.1)
            if digest(path) != request_sha:
                raise ValueError("Proposal request changed during execution")
            if process.returncode != 0:
                return {"status":"blocked","reason":"Proposal worker failed","exit_code":process.returncode}
            result = read_json(catalog.artifact_path(folder,folder / "proposal.json"))
            if result.get("status") == "proposed":
                output = catalog.artifact_path(folder,folder / result["output"])
                if output.parent != folder or digest(output) != result["output_sha256"]:
                    raise ValueError("Proposal output escaped its directory or changed")
            return result
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def repair(engine, project, revision, options=None, *, checkpoint=lambda:None, progress=lambda value:None):
    options = options if isinstance(options,AutoRepairOptions) else AutoRepairOptions.model_validate(options or {})
    started = time.monotonic()
    with engine.store.lock(project):
        original, parent = engine._verified(project,revision)
        folder = catalog.artifact_path(parent,parent / "repairs" / ("auto-"+uuid4().hex[:12]))
        folder.mkdir(parents=True)
        best = revision
        attempted_scopes = set()
        result = {"status":"running","project":project,"source_revision":revision,"revision":best,
                  "source_digest":original["digest"],"options":options.model_dump(),"attempts":[],
                  "attempt_id":folder.name,"manufacturing_authorized":False,
                  "budget_scope":"No new stage after budget; active native commands retain their configured timeout. Proposal processes have a hard timeout."}

        def save(stage):
            result.update(revision=best,elapsed_seconds=round(time.monotonic()-started,3),stage=stage)
            write_json(folder / "result.json",result)
            progress(deepcopy(result))

        def finish(reason=None):
            result["status"] = "blocked" if reason else "repaired"
            if reason:
                result["reason"] = reason
            engine._verified(project,revision)
            engine._verified(project,best)
            save("finished")
            engine.store.event(project,"auto_repair_completed",{"revision":best,"attempt_id":folder.name,
                               "status":result["status"],"sha256":digest(folder / "result.json")})
            return result

        def boundary():
            checkpoint()
            return time.monotonic()-started < options.time_budget_seconds

        try:
            save("preflight")
            intent = read_json(parent / "constraints.json")
            source = parent / "design" / original["board"]
            board = read_board(source)
            if board["unsupported"] or not board["outline"]["supported"] or intent["critical_nets"]:
                return finish("Unsupported geometry or critical nets require engineering review")
            if options.allow_neckdown and engine.toolchain.config.get("controlled_neckdown") is not True:
                return finish("Controlled neckdown is not enabled by the project toolchain")
            if not boundary():
                return finish("Time budget exhausted before baseline verification")
            save("baseline_verification")
            check = engine._verify(project,best)
            report = _report(engine,project,best,check)
            issues = quality_issues(check)+_report_issues(report)
            result["before_unconnected"] = check["drc"].get("unconnected")
            result["after_unconnected"] = result["before_unconnected"]
            if issues:
                return finish("Baseline is not eligible: "+"; ".join(issues))
            if check["status"] == "passed":
                result["already_passed"] = True
                return finish()
            while len(result["attempts"]) < options.max_attempts:
                if not boundary():
                    return finish("Time budget exhausted; retained the last accepted revision")
                data, current = engine._verified(project,best)
                source = current / "design" / data["board"]
                save("native_rules")
                native = engine.toolchain.inspect_board(source)
                if native.get("status") != "ok":
                    return finish("Native net rules are unavailable")
                adopted = False
                pending = []
                def requests():
                    candidates = targets(source,report,native,check,options,intent["board"]["edge_clearance_mm"])
                    if options.allow_multinet:
                        candidates = _untried_first(candidates,attempted_scopes)
                    for candidate in candidates:
                        yield candidate
                        while pending:
                            yield pending.pop()
                for request in requests():
                    if len(result["attempts"]) >= options.max_attempts or not boundary():
                        return finish("Attempt or time budget exhausted; retained the last accepted revision")
                    work = folder / f"attempt-{len(result['attempts'])+1:02d}"
                    work.mkdir()
                    attempt = {"parent_revision":best,"strategy":request["strategy"],"net":request["net"],
                               "region":request["region"],"status":"running",
                               "endpoint_retry":request.get("preserve_cut_terminals",False)}
                    if options.allow_multinet:
                        key = _hypothesis_key(request)
                        attempt["repeated_scope"] = key in attempted_scopes
                        attempted_scopes.add(key)
                    result["attempts"].append(attempt)
                    save("proposal")
                    worker = {"worker_module":"pcb_weaver.negotiated_reroute"} if request["strategy"] == "multinet" else {}
                    generated = proposal(work,request,min(options.proposal_timeout_seconds,
                        max(.1,options.time_budget_seconds-(time.monotonic()-started))),checkpoint,**worker)
                    attempt["proposal"] = generated
                    attempt["status"] = generated["status"]
                    if generated["status"] != "proposed":
                        save("proposal_rejected")
                        continue
                    if not boundary():
                        return finish("Time budget exhausted before candidate validation")
                    engine._verified(project,best)
                    _report(engine,project,best,check)
                    removed, nets = [], [request["net"]]
                    adjustment = generated.get("adjustment")
                    if adjustment:
                        multinet = request["strategy"] == "multinet" and options.allow_multinet
                        if not multinet and (not options.allow_local_adjustment or request["strategy"] != "fanout"):
                            raise ValueError("Unrequested copper adjustment")
                        if multinet and (set(adjustment["changed_ids"]) != set(request["ripup"]["remove_ids"])
                                or set(adjustment["nets"]) != set(request["ripup"]["nets"])
                                or generated.get("reference_used") is not False
                                or generated.get("original_partitions_preserved") is not True):
                            raise ValueError("Autonomous proposal changed its authorized scope or lacks preservation proof")
                        if multinet:
                            audit = generated.get("board_access_audit",{})
                            if (audit.get("source") != str(source.resolve()) or audit.get("working_root") != str(work.resolve())
                                    or not audit.get("opened") or any(Path(p).resolve() != source.resolve()
                                        and not Path(p).resolve().is_relative_to(work.resolve()) for p in audit["opened"])):
                                raise ValueError("Autonomous board access audit is missing or outside its source/work directory")
                        removed = adjustment["changed_ids"]
                        nets = sorted(set(nets+adjustment["nets"]))
                    scope = request["region"]
                    if removed:
                        try:
                            scope = adjustment_scope(source,scope,removed,options.max_region_area_mm2)
                        except ValueError as error:
                            attempt.update(status="blocked",reason=str(error))
                            save("proposal_rejected")
                            continue
                        attempt["patch_region"] = scope
                    manifest = prepare_repair(source,work / "working.kicad_pcb",work / "empty.kicad_pcb",
                                              nets,scope,removed)
                    write_json(work / "scope.json",manifest)
                    scope_sha = digest(work / "scope.json")
                    merged = work / "merged.kicad_pcb"
                    output = catalog.artifact_path(work,work / generated["output"])
                    if digest(output) != generated["output_sha256"]:
                        raise ValueError("Candidate output changed")
                    attempt["patch"] = merge_repair(source,output,merged,manifest)
                    if request["strategy"] == "multinet" and (attempt["patch"]["dropped_outside_region"] or attempt["patch"]["dropped_unselected_nets"]):
                        raise ValueError("Autonomous copper exceeds its declared scope")
                    if engine._electrical_signature(read_board(source)) != engine._electrical_signature(read_board(merged)):
                        raise ValueError("Candidate changed placement or electrical identity")
                    child, child_folder, _ = engine._new(project,best)
                    shutil.copy2(merged,child_folder / "design" / data["board"])
                    engine._seal(project,child,child_folder,data["board"],best,"repair_candidate",
                                 {"auto_repair_attempt":folder.name,"scope_sha256":scope_sha,
                                  **({"strategy":"multinet","reference_used":False} if request["strategy"]=="multinet" else {})})
                    attempt["candidate_revision"] = child
                    save("candidate_verification")
                    after = engine._verify(project,child)
                    before_report = _report(engine,project,best,check)
                    after_report = _report(engine,project,child,after)
                    if digest(work / "scope.json") != scope_sha:
                        raise ValueError("Candidate scope changed")
                    comparison = compare_checks(check,after,before_report,after_report,_net_index(source),
                                                _net_index(child_folder / "design" / data["board"]),nets)
                    if _erc_violations(_erc_report(engine,project,child,after)) - _erc_violations(_erc_report(engine,project,best,check)):
                        comparison["reasons"].append("New native ERC findings")
                    comparison["accepted"] = not comparison["reasons"]
                    new_findings = _violations(after_report)-_violations(before_report)
                    if (request["strategy"] == "multinet" and not request.get("preserve_cut_terminals")
                            and comparison["reasons"] == ["New native DRC violations or warnings appeared"]
                            and new_findings and all(key[0]=="track_dangling" and key[1]=="warning" for key in new_findings)):
                        retry = deepcopy(request)
                        retry["preserve_cut_terminals"] = True
                        pending.append(retry)
                        attempt["followup"] = "Restore original cut endpoints; native dangling-track feedback"
                    attempt.update(comparison=comparison,status="accepted" if comparison["accepted"] else "rejected")
                    if comparison["accepted"]:
                        best, check, report = child, after, after_report
                        result["after_unconnected"] = after["drc"]["unconnected"]
                        adopted = True
                    save("accepted" if adopted else "candidate_rejected")
                    if adopted:
                        if check["status"] == "passed":
                            return finish()
                        break
                if not adopted:
                    return finish("No acceptable proposal within supported strategies and scope; retained the last accepted revision")
            return finish("Attempt budget exhausted; retained the last accepted revision")
        except (ValueError,OSError,KeyError,TypeError,IndexError) as error:
            return finish(type(error).__name__+": "+str(error))
        finally:
            if result["status"] == "running":
                result["status"] = "interrupted"
                save("interrupted")
