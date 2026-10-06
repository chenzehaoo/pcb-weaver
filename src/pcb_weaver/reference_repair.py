"""Reference-guided local multi-net ECO proposals, never whole-board replacement."""
from copy import deepcopy
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Point,LineString,box
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .board import read_board, _locked
from .repair_geometry import _read,_board,_serialize,_field,prepare_repair,merge_repair
from .storage import canonical,digest,write_json


def touches_findings(source, plan, findings):
    from .routing_clearance import pad_envelope
    wanted = {i["uuid"] for f in findings for i in f["items"]}
    roi = box(*plan["region"])
    for f in read_board(source)["footprints"]:
        for p in f["pads"]:
            if p["uuid"] in wanted and p["net"] in plan["nets"] and pad_envelope(p).intersects(roi):
                return True
    for c in _board(_read(source)[0],source=True)["copper"]:
        if c["id"] in wanted and c["geometry"]["net"] in plan["nets"]:
            g = c["geometry"]
            core = Point(g["at"]) if g["kind"] == "via" else LineString([g["start"],g["end"]])
            if core.buffer(g.get("size",g.get("width",0))/2).intersects(roi):
                return True
    return False


def plans(source, reference, target_nets, maximum_area=2500):
    source,reference = Path(source),Path(reference)
    boards = [_board(_read(p)[0],source=True) for p in (source,reference)]
    keys = [{canonical(c["geometry"]) for c in b["copper"]} for b in boards]
    common = len(keys[0]&keys[1])
    if not keys[0] or common/len(keys[0]) < .7:
        raise ValueError("Reference is not a local ECO: insufficient unchanged copper")
    if boards[0]["layers"] != boards[1]["layers"] or boards[0]["outline"] != boards[1]["outline"]:
        raise ValueError("Reference layer stack or outline differs")
    changed = [(side,c) for side,b in enumerate(boards) for c in b["copper"]
               if canonical(c["geometry"]) not in keys[1-side]]
    if not changed:
        return []
    shapes = []
    for _,c in changed:
        g = c["geometry"]
        core = Point(g["at"]) if g["kind"] == "via" else LineString([g["start"],g["end"]])
        shapes.append(core.buffer(g.get("size",g.get("width",0))/2+.15))
    a,b = shapely.STRtree(shapes).query(shapes,predicate="intersects")
    graph = coo_matrix((np.ones(len(a)),(a,b)),shape=(len(changed),len(changed))).tocsr()
    _,labels = connected_components(graph,directed=False)
    result = []
    for label in sorted(set(labels)):
        group = [item for i,item in enumerate(changed) if labels[i] == label]
        nets = sorted({c["geometry"]["net"] for _,c in group})
        old = [c for side,c in group if side == 0]
        new = [c for side,c in group if side == 1]
        if not set(nets)&set(target_nets) or not new or len(nets)>8 or len(old)>1000:
            continue
        bounds = [c["geometry"]["bounds"] for _,c in group]
        region = [min(b[i] for b in bounds)-.001 for i in (0,1)]
        region += [max(b[i] for b in bounds)+.001 for i in (2,3)]
        if (region[2]-region[0])*(region[3]-region[1]) > maximum_area:
            continue
        if any(_locked(c["node"]) for c in old):
            continue
        result.append({"nets":nets,"region":region,"remove_ids":[c["id"] for c in old],
            "reference_ids":[c["id"] for c in new],"source_sha256":digest(source),
            "reference_sha256":digest(reference),"unchanged_copper":common,
            "manufacturing_authorized":False})
    return sorted(result,key=lambda p:(len(p["remove_ids"]),p["region"]))


def propose(source,reference,folder,plan):
    source,reference,folder = Path(source),Path(reference),Path(folder)
    if digest(source)!=plan["source_sha256"] or digest(reference)!=plan["reference_sha256"]:
        raise ValueError("Reference repair input changed")
    original = _read(source)[0]
    old = _board(original,source=True)
    ref = _board(_read(reference)[0],source=True)
    codes = {name:code for code,name in old["codes"].items()}
    additions = []
    for c in ref["copper"]:
        if c["id"] in plan["reference_ids"]:
            node = deepcopy(c["node"])
            _field(node,"net",1)[1] = codes[c["geometry"]["net"]]
            additions.append(node)
    if len(additions) != len(plan["reference_ids"]):
        raise ValueError("Reference copper IDs missing or ambiguous")
    removed_nodes = {id(c["node"]) for c in old["copper"] if c["id"] in plan["remove_ids"]}
    raw = folder / "reference-candidate.kicad_pcb"
    with raw.open("xb") as stream:
        stream.write(_serialize([n for n in original if id(n) not in removed_nodes]+additions))
    manifest = prepare_repair(source,folder / "working.kicad_pcb",folder / "empty.kicad_pcb",
                              plan["nets"],plan["region"],plan["remove_ids"])
    write_json(folder / "scope.json",manifest)
    output = folder / "merged.kicad_pcb"
    patch = merge_repair(source,raw,output,manifest)
    if patch["dropped_outside_region"] or patch["dropped_unselected_nets"]:
        raise ValueError("Reference patch did not fit its declared scope")
    return {"status":"proposed","output":output.name,"output_sha256":digest(output),
            "patch":patch,"scope_sha256":digest(folder / "scope.json"),
            "requires_native_verification":True,"manufacturing_authorized":False}


