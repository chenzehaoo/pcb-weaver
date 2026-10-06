"""Deterministic local placement optimization and explicitly scoped auditing.

Feasible means the supported placement/width/drill checks pass. Electrical
clearance, voltage safety and release checks require external evidence, recorded
in external_checks; they are never asserted to pass by this module.
"""

from __future__ import annotations

from copy import deepcopy
from itertools import combinations
import math
import time

import numpy as np
from scipy.optimize import minimize

from .board import collision_boxes, copper_layer_issues
from .models import PlacementOptions


_TOL = 1e-6


def _finite(value: object, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"Invalid {name}")
    return number


def _bounds(value: object, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{name} must have four coordinates")
    result = [_finite(v, name) for v in value]
    if result[0] >= result[2] or result[1] >= result[3]:
        raise ValueError(f"{name} must have positive area")
    return result


def _names(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError(f"{name} must be a list of nonempty strings")
    return value


def _validate(constraints: dict) -> list[str]:
    if constraints.get("schema_version") != 1 or isinstance(constraints.get("schema_version"), bool):
        raise ValueError("constraints.schema_version must be 1")
    known = {"schema_version", "board", "fixed_references", "edge_overhang_references", "critical_nets", "proximity", "regions",
             "net_rules", "fabrication", "release", "placement"}
    unknowns = [f"Unknown constraint field: {k}" for k in constraints.keys() - known]
    fields = {"board": {"layers", "max_voltage", "edge_clearance_mm", "component_gap_mm"},
              "fabrication": {"min_track_mm", "min_clearance_mm", "min_via_drill_mm"},
              "release": {"require_erc"}}
    for section, allowed in fields.items():
        values = constraints.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"{section} must be an object")
        unknowns.extend(f"Unknown constraint: {section}.{k}" for k in values.keys() - allowed)
        for key, value in values.items():
            if key in allowed and key != "require_erc":
                _finite(value, f"{section}.{key}", 0)
        if "require_erc" in values and not isinstance(values["require_erc"], bool):
            raise ValueError("require_erc must be boolean")
    if "layers" in constraints.get("board", {}) and constraints["board"]["layers"] not in {2, 4, 6, 8}:
        unknowns.append("Only 2/4/6/8-copper-layer constraint profiles are supported")
    PlacementOptions.model_validate(constraints.get("placement", {}))
    for key in ("fixed_references", "edge_overhang_references", "critical_nets"):
        _names(constraints.get(key, []), key)
    for section, allowed in {"proximity": {"reference", "target", "max_distance_mm"},
                             "regions": {"references", "bounds"},
                             "net_rules": {"nets", "min_width_mm"}}.items():
        values = constraints.get(section, [])
        if not isinstance(values, list):
            raise ValueError(f"{section} must be a list")
        for item in values:
            if not isinstance(item, dict) or not allowed <= item.keys():
                raise ValueError(f"Missing fields in {section}")
            unknowns.extend(f"Unknown constraint: {section}.{key}" for key in item.keys() - allowed)
            if section == "proximity":
                _names([item["reference"], item["target"]], section)
                _finite(item["max_distance_mm"], section, 0)
            elif section == "regions":
                _names(item["references"], section)
                _bounds(item["bounds"], section)
            else:
                _names(item["nets"], section)
                _finite(item["min_width_mm"], section, 0)
    return unknowns


def _index(board: dict) -> dict:
    result = {}
    for fp in board["footprints"]:
        reference = fp["reference"]
        if reference in result:
            raise ValueError(f"Duplicate footprint reference: {reference}")
        for field in ("x", "y", "rotation"):
            _finite(fp[field], f"{reference}.{field}")
        envelope = fp["bounds"]
        if not isinstance(envelope, (list, tuple)) or len(envelope) != 4:
            raise ValueError(f"{reference}.bounds must have four coordinates")
        for coordinate in envelope:
            _finite(coordinate, f"{reference}.bounds")
        if envelope[0] > envelope[2] or envelope[1] > envelope[3]:
            raise ValueError(f"{reference}.bounds is inverted")
        for pad in fp["pads"]:
            _finite(pad["x"], f"{reference} pad x")
            _finite(pad["y"], f"{reference} pad y")
        result[reference] = fp
    return result


def _separation(a: list, b: list) -> float:
    # Positive: separated along at least one axis. Negative: AABB penetration.
    return max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])


