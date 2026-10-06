"""Native-finding scopes for bounded cleanup of newly generated routing."""
import math
from pathlib import Path
import shutil
from uuid import uuid4

from shapely.geometry import Point, LineString

from . import catalog
from .repair_geometry import _read, _board, _serialize
from .repair import (_net_index, _report, _erc_report, _violations, _erc_violations,
                     connection_counts, quality_issues, _report_issues)
from .board import read_board, _locked
from .routing_clearance import pad_envelope
from .storage import read_json, digest, write_json
from .ses_widths import raise_minima


def normalize_route_widths(board_path, ses, output):
    rules = read_json(board_path.with_suffix(".kicad_pro"))
    floor = rules["board"]["design_settings"]["rules"]["min_track_width"]
    per_net = {net:row.get("declared_min_width_mm",row.get("minimums",{}).get("track_width"))
               for net,row in rules.get("pcb_weaver_net_rules",{}).items()}
    board = read_board(board_path)
    return raise_minima(ses,output,floor,per_net,{n["name"] for n in board["nets"]})


def clearance_scopes(source, report, margin=0.8):
    if type(margin) not in (int,float) or not math.isfinite(margin) or not 0 < margin <= 2:
        raise ValueError("Clearance scope margin must be positive and at most 2 mm")
    ast,_ = _read(source)
    parsed = _board(ast,source=True)
    index = _net_index(source)
    copper = {c["id"]:c["geometry"] for c in parsed["copper"]}
    clusters = []
    for finding in report["violations"]:
        if finding["severity"] != "error" or finding["type"] != "clearance":
            continue
        ids = {str(item["uuid"]) for item in finding["items"]}
        if len(ids) < 2 or any(not index.get(i) for i in ids):
            raise ValueError("Clearance finding has an unknown electrical object")
        joined = [c for c in clusters if c["ids"] & ids]
        points = [(item["pos"]["x"],item["pos"]["y"]) for item in finding["items"]]
        for cluster in joined:
            ids.update(cluster["ids"])
            points.extend(cluster["points"])
            clusters.remove(cluster)
        clusters.append({"ids":ids,"points":points})
    scopes = []
    for cluster in clusters:
        selected = [copper[i] for i in cluster["ids"] if i in copper]
        if not selected or any(g["kind"] != "segment" or g["layer"] != "F.Cu" for g in selected):
            continue
        points = cluster["points"][:]
        for g in selected:
            x0,y0,x1,y1 = g["bounds"]
            points.extend(((x0,y0),(x1,y1)))
        if any(type(v) not in (int,float) or not math.isfinite(v) for point in points for v in point):
            raise ValueError("Non-finite native finding position")
        outline = parsed["outline"]
        region = [max(outline[0],min(p[0] for p in points)-margin),
                  max(outline[1],min(p[1] for p in points)-margin),
                  min(outline[2],max(p[0] for p in points)+margin),
                  min(outline[3],max(p[1] for p in points)+margin)]
        nets = sorted({index[i] for i in cluster["ids"]})
        area = (region[2]-region[0])*(region[3]-region[1])
        if len(nets) <= 8 and 0 < area <= 100 and region[0] < region[2] and region[1] < region[3]:
            scopes.append({"nets":nets,"region":region,"source_items":sorted(cluster["ids"]),"margin_mm":margin})
    return scopes


def dangling_via_ids(report):
    identities = []
    for finding in report["violations"]:
        if finding["type"] != "via_dangling":
            continue
        items = finding["items"]
        if (finding["severity"] != "warning" or finding.get("excluded")
                or len(items) != 1 or not isinstance(items[0].get("uuid"), str)
                or not items[0]["uuid"]):
            raise ValueError("Malformed or excluded dangling-via finding")
        identities.append(items[0]["uuid"])
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate dangling-via finding")
    return sorted(identities)


def _retained_partitions(path, net, removed):
    from .negotiated_reroute import islands
    groups = ({item[0] for item in group} - removed for group in islands(path, net, None))
    return sorted(sorted(group) for group in groups if group)