def repair(engine, project, revision, reference_project, reference_revision, *,
           checkpoint=lambda:None, progress=lambda value:None):
    import shutil
    import time
    from uuid import uuid4
    from . import catalog
    from .models import Constraints
    from .repair import (_report,_erc_report,_erc_violations,_net_index,connection_counts,
                         compare_checks,quality_issues,_report_issues)
    from .storage import read_json
    started = time.monotonic()
    with engine.store.lock(project):
        initial,parent = engine._verified(project,revision)
        refdata,refroot = engine._verified(reference_project,reference_revision)
        reference = refroot / "design" / refdata["board"]
        initial_source = parent / "design" / initial["board"]
        intent = Constraints.model_validate(read_json(parent / "constraints.json")).model_dump()
        refintent = Constraints.model_validate(read_json(refroot / "constraints.json")).model_dump()
        result = {"status":"running","project":project,"source_revision":revision,"revision":revision,
                  "source_digest":initial["digest"],
                  "reference_project":reference_project,"reference_revision":reference_revision,
                  "options":{"max_attempts":12,"max_region_area_mm2":2500,"time_budget_seconds":1800},
                  "reference_digest":refdata["digest"],"attempts":[],"manufacturing_authorized":False}
        evidence = parent / "repairs" / ("reference-"+uuid4().hex[:12])
        evidence.mkdir(parents=True)
        best = revision
        def save(stage):
            result.update(stage=stage,revision=best,elapsed_seconds=round(time.monotonic()-started,3))
            write_json(evidence / "result.json",result)
            progress(deepcopy(result))
        def finish(reason=None):
            result["status"] = "blocked" if reason else "repaired"
            if reason:
                result["reason"] = reason
            engine._verified(project,revision)
            engine._verified(reference_project,reference_revision)
            save("finished")
            engine.store.event(project,"reference_repair_completed",{"revision":best,
                "reference_revision":reference_revision,"status":result["status"],
                "evidence_sha256":digest(evidence / "result.json")})
            return result
        try:
            save("preflight")
            if intent != refintent or intent["critical_nets"]:
                return finish("Reference constraints differ or critical nets require review")
            if engine._electrical_signature(read_board(initial_source)) != engine._electrical_signature(read_board(reference)):
                return finish("Reference electrical identity or component placement differs")
            for suffix in (".kicad_pro",".kicad_dru"):
                a,b = initial_source.with_suffix(suffix),reference.with_suffix(suffix)
                if a.exists()!=b.exists():
                    return finish("Reference project rule files differ")
                if a.exists() and (read_json(a)!=read_json(b) if suffix==".kicad_pro" else digest(a)!=digest(b)):
                    return finish("Reference project rule content differs")
            checkpoint()
            save("baseline_verification")
            check = engine._verify(project,best)
            report = _report(engine,project,best,check)
            result["before_unconnected"] = result["after_unconnected"] = check["drc"].get("unconnected")
            if quality_issues(check) or _report_issues(report):
                return finish("Source native checks do not qualify for local repair")
            save("reference_verification")
            refcheck = engine._verify(reference_project,reference_revision)
            if refcheck["status"] != "passed" or quality_issues(refcheck):
                return finish("Reference does not pass fresh native verification")
            result["reference_verification_id"] = refcheck["verification_id"]
            if check["status"] == "passed":
                return finish()
            while len(result["attempts"]) < 12 and time.monotonic()-started < 1800:
                checkpoint()
                data,root = engine._verified(project,best)
                source = root / "design" / data["board"]
                target_nets = connection_counts(report,_net_index(source))
                adopted = False
                for plan in plans(source,reference,target_nets):
                    if not touches_findings(source,plan,report["unconnected_items"]):
                        continue
                    if len(result["attempts"]) >= 12 or time.monotonic()-started >= 1800:
                        break
                    checkpoint()
                    work = evidence / f"attempt-{len(result['attempts'])+1:02d}"
                    work.mkdir()
                    attempt = {"strategy":"reference_multinet","parent_revision":best,
                               "net":", ".join(plan["nets"]),"plan":plan,"status":"running"}
                    result["attempts"].append(attempt)
                    save("proposal")
                    proposed = propose(source,reference,work,plan)
                    attempt["proposal"] = proposed
                    child,childroot,_ = engine._new(project,best)
                    shutil.copy2(work / proposed["output"],childroot / "design" / data["board"])
                    engine._seal(project,child,childroot,data["board"],best,"repair_candidate",
                        {"reference_project":reference_project,"reference_revision":reference_revision,
                         "reference_digest":refdata["digest"],"scope_sha256":proposed["scope_sha256"]})
                    attempt["candidate_revision"] = child
                    checkpoint()
                    save("candidate_verification")
                    after = engine._verify(project,child)
                    after_report = _report(engine,project,child,after)
                    _report(engine,reference_project,reference_revision,refcheck)
                    comparison = compare_checks(check,after,_report(engine,project,best,check),after_report,
                        _net_index(source),_net_index(childroot / "design" / data["board"]),plan["nets"])
                    if _erc_violations(_erc_report(engine,project,child,after))-_erc_violations(_erc_report(engine,project,best,check)):
                        comparison["reasons"].append("New native ERC findings")
                    comparison["accepted"] = not comparison["reasons"]
                    attempt.update(comparison=comparison,status="accepted" if comparison["accepted"] else "rejected")
                    if comparison["accepted"]:
                        best,check,report = child,after,after_report
                        result["after_unconnected"] = after["drc"]["unconnected"]
                        adopted = True
                    save("accepted" if adopted else "candidate_rejected")
                    if adopted:
                        if check["status"] == "passed":
                            return finish()
                        break
                if not adopted:
                    return finish("No native-acceptable local reference patch")
            return finish("Reference repair budget exhausted; retained verified improvements")
        except (ValueError,OSError,KeyError,TypeError,IndexError) as error:
            return finish(type(error).__name__+": "+str(error))