def _containment(inner: list, outer: list, clearance: float = 0) -> list[float]:
    return [inner[0] - outer[0] - clearance, inner[1] - outer[1] - clearance,
            outer[2] - inner[2] - clearance, outer[3] - inner[3] - clearance]


def _measure(board: dict, constraints: dict) -> dict:
    fps = board["footprints"]
    nets: dict[str, list] = {}
    for fp in fps:
        for pad in fp["pads"]:
            if pad["net"]:
                nets.setdefault(pad["net"], []).append((pad["x"], pad["y"]))
    lengths = {name: max(p[0] for p in pads) - min(p[0] for p in pads)
                     + max(p[1] for p in pads) - min(p[1] for p in pads)
               for name, pads in nets.items()}
    critical = set(constraints.get("critical_nets", []))
    overlap = 0.0
    gaps = []
    for first, second in combinations(fps, 2):
        for a, b in collision_boxes(first, second):
            overlap += max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
            gaps.append(_separation(a, b))
    outline = board["outline"]
    edge = min((min(_containment(edge_envelope(fp,constraints), outline["bounds"])) for fp in fps), default=None) if outline["supported"] else None
    return {"hpwl_mm": sum(lengths.values()),
            "critical_hpwl_mm": sum(v for k, v in lengths.items() if k in critical),
            "weighted_hpwl_mm": sum(v * (2 if k in critical else 1) for k, v in lengths.items()),
            "net_hpwl_mm": lengths, "pairwise_aabb_overlap_mm2": overlap,
            "minimum_axis_gap_mm": min(gaps, default=None), "minimum_edge_clearance_mm": edge,
            "routed_length_mm": board.get("routed_length_mm"),
            "metric_scope": "Pad-based HPWL estimate; same-face body and cross-face through-pad AABB pairs (overlap sums may double count); not routed length or signoff"}


def _margins(board: dict, constraints: dict) -> list[tuple[str, str, float]]:
    fps = _index(board)
    rules = constraints.get("board", {})
    edge = rules.get("edge_clearance_mm", 0)
    gap = rules.get("component_gap_mm", 0)
    result = []
    if board["outline"]["supported"]:
        for ref, fp in fps.items():
            result.append(("edge_clearance", ref, min(_containment(edge_envelope(fp,constraints), board["outline"]["bounds"], edge))))
    for a, b in combinations(fps.values(), 2):
        gaps = [_separation(first, second) - gap for first, second in collision_boxes(a, b)]
        if gaps:
            result.append(("component_gap", f"{a['reference']},{b['reference']}", min(gaps)))
    for rule in constraints.get("regions", []):
        for ref in rule["references"]:
            if ref in fps:
                result.append(("region", ref, min(_containment(fps[ref]["bounds"], rule["bounds"])) ))
    for rule in constraints.get("proximity", []):
        if rule["reference"] in fps and rule["target"] in fps:
            a, b = fps[rule["reference"]], fps[rule["target"]]
            distance = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
            result.append(("proximity", f"{rule['reference']},{rule['target']}", rule["max_distance_mm"] - distance))
    return result