def remove_redundant_vias(source, output, identities, *, source_sha256, original):
    """Copy-only removal at exact two-segment joints; never infer dangling from geometry alone."""
    source, output, original = Path(source), Path(output), Path(original)
    if output.exists() or output.is_symlink() or output.resolve() in {source.resolve(), original.resolve()}:
        raise ValueError("Via cleanup output must be new")
    if (not isinstance(identities, list) or not 1 <= len(identities) <= 32
            or any(not isinstance(i, str) or not i for i in identities)
            or len(set(identities)) != len(identities)):
        raise ValueError("One to 32 unique via UUIDs required")
    ast, sha = _read(source)
    original_ast, original_sha = _read(original)
    if sha != source_sha256:
        raise ValueError("Via cleanup source hash changed")
    fixture = _board(original_ast, source=True)
    if fixture["copper"] or read_board(original)["unsupported"]:
        raise ValueError("Via cleanup requires a zero-copper original fixture")
    pcb, board = _board(ast, source=True), read_board(source)
    if board["unsupported"]:
        raise ValueError("Unsupported via cleanup geometry")
    copper = {c["id"]: c for c in pcb["copper"]}
    pads = [pad_envelope(p) for f in board["footprints"] for p in f["pads"]]
    removed, contacts, nets = set(identities), [], set()
    for identity in identities:
        item = copper.get(identity)
        if item is None or item["geometry"]["kind"] != "via" or _locked(item["node"]):
            raise ValueError("Dangling UUID must resolve to an unlocked through via")
        via = item["geometry"]
        center = Point(via["at"])
        # Conservative contact detection includes a 1 nm guard; shared endpoints
        # below must still be exactly equal, with no snapping or tolerance.
        touching = []
        for other in pcb["copper"]:
            if other["id"] == identity:
                continue
            g = other["geometry"]
            core = Point(g["at"]) if g["kind"] == "via" else LineString([g["start"], g["end"]])
            radius = g["size"] / 2 if g["kind"] == "via" else g["width"] / 2
            if core.distance(center) <= radius + via["size"] / 2 + .000001:
                touching.append(other)
        if any(p.distance(center) <= via["size"] / 2 + .000001 for p in pads):
            raise ValueError("Dangling via touches a pad")
        if (len(touching) != 2
                or any(c["geometry"]["kind"] != "segment" or c["geometry"]["net"] != via["net"]
                       or via["at"] not in [c["geometry"]["start"], c["geometry"]["end"]]
                       for c in touching)
                or touching[0]["geometry"]["layer"] != touching[1]["geometry"]["layer"]):
            raise ValueError("Via is not an exact same-net single-layer two-segment joint")
        contacts.append({"uuid": identity, "net": via["net"], "at": via["at"],
                         "layer": touching[0]["geometry"]["layer"],
                         "retained_segments": sorted(c["id"] for c in touching),
                         "removed_node": _serialize(item["node"]).decode("utf-8")})
        nets.add(via["net"])
    before = {net: _retained_partitions(source, net, removed) for net in sorted(nets)}
    deleted_nodes = {id(copper[i]["node"]) for i in identities}
    stripped = [node for node in ast if id(node) not in deleted_nodes]
    if digest(source) != sha or digest(original) != original_sha:
        raise ValueError("Via cleanup input changed")
    with output.open("xb") as stream:
        stream.write(_serialize(stripped))
    if _read(output)[0] != stripped:
        raise ValueError("Via cleanup changed retained AST")
    after = {net: _retained_partitions(output, net, removed) for net in sorted(nets)}
    if before != after:
        raise ValueError("Via cleanup changed retained connectivity partitions")
    if digest(source) != sha or digest(original) != original_sha:
        raise ValueError("Via cleanup input changed")
    return {"status": "proposed", "source_sha256": sha, "original_sha256": original_sha,
            "output_sha256": digest(output), "removed_ids": sorted(removed), "contacts": contacts,
            "retained_partitions": before, "partitions_preserved": True,
            "retained_ast_preserved": True, "manufacturing_authorized": False,
            "requires_native_verification": True}