def audit_constraints(board: dict, constraints: dict) -> dict:
    """Audit supported constraints; external_checks require separate real evidence.

    ``passed``/``feasible`` are scoped to this audit. ``signoff`` is always false.
    Fixed/locked references are held at input poses by plan_placements. A static
    board alone cannot establish whether a footprint moved from an earlier file.
    """
    unknowns = _validate(constraints) + list(board.get("unsupported", []))
    fps = _index(board)
    violations = []
    two_sided = any(fp["layer"] == "B.Cu" for fp in fps.values())
    for ref, fp in fps.items():
        if fp["layer"] not in {"F.Cu", "B.Cu"}:
            unknowns.append(f"{ref}: unknown footprint side")
        if two_sided and "through_hole_bounds" not in fp:
            unknowns.append(f"{ref}: cross-face through-hole occupancy unavailable")
        for box in fp.get("through_hole_bounds", []):
            _bounds(box, f"{ref}.through_hole_bounds")
        if fp["bounds"][0] == fp["bounds"][2] or fp["bounds"][1] == fp["bounds"][3]:
            unknowns.append(f"{ref}: component envelope has zero area")
    if not board["outline"].get("supported"):
        unknowns.append("Unsupported board outline")
    else:
        _bounds(board["outline"]["bounds"], "outline")
    requested_refs = set(constraints.get("fixed_references", [])) | set(constraints.get("edge_overhang_references",[]))
    for rule in constraints.get("regions", []):
        requested_refs.update(rule["references"])
    for rule in constraints.get("proximity", []):
        requested_refs.update([rule["reference"], rule["target"]])
    for ref in sorted(requested_refs - fps.keys()):
        violations.append({"code": "missing_reference", "reference": ref})
    existing_nets = {net["name"] for net in board["nets"] if net["name"]}
    requested_nets = set(constraints.get("critical_nets", []))
    for rule in constraints.get("net_rules", []):
        requested_nets.update(rule["nets"])
    for name in sorted(requested_nets - existing_nets):
        violations.append({"code": "missing_net", "net": name})
    requested_layers = constraints.get("board", {}).get("layers")
    unknowns.extend(copper_layer_issues(board.get("copper_layers")))
    stackup = board.get("stackup_copper_layers")
    if stackup is not None and (len(stackup) != len(board.get("copper_layers", [])) or set(stackup) != set(board.get("copper_layers", []))):
        unknowns.append("Stackup copper layers disagree with declared copper layers")
    if requested_layers is not None:
        layers = board.get("copper_layers")
        if layers is None:
            unknowns.append("Copper layer count unavailable")
        elif len(layers) != requested_layers:
            violations.append({"code": "copper_layers", "required": requested_layers, "actual": len(layers)})
    for code, reference, margin in _margins(board, constraints):
        if margin < -_TOL:
            violations.append({"code": code, "reference": reference, "shortfall_mm": -margin})
    baseline = board.get("_baseline_placements", {})
    for ref, pose in baseline.items():
        if ref not in fps:
            violations.append({"code": "missing_reference", "reference": ref})
        elif fps[ref]["locked"] or pose.get("locked") or ref in constraints.get("fixed_references", []):
            if any(abs(fps[ref][k] - pose[k]) > _TOL for k in ("x", "y", "rotation")) or ("layer" in pose and fps[ref]["layer"] != pose["layer"]):
                violations.append({"code": "fixed_placement", "reference": ref})
    fabrication = constraints.get("fabrication", {})
    tracks = board.get("track_items")
    vias = board.get("via_items")
    if (fabrication.get("min_track_mm") is not None or constraints.get("net_rules")):
        if tracks is None or len(tracks) != board.get("tracks", 0):
            unknowns.append("Complete track width geometry unavailable")
        for index, track in enumerate(tracks or []):
            if "net" not in track or "width_mm" not in track:
                unknowns.append(f"Track {index} width/net unavailable")
                continue
            required = fabrication.get("min_track_mm", 0)
            for rule in constraints.get("net_rules", []):
                if track["net"] in rule["nets"]:
                    required = max(required, rule["min_width_mm"])
            actual = _finite(track["width_mm"], "track width", 0)
            if actual + _TOL < required:
                violations.append({"code": "track_width", "track_index": index, "net": track["net"],
                                   "actual_mm": actual, "required_mm": required})
    if "min_via_drill_mm" in fabrication:
        if vias is None or len(vias) != board.get("vias", 0):
            unknowns.append("Complete via drill geometry unavailable")
        for index, via in enumerate(vias or []):
            if "drill_mm" not in via:
                unknowns.append(f"Via {index} drill unavailable")
                continue
            actual = _finite(via["drill_mm"], "via drill", 0)
            if actual + _TOL < fabrication["min_via_drill_mm"]:
                violations.append({"code": "via_drill", "via_index": index, "actual_mm": actual,
                                   "required_mm": fabrication["min_via_drill_mm"]})
    external_checks = [{"code": "electrical_connectivity", "status": "not_checked", "requires": "KiCad DRC"}]
    if "min_clearance_mm" in fabrication:
        external_checks.append({"code": "copper_clearance", "status": "not_checked",
                                "required_mm": fabrication["min_clearance_mm"],
                                "requires": "KiCad DRC with compiled project constraints"})
    if "max_voltage" in constraints.get("board", {}):
        external_checks.append({"code": "voltage_safety", "status": "not_checked", "requires": "Electrical design review"})
    if constraints.get("release", {}).get("require_erc"):
        external_checks.append({"code": "erc", "status": "not_checked", "requires": "KiCad schematic ERC"})
    passed = not violations and not unknowns
    return {"status": "failed" if violations else "unknown" if unknowns else "passed",
            "passed": passed, "feasible": passed, "violations": violations, "unknowns": sorted(set(unknowns)),
            "metrics": _measure(board, constraints), "external_checks": external_checks,
            "scope": "placement geometry, copper layer count, track widths and via drills",
            "signoff": False, "all_constraints_verified": False}


def _translated(board: dict, refs: list[str], vector: np.ndarray) -> dict:
    result = deepcopy(board)
    updates = {ref: (float(vector[2 * i]), float(vector[2 * i + 1])) for i, ref in enumerate(refs)}
    result["_baseline_placements"] = deepcopy(board.get("_baseline_placements", {
        fp["reference"]: {k: fp[k] for k in ("x", "y", "rotation", "layer", "locked")}
        for fp in board["footprints"]}))
    nets: dict[str, list] = {}
    for fp in result["footprints"]:
        x, y = updates.get(fp["reference"], (fp["x"], fp["y"]))
        dx, dy = x - fp["x"], y - fp["y"]
        fp["x"], fp["y"] = x, y
        fp["bounds"] = [value + (dx if i % 2 == 0 else dy) for i, value in enumerate(fp["bounds"])]
        if "through_hole_bounds" in fp:
            fp["through_hole_bounds"] = [[value + (dx if i % 2 == 0 else dy) for i, value in enumerate(box)]
                                         for box in fp["through_hole_bounds"]]
        for pad in fp["pads"]:
            pad["x"] += dx
            pad["y"] += dy
            if pad["net"]:
                nets.setdefault(pad["net"], []).append({"reference": fp["reference"], "number": pad["number"], "x": pad["x"], "y": pad["y"]})
    result["nets"] = [{"name": name, "pads": pads} for name, pads in sorted(nets.items())]
    return result


def edge_envelope(fp, constraints):
    if fp["reference"] not in constraints.get("edge_overhang_references",[]):
        return fp["bounds"]
    from .routing_clearance import pad_envelope
    if not fp["pads"]:
        raise ValueError("Body overhang requires physical pads for edge containment")
    boxes = [pad_envelope(pad).bounds for pad in fp["pads"]]
    return [min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]