def cleanup_dangling_vias(engine, project, revision, check, original_revision, baseline_check,
                          *, checkpoint=lambda: True):
    """One bounded candidate, authenticated parent reports, and child-only native verification."""
    from .completion import _preserved_state
    with engine.store.lock(project):
        data, parent = engine._verified(project, revision)
        original_data, original_folder = engine._verified(project, original_revision)
        work = catalog.artifact_path(parent, parent / "repairs" / ("via-cleanup-" + uuid4().hex[:12]))
        work.mkdir(parents=True)
        result = {"status": "blocked", "revision": revision, "source_revision": revision,
                  "source_digest": data["digest"], "original_revision": original_revision,
                  "manufacturing_authorized": False, "native_status": "not_run"}

        def finish(reason=None):
            if reason:
                result["reason"] = reason
            write_json(work / "result.json", result)
            return {**result, "evidence_path": str(work / "result.json")}

        try:
            old_report = _report(engine, project, revision, check)
            baseline_report = _report(engine, project, original_revision, baseline_check)
            old_erc = _erc_report(engine, project, revision, check)
            identities = sorted(set(dangling_via_ids(old_report)) - set(dangling_via_ids(baseline_report)))
            result.update(removed_ids=identities, verification_id=check["verification_id"],
                          evidence_hashes=check["evidence_hashes"],
                          original_verification_id=baseline_check["verification_id"],
                          original_evidence_hashes=baseline_check["evidence_hashes"])
            issues = quality_issues(check) + _report_issues(old_report)
            if issues or not identities:
                return finish("Via cleanup is not eligible: " + "; ".join(issues or ["No new dangling vias"]))
            preserved = _preserved_state(engine, project, revision)
            if preserved != _preserved_state(engine, project, original_revision):
                return finish("Via cleanup source changed original nonrouting state")
            if not checkpoint():
                return finish("Scheduling budget exhausted before via cleanup")
            source = parent / "design" / data["board"]
            target = work / "proposal.kicad_pcb"
            proof = remove_redundant_vias(source, target, identities,
                source_sha256=check["drc"]["source_sha256"],
                original=original_folder / "design" / original_data["board"])
            result["proof"] = proof
            finish()
            engine._verified(project, revision)
            child, child_folder, _ = engine._new(project, revision)
            destination = child_folder / "design" / data["board"]
            shutil.copy2(target, destination)
            if digest(destination) != proof["output_sha256"]:
                raise ValueError("Via cleanup proposal changed before sealing")
            engine._seal(project, child, child_folder, data["board"], revision, "repair_candidate",
                         {"repair_method": "redundant_dangling_vias", "cleanup_proof": proof})
            result["candidate_revision"] = child
            finish()
            if not checkpoint():
                return finish("Scheduling budget exhausted before cleanup native verification")
            after = engine._verify(project, child)
            result.update(verification=after, native_status=after["status"])
            new_report = _report(engine, project, child, after)
            new_erc = _erc_report(engine, project, child, after)
            reasons = quality_issues(after) + _report_issues(new_report)
            old_counts = connection_counts(old_report, _net_index(source))
            new_counts = connection_counts(new_report, _net_index(destination))
            if (sum(old_counts.values()) != check["drc"]["unconnected"]
                    or sum(new_counts.values()) != after["drc"]["unconnected"]):
                reasons.append("Native connection summary disagrees with actual report")
            if new_counts - old_counts:
                reasons.append("Via cleanup increased missing connections on a net")
            for kind in ("drc", "erc"):
                if after[kind].get("warnings", 0) > check[kind].get("warnings", 0):
                    reasons.append("Via cleanup increased " + kind.upper() + " warnings")
            if _violations(new_report) - _violations(old_report) or _erc_violations(new_erc) - _erc_violations(old_erc):
                reasons.append("Via cleanup introduced native findings")
            if set(dangling_via_ids(new_report)) & set(identities):
                reasons.append("Target dangling-via findings remain")
            if _preserved_state(engine, project, child) != preserved:
                reasons.append("Via cleanup changed nonrouting state")
            if digest(destination) != proof["output_sha256"]:
                reasons.append("Via cleanup candidate changed after proof")
            # Rebind the existing parent evidence; never run verification on the parent.
            _report(engine, project, revision, check)
            _erc_report(engine, project, revision, check)
            engine._verified(project, original_revision)
            result["comparison"] = {"accepted": not reasons, "reasons": reasons,
                                    "before_by_net": dict(old_counts), "after_by_net": dict(new_counts)}
            if not reasons:
                result.update(status="improved", revision=child)
            return finish("; ".join(reasons) if reasons else None)
        except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
            return finish(type(error).__name__ + ": " + str(error))