def completion_candidates(board, constraints, plan, count, spread_mm):
    """Add bounded, audited passive-spreading alternatives to duplicate legalizations."""
    spread_mm = _finite(spread_mm,"completion spread",0)
    if spread_mm > 2:
        raise ValueError("Completion spread exceeds 2 mm")
    if plan["status"] != "ok" or not spread_mm:
        return plan
    def signature(candidate):
        return tuple(sorted((p["reference"],p["x"],p["y"],p["rotation"]) for p in candidate["placements"]))
    unique,seen = [],set()
    for candidate in plan["candidates"]:
        if candidate["feasible"] and signature(candidate) not in seen:
            unique.append(candidate)
            seen.add(signature(candidate))
    if not unique or len(unique) >= count:
        return plan
    refs = [fp["reference"] for fp in board["footprints"]]
    poses = {p["reference"]:p for p in unique[0]["placements"]}
    base = _translated(board,refs,np.array([poses[r][a] for r in refs for a in ("x","y")]))
    anchors = [fp for fp in base["footprints"] if len(fp["pads"]) >= 8]
    fixed = set(constraints.get("fixed_references",[]))
    local = deepcopy(constraints)
    local.setdefault("placement",{})["algorithm"] = "legalize"
    for index in range(1,count):
        if len(unique) >= count or not anchors:
            break
        moved,vector = [],[]
        for fp in base["footprints"]:
            if fp["locked"] or fp["reference"] in fixed or not 1 <= len(fp["pads"]) <= 4:
                continue
            anchor = min(anchors,key=lambda a:math.hypot(fp["x"]-a["x"],fp["y"]-a["y"]))
            dx,dy = fp["x"]-anchor["x"],fp["y"]-anchor["y"]
            length = math.hypot(dx,dy)
            if length < 1e-6 or length > 20:
                continue
            # A second direction changes channel geometry without rotating packages.
            if index % 2 == 0:
                dx,dy = -dy,dx
            amount = spread_mm * index / (count-1)
            moved.append(fp["reference"])
            vector.extend([fp["x"]+amount*dx/length,fp["y"]+amount*dy/length])
        if not moved:
            break
        seed = _translated(base,moved,np.array(vector))
        alternative = plan_placements(seed,local,1)
        if alternative["status"] != "ok":
            continue
        candidate = alternative["candidates"][0]
        if signature(candidate) in seen:
            continue
        updates = {p["reference"]:p for p in candidate["placements"]}
        if any(math.hypot(updates[r]["x"]-poses[r]["x"],updates[r]["y"]-poses[r]["y"]) > 2*spread_mm+1e-6 for r in refs):
            continue
        placed = _translated(board,refs,np.array([updates[r][a] for r in refs for a in ("x","y")]))
        audit = audit_constraints(placed,constraints)
        if not audit["passed"]:
            continue
        displacement = [math.hypot(fp["x"]-updates[fp["reference"]]["x"],fp["y"]-updates[fp["reference"]]["y"]) for fp in board["footprints"]]
        measured = {**candidate["metrics"],**audit["metrics"]}
        measured.update(total_displacement_mm=sum(displacement),maximum_displacement_mm=max(displacement,default=0),
                        squared_displacement_mm2=sum(d*d for d in displacement),moved_components=sum(d>1e-6 for d in displacement),
                        hpwl_improvement_mm=plan["baseline"]["metrics"]["hpwl_mm"]-measured["hpwl_mm"])
        candidate.update(id=f"spread-{index:03d}",metrics=measured,optimizer={**candidate["optimizer"],
                         "completion_spread_mm":spread_mm,"maximum_change_from_primary_mm":2*spread_mm,
                         "purpose":"Bounded channel alternatives; routability requires actual routing"})
        unique.append(candidate)
        seen.add(signature(candidate))
    return {**plan,"candidates":unique,"completion_diversification":"Primary retained; distinct bounded passive-spreading candidates independently audited"}


def plan_placements(board: dict, constraints: dict, count: int = 3) -> dict:
    """Plan local HPWL optimization or minimum-displacement legalization.

    This is a local nonlinear optimizer. Candidate feasibility comes from an
    independent geometric audit, never from the optimizer's success flag.
    """
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
        raise ValueError("count must be an integer from 1 to 20")
    baseline = audit_constraints(board, constraints)
    options = PlacementOptions.model_validate(constraints.get("placement", {}))
    if baseline["unknowns"] or board.get("tracks", 0) or board.get("vias", 0):
        return {"status": "blocked", "candidates": [], "baseline": baseline,
                "reason": "Unsupported geometry/constraints or routed board", "seed": options.seed, "signoff": False}
    fps = _index(board)
    fixed = set(constraints.get("fixed_references", [])) | {ref for ref, fp in fps.items() if fp["locked"]}
    refs = sorted(fps.keys() - fixed)
    algorithm = options.algorithm
    if algorithm == "auto":
        algorithm = "block_coordinate" if len(refs) > 32 else "slsqp"
    if algorithm == "slsqp" and len(refs) > 32:
        return {"status": "blocked", "candidates": [], "baseline": baseline, "seed": options.seed,
                "reason": "Full-board SLSQP is limited to 32 movable footprints; use auto or block_coordinate", "signoff": False}
    vector = np.array([fps[ref][axis] for ref in refs for axis in ("x", "y")], dtype=float)
    outline = board["outline"]["bounds"]
    edge = constraints.get("board", {}).get("edge_clearance_mm", 0)
    bounds = []
    for ref in refs:
        fp = fps[ref]
        allowed = [outline[0] + edge, outline[1] + edge, outline[2] - edge, outline[3] - edge]
        for region in constraints.get("regions", []):
            if ref in region["references"]:
                box = region["bounds"]
                allowed = [max(allowed[0], box[0]), max(allowed[1], box[1]), min(allowed[2], box[2]), min(allowed[3], box[3])]
        for i, axis in enumerate(("x", "y")):
            envelope = edge_envelope(fp,constraints)
            bounds.append((allowed[i] - envelope[i] + fp[axis], allowed[i + 2] - envelope[i + 2] + fp[axis]))
    if any(low > high for low, high in bounds):
        return {"status": "infeasible", "candidates": [], "baseline": baseline,
                "reason": "Component envelope cannot fit board/region", "seed": options.seed, "signoff": False}
    rng = np.random.default_rng(options.seed)
    candidates = []

    def objective(v: np.ndarray) -> float:
        return _measure(_translated(board, refs, v), constraints)["weighted_hpwl_mm"]

    def margins(v: np.ndarray) -> np.ndarray:
        return np.array([value for _, _, value in _margins(_translated(board, refs, v), constraints)])

    for index in range(count if refs else 1):
        started = time.perf_counter()
        if refs and algorithm == "legalize":
            from .placement_large import legalize_minimum_displacement
            v, optimizer = legalize_minimum_displacement(board, constraints, refs, bounds, options, index)
        elif refs and algorithm == "block_coordinate":
            from .placement_large import optimize_blocks
            v, optimizer = optimize_blocks(board, constraints, refs, bounds, options, index)
        elif refs:
            start = np.array([min(high, max(low, value)) for value, (low, high) in zip(vector, bounds)])
            if index:
                start = np.array([rng.uniform(low, high) for low, high in bounds])
            result = minimize(objective, start, method="SLSQP", bounds=bounds,
                              constraints=[{"type": "ineq", "fun": margins}],
                              options={"maxiter": options.max_iterations, "ftol": 1e-8, "eps": 1e-5})
            v = result.x
            optimizer = {"method": "scipy.optimize.minimize/SLSQP", "success": bool(result.success),
                         "message": str(result.message), "iterations": int(result.get("nit", 0)), "evaluations": int(result.get("nfev", 0))}
        else:
            v = vector
            optimizer = {"method": "none", "success": True, "message": "All footprints fixed", "iterations": 0, "evaluations": 0}
        # Never exchange a feasible input for a failed numerical step.
        if baseline["passed"] and (not np.isfinite(v).all() or not audit_constraints(_translated(board, refs, np.round(v, 8)), constraints)["passed"]):
            v = vector.copy()
            optimizer["fallback"] = "feasible input retained after candidate audit failed"
        # Round before auditing, so serialized placements satisfy the same checks.
        v = np.where(v == vector, vector, np.round(v, 8)) if algorithm == "legalize" else np.round(v, 8)
        placed = _translated(board, refs, v)
        audit = audit_constraints(placed, constraints)
        metrics = audit["metrics"]
        displacement = [math.hypot(fp["x"] - fps[fp["reference"]]["x"], fp["y"] - fps[fp["reference"]]["y"]) for fp in placed["footprints"]]
        metrics["total_displacement_mm"] = sum(displacement)
        metrics["maximum_displacement_mm"] = max(displacement, default=0)
        metrics["squared_displacement_mm2"] = sum(value * value for value in displacement)
        metrics["moved_components"] = sum(value > _TOL for value in displacement)
        metrics["hpwl_improvement_mm"] = baseline["metrics"]["hpwl_mm"] - metrics["hpwl_mm"]
        metrics["optimization_seconds"] = time.perf_counter() - started
        candidates.append({"id": f"candidate-{index + 1:03d}",
                           "placements": [{k: fp[k] for k in ("reference", "x", "y", "rotation")} for fp in placed["footprints"]],
                           "metrics": metrics, "feasible": audit["feasible"], "violations": audit["violations"],
                           "unknowns": audit["unknowns"], "external_checks": audit["external_checks"], "optimizer": optimizer})
    if algorithm == "legalize":
        candidates.sort(key=lambda candidate: (not candidate["feasible"],
                                               candidate["metrics"]["squared_displacement_mm2"],
                                               candidate["metrics"]["weighted_hpwl_mm"]))
    else:
        candidates.sort(key=lambda candidate: (not candidate["feasible"], candidate["metrics"]["weighted_hpwl_mm"]))
    return {"status": "ok" if any(c["feasible"] for c in candidates) else "infeasible",
            "candidates": candidates, "baseline": baseline, "seed": options.seed, "algorithm": algorithm,
            "scope": "Local translation optimization; fixed rotations; no global optimality claim", "signoff": False}
