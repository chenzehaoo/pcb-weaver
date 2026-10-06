"""Shared business operations for the CLI and MCP transports."""
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path, PureWindowsPath
import shutil
import uuid
import zipfile

import sexpdata

from .board import read_board, write_placements, compare_boards
from .compiler import compile_rules, project_rule_issues, report_rule_issues
from .eco import analyze_impact
from .models import Constraints
from .netlist import inspect_netlist
from .planning import plan_placements, audit_constraints
from .storage import Store, canonical, digest, now, read_json, write_json
from .toolchain import Toolchain

DESIGN_SUFFIXES = {".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_dru", ".kicad_mod", ".kicad_sym"}
LIBRARY_TABLES = {"fp-lib-table", "sym-lib-table"}
DESIGN_NAMES = LIBRARY_TABLES | {"PROVENANCE.json", "LICENSE", "LICENSE.txt", "LICENSE.KiCad.README", "COPYING"}


def design_files(directory):
    directory = Path(directory).resolve(strict=True)
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Symlinks and junctions in engineering snapshots are not supported")
        if path.is_file() and (path.suffix in DESIGN_SUFFIXES or path.name in DESIGN_NAMES):
            result[path.relative_to(directory).as_posix()] = digest(path)
    _validate_sheets(directory, result)
    _validate_library_tables(directory, result)
    return result


def _validate_library_tables(directory, files):
    for name in files:
        table_name = Path(name).name
        if table_name not in LIBRARY_TABLES:
            continue
        tree = sexpdata.loads((directory / name).read_text(encoding="utf-8-sig"),
                              nil=None, true=None, false=None)
        expected_tag = "fp_lib_table" if table_name == "fp-lib-table" else "sym_lib_table"
        if not isinstance(tree, list) or not tree or str(tree[0]) != expected_tag:
            raise ValueError(f"Invalid library table: {name}")
        for entry in tree[1:]:
            if not isinstance(entry, list) or not entry:
                raise ValueError(f"Invalid library entry in {name}")
            if str(entry[0]) == "version":
                continue
            if str(entry[0]) != "lib":
                raise ValueError(f"Unsupported library table entry in {name}: {entry[0]}")
            values = {}
            for field in ("uri", "type"):
                matches = [item for item in entry[1:] if isinstance(item, list)
                           and item and str(item[0]) == field]
                if len(matches) != 1 or len(matches[0]) != 2:
                    raise ValueError(f"Missing or ambiguous library {field} in {name}")
                values[field] = matches[0][1]
            if str(values["type"]) != "KiCad":
                raise ValueError(f"Only native KiCad library snapshots are supported in {name}")
            raw = values["uri"]
            if isinstance(raw, sexpdata.Symbol):
                raw = str(raw)
            if not isinstance(raw, str) or not raw:
                raise ValueError(f"Invalid library URI in {name}")
            local = raw.replace("\\", "/")
            if local.startswith("${KIPRJMOD}/"):
                local = local[len("${KIPRJMOD}/"):]
            windows = PureWindowsPath(local)
            if (not local or Path(local).is_absolute() or windows.drive or windows.root
                    or local.startswith("~") or any(c in local for c in "$%:?#")
                    or any(ord(c) < 32 for c in local)):
                raise ValueError(f"External, network or unresolved library URI in {name}: {raw}")
            # KIPRJMOD and plain relative library URIs are rooted at the imported project,
            # never at the process cwd or an arbitrarily nested table's directory.
            target = (directory / local).resolve()
            if not target.is_relative_to(directory):
                raise ValueError(f"Library URI escapes the design snapshot in {name}: {raw}")
            if table_name == "fp-lib-table":
                if not target.is_dir() or target.suffix != ".pretty":
                    raise ValueError(f"Library URI must name an existing native .pretty directory in {name}: {raw}")
                members = [p for p in target.rglob("*") if p.is_file()
                           and p.suffix.lower() in {".kicad_mod", ".kicad_sym"}]
                if not any(p.suffix == ".kicad_mod" for p in members):
                    raise ValueError(f"Footprint library has no snapshot-compatible .kicad_mod files in {name}: {raw}")
            else:
                if not target.is_file() or target.suffix != ".kicad_sym":
                    raise ValueError(f"Library URI must name an existing native .kicad_sym file in {name}: {raw}")
                members = [target]
            for member in members:
                if member.relative_to(directory).as_posix() not in files:
                    raise ValueError(f"Library file is not included in the snapshot: {member.relative_to(directory)}")


def _dependency_scope(files):
    tables = sorted(name for name in files if Path(name).name in LIBRARY_TABLES)
    return {"project_library_tables": tables,
            "project_library_uris": "validated_local_snapshot" if tables else "not_declared",
            "global_libraries_snapshotted": False,
            "scope": "Design files, schematic sheets and native libraries declared by project tables only. "
                     "Global libraries, 3D models and tool configuration are not pinned. "
                     "Embedded symbols/footprints can be analyzed without project library tables."}


def _validate_sheets(directory, files):
    dependencies = {}
    for name in files:
        if not name.endswith(".kicad_sch"):
            continue
        path = directory / name
        tree = sexpdata.loads(path.read_text(encoding="utf-8-sig"), nil=None, true=None, false=None)
        if not isinstance(tree, list) or not tree or str(tree[0]) != "kicad_sch":
            raise ValueError(f"Invalid KiCad schematic: {name}")
        dependencies[name] = []
        for sheet in tree[1:]:
            if not isinstance(sheet, list) or not sheet or str(sheet[0]) != "sheet":
                continue
            properties = [p for p in sheet[1:] if isinstance(p, list) and len(p) >= 3
                          and str(p[0]) == "property" and str(p[1]).lower() == "sheetfile"]
            if len(properties) != 1 or not isinstance(properties[0][2], str) or not properties[0][2]:
                raise ValueError(f"Missing or ambiguous Sheetfile in {name}")
            raw = properties[0][2]
            relative = Path(raw.replace("\\", "/"))
            windows = PureWindowsPath(raw)
            if relative.is_absolute() or windows.drive or windows.root or "${" in raw or raw.startswith("~"):
                raise ValueError(f"External or unresolved Sheetfile is not allowed in {name}: {raw}")
            target = (path.parent / relative).resolve()
            if not target.is_relative_to(directory):
                raise ValueError(f"Sheetfile escapes the design snapshot in {name}: {raw}")
            child = target.relative_to(directory).as_posix()
            if target.suffix != ".kicad_sch" or child not in files or not target.is_file():
                raise ValueError(f"Sheetfile is not a copied schematic in {name}: {raw}")
            dependencies[name].append(child)
    active, visited = set(), set()

    def visit(name):
        if name in active:
            raise ValueError(f"Recursive Sheetfile dependency: {name}")
        if name in visited:
            return
        active.add(name)
        for child in dependencies[name]:
            visit(child)
        active.remove(name)
        visited.add(name)

    for name in dependencies:
        visit(name)


def _audit_passed(audit):
    return (audit.get("passed") is True and audit.get("status", "passed") == "passed" and not audit.get("violations")
            and not audit.get("unknowns") and not audit.get("unsupported"))


def _audit_persisted_track_minima(board, board_path):
    """Check preserved project minima even when no SES import ran for this revision."""
    result = {"status": "blocked", "passed": False, "violations": [], "unknowns": [],
              "segments_checked": 0, "comparison_unit": "KiCad nanometre (1e-6 mm)",
              "per_net_minimum_mm": {}, "minimum_sources": {}, "observed_minimum_mm": {},
              "scope": "Persisted global and PCB Weaver per-net track minima; custom rules require native DRC"}

    def object_pairs(pairs):
        fields = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError("Duplicate project JSON key: " + key)
            fields[key] = value
        return fields

    def width_units(value, label):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(label + " must be a positive finite number")
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError(label + " must be a positive finite number")
        units = number * 1_000_000
        if units != units.to_integral_value():
            raise ValueError(label + " is not representable on KiCad's 1 nm grid")
        return int(units)

    try:
        project_path = board_path.with_suffix(".kicad_pro")
        raw = project_path.read_bytes()
        result.update(board_sha256=digest(board_path), project_sha256=hashlib.sha256(raw).hexdigest())
        project = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=object_pairs)
        if not isinstance(project, dict):
            raise ValueError("Project rules must be an object")
        global_min = width_units(project["board"]["design_settings"]["rules"]["min_track_width"], "min_track_width")
        result["global_minimum_mm"] = global_min / 1_000_000
        metadata = project.get("pcb_weaver_net_rules", {})
        if not isinstance(metadata, dict):
            raise ValueError("pcb_weaver_net_rules must be an object")
        nets = {n["name"] for n in board["nets"]}
        minima = {}
        for net, entry in metadata.items():
            if not isinstance(net, str) or not net or net not in nets:
                raise ValueError("Persisted width rule references an absent or invalid board net: " + repr(net))
            if not isinstance(entry, dict):
                raise ValueError("Persisted width entry must be an object: " + net)
            legacy = entry.get("minimums", {})
            if not isinstance(legacy, dict):
                raise ValueError("Persisted minimums must be an object: " + net)
            if "track_width" in legacy:
                width_units(legacy["track_width"], net + ".minimums.track_width")
            # An explicit zero/null/bad value is invalid, never a request to fall back.
            if "declared_min_width_mm" in entry:
                value, source = entry["declared_min_width_mm"], "declared_min_width_mm"
            else:
                value, source = legacy["track_width"], "legacy.minimums.track_width"
            minima[net] = max(global_min, width_units(value, net + "." + source))
            result["minimum_sources"][net] = source
            result["per_net_minimum_mm"][net] = minima[net] / 1_000_000
        tracks = board.get("track_items")
        if (not isinstance(tracks, list) or type(board.get("tracks")) is not int
                or len(tracks) != board["tracks"]):
            raise ValueError("Complete routed track geometry is unavailable")
        observed = {}
        for index, track in enumerate(tracks):
            if not isinstance(track, dict) or not isinstance(track.get("net"), str):
                raise ValueError("Track width/net metadata is unavailable")
            net = track["net"]
            if net and net not in nets:
                raise ValueError("Track references an unresolved board net: " + net)
            actual = width_units(track["width_mm"], "track width")
            required = minima.get(net, global_min)
            if actual < required:
                result["violations"].append({"code": "persisted_track_width", "track_index": index,
                                             "net": net, "actual_mm": actual / 1_000_000,
                                             "required_mm": required / 1_000_000,
                                             "actual_nm": actual, "required_nm": required})
            observed[net] = min(observed.get(net, actual), actual)
            result["segments_checked"] += 1
        result["observed_minimum_mm"] = {net: value / 1_000_000 for net, value in observed.items()}
    except (OSError, ValueError, KeyError, TypeError) as error:
        result["unknowns"].append("Invalid or unavailable persisted track minima: " + str(error))
    result["passed"] = not result["violations"] and not result["unknowns"]
    result["status"] = "passed" if result["passed"] else "blocked"
    return result


class EngineeringService:
    def __init__(self, root, config=None):
        self.store = Store(Path(root))
        self.toolchain = Toolchain(config or {})

    def doctor(self):
        return self.toolchain.doctor()

    def _new(self, project, parent=None):
        revision = "r-" + uuid.uuid4().hex[:16]
        folder = self.store.revision_dir(project, revision)
        (folder / "design").mkdir(parents=True, exist_ok=False)
        if parent:
            old, old_folder = self._verified(project, parent)
            shutil.copytree(old_folder / "design", folder / "design", dirs_exist_ok=True)
            shutil.copy2(old_folder / "constraints.json", folder / "constraints.json")
            return revision, folder, old
        return revision, folder, None

    def _seal(self, project, revision, folder, board_name, parent, operation, extra=None):
        files = design_files(folder / "design")
        constraint_hash = digest(folder / "constraints.json")
        checksum = hashlib.sha256(canonical({"files": files, "constraints": constraint_hash}).encode()).hexdigest()
        data = {"id": revision, "project": project, "parent": parent, "created": now(),
                "board": board_name, "operation": operation, "files": files,
                "dependency_scope": _dependency_scope(files),
                "constraints_hash": constraint_hash, "digest": checksum, **(extra or {})}
        write_json(folder / "revision.json", data)
        self.store.add_revision(data)
        return data

    def _verified(self, project, revision):
        data = self.store.revision(project, revision)
        folder = self.store.revision_dir(project, revision)
        if design_files(folder / "design") != data["files"] or digest(folder / "constraints.json") != data["constraints_hash"]:
            raise ValueError("Revision changed outside PCB Weaver; import the changed files as a new revision")
        return data, folder

    def import_project(self, project: str, board_path: str, constraints: dict | None = None,
                       parent_revision: str | None = None):
        self.store.identifier(project)
        source = Path(board_path).expanduser().resolve(strict=True)
        if source.suffix != ".kicad_pcb":
            raise ValueError("Expected a .kicad_pcb file")
        board = read_board(source)
        intent = Constraints.model_validate(constraints or {}).model_dump(mode="json")
        self._validate_references(board, intent)
        with self.store.lock(project):
            if parent_revision:
                self._verified(project, parent_revision)
            revision, folder, _ = self._new(project)
            if folder.is_relative_to(source.parent):
                raise ValueError("Place the managed workspace outside the imported project directory")
            files = design_files(source.parent)
            if len(files) > 1000:
                raise ValueError("Project exceeds 1000 design files; import a focused project directory")
            for name, expected_hash in files.items():
                file = source.parent / name
                if file.stat().st_size > 50_000_000:
                    raise ValueError("Individual design file exceeds 50 MB")
                target = folder / "design" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, target)
                if digest(target) != expected_hash:
                    raise ValueError("Source changed during import; retry with a stable source")
            write_json(folder / "constraints.json", intent)
            compiler = compile_rules(folder / "design" / source.name, intent)
            data = self._seal(project, revision, folder, source.name, parent_revision, "import", {"compiler": compiler})
            return {"status": "imported", "revision": data, "summary": self._summary(board)}

    @staticmethod
    def _validate_references(board, intent):
        refs = {f["reference"] for f in board["footprints"]}
        nets = {n["name"] for n in board["nets"]}
        needed = set(intent["fixed_references"]) | set(intent.get("edge_overhang_references",[]))
        for p in intent["proximity"]:
            needed.update((p["reference"], p["target"]))
        for region in intent["regions"]:
            needed.update(region["references"])
        missing = needed - refs
        needed_nets = set(intent["critical_nets"])
        for rule in intent["net_rules"]:
            needed_nets.update(rule["nets"])
        if missing or needed_nets - nets:
            raise ValueError(f"Unknown constraint references/nets: {sorted(missing)} / {sorted(needed_nets - nets)}")

    @staticmethod
    def _summary(board):
        return {"footprints": len(board["footprints"]), "nets": len(board["nets"]),
                "tracks": board["tracks"], "vias": board["vias"],
                "outline": board["outline"], "unsupported": board["unsupported"]}

    def inspect_revision(self, project, revision):
        data, folder = self._verified(project, revision)
        return {"revision": data, "board": read_board(folder / "design" / data["board"]),
                "constraints": read_json(folder / "constraints.json")}

    def update_constraints(self, project, revision, constraints):
        intent = Constraints.model_validate(constraints).model_dump(mode="json")
        with self.store.lock(project):
            child, folder, old = self._new(project, revision)
            board = read_board(folder / "design" / old["board"])
            self._validate_references(board, intent)
            write_json(folder / "constraints.json", intent)
            compiler = compile_rules(folder / "design" / old["board"], intent)
            data = self._seal(project, child, folder, old["board"], revision, "constraints", {"compiler": compiler})
            return {"status": "created", "revision": data}

    def plan_layout(self, project, revision, count=3, *, completion_spread_mm=None):
        if not 1 <= count <= 6:
            raise ValueError("Candidate count must be 1-6")
        with self.store.lock(project):
            data, folder = self._verified(project, revision)
            result = plan_placements(read_board(folder / "design" / data["board"]), read_json(folder / "constraints.json"), count)
            if completion_spread_mm is not None:
                from .planning import completion_candidates
                result = completion_candidates(read_board(folder / "design" / data["board"]),
                    read_json(folder / "constraints.json"),result,count,completion_spread_mm)
            result.update({"revision_digest": data["digest"], "plan_id": "p-" + uuid.uuid4().hex[:12]})
            path = folder / "plans" / (result["plan_id"] + ".json")
            self._verified(project, revision)
            write_json(path, result)
            self.store.event(project, "layout_planned", {"revision": revision, "plan_id": result["plan_id"], "sha256": digest(path)})
            return result

    def apply_layout(self, project, revision, plan_id, candidate_id):
        self.store.identifier(plan_id)
        with self.store.lock(project):
            old, old_folder = self._verified(project, revision)
            plan_path = old_folder / "plans" / (plan_id + ".json")
            plan = read_json(plan_path)
            events = self.store.history(project)["events"]
            if not any(e["kind"] == "layout_planned" and e["payload"].get("sha256") == digest(plan_path)
                       and e["payload"].get("revision") == revision for e in events):
                raise ValueError("Plan is not a recorded, unmodified plan for this revision")
            if plan["revision_digest"] != old["digest"]:
                raise ValueError("Plan is stale")
            candidate = next((c for c in plan["candidates"] if c["id"] == candidate_id), None)
            if not candidate or not candidate["feasible"]:
                raise ValueError("Select an existing feasible candidate")
            child, folder, _ = self._new(project, revision)
            target = folder / "design" / old["board"]
            target.unlink()
            write_placements(old_folder / "design" / old["board"], target, candidate["placements"])
            result = audit_constraints(read_board(target), read_json(folder / "constraints.json"))
            if not _audit_passed(result):
                raise ValueError("Written candidate failed constraint re-evaluation")
            self._verified(project, revision)
            data = self._seal(project, child, folder, old["board"], revision, "placement", {"plan_id": plan_id, "candidate_id": candidate_id})
            return {"status": "created", "revision": data, "audit": result}

    def diagnose_repair(self, project, revision):
        from .repair import diagnose
        return diagnose(self, project, revision)

    def repair_revision(self, project, revision, nets, region, remove_ids=None, passes=3):
        from .repair import repair
        return repair(self, project, revision, nets, region, remove_ids, passes)

    def auto_repair_revision(self, project, revision, options=None, **callbacks):
        from .auto_repair import repair
        return repair(self, project, revision, options, **callbacks)

    def reference_repair_revision(self, project, revision, reference_project, reference_revision, **callbacks):
        from .reference_repair import repair
        return repair(self,project,revision,reference_project,reference_revision,**callbacks)

    def complete_revision(self, project, revision, options=None, **callbacks):
        from .completion import complete
        return complete(self, project, revision, options, **callbacks)

    def repair_clearance(self, project, revision, nets, region):
        from .clearance_repair import repair
        return repair(self, project, revision, nets, region)

    def route_revision(self, project, revision, passes=10, *, strict_widths=False, normalize_widths=False):
        if not 1 <= passes <= 100:
            raise ValueError("Routing passes must be 1-100")
        if not isinstance(strict_widths, bool):
            raise ValueError("strict_widths must be a boolean")
        if not isinstance(normalize_widths, bool):
            raise ValueError("normalize_widths must be a boolean")
        # Completion must not inherit a router's implicit pad-width neckdown.
        routing_tools = Toolchain({**self.toolchain.config, "controlled_neckdown": False}) if strict_widths else self.toolchain
        with self.store.lock(project):
            child, folder, old = self._new(project, revision)
            original = folder / "design" / old["board"]
            board = read_board(original)
            intent = read_json(folder / "constraints.json")
            if board["unsupported"] or not board["outline"]["supported"]:
                return self._blocked(project, revision, "Unsupported routing geometry", board["unsupported"])
            if intent["critical_nets"]:
                return self._blocked(project, revision, "Critical nets require reviewed routing; whole-board autorouting is disabled for this revision")
            if board["tracks"] or board["vias"]:
                return self._blocked(project, revision, "Existing routing is preserved. This adapter routes unrouted boards only")
            outputs = folder / "routing"
            outputs.mkdir()
            dsn, ses = outputs / "board.dsn", outputs / "board.ses"
            operations = []
            width_audit = None
            def import_routing():
                nonlocal width_audit
                effective_ses = ses
                if normalize_widths:
                    from .completion_cleanup import normalize_route_widths
                    effective_ses = outputs / "minimum-width.ses"
                    try:
                        width_audit = normalize_route_widths(original,ses,effective_ses)
                        write_json(outputs / "width-audit.json",width_audit)
                    except (ValueError,OSError,KeyError,TypeError) as error:
                        return {"status":"blocked","reason":"SES width normalization failed: "+str(error)}
                return routing_tools.import_ses(original,effective_ses,outputs / old["board"])
            for stage, operation in zip(("export_dsn", "autoroute", "import_ses"), (lambda: routing_tools.export_dsn(original, dsn),
                              lambda: routing_tools.route(dsn, ses, passes),
                              import_routing)):
                result = operation()
                operations.append(result)
                if result["status"] != "ok":
                    write_json(outputs / "operations.json", operations)
                    reason = f"Routing {stage} failed: " + str(result.get("reason") or result.get("validation_error") or result["status"])
                    if result.get("timed_out"):
                        budget = routing_tools.route_timeout if stage == "autoroute" else routing_tools.timeout
                        reason += f" (stage budget {budget:g}s; no candidate adopted)"
                    blocked = self._blocked(project, revision, reason, operations)
                    blocked.update(failed_stage=stage, timed_out=bool(result.get("timed_out")),
                                   evidence_path=str(outputs / "operations.json"))
                    return blocked
            routed = outputs / old["board"]
            routed_board = read_board(routed)
            if self._electrical_signature(board) != self._electrical_signature(routed_board):
                return self._blocked(project, revision, "Routing import changed components, positions, or net assignments", operations)
            shutil.copy2(routed, original)
            write_json(outputs / "operations.json", operations)
            self._verified(project, revision)
            data = self._seal(project, child, folder, old["board"], revision, "autoroute",
                              {"dsn_sha256": digest(dsn), "ses_sha256": digest(ses)})
            return {"status": "routed_unverified", "revision": data, "summary": self._summary(routed_board), "operations": operations,
                    **({"width_normalization":width_audit} if width_audit is not None else {})}

    @staticmethod
    def _electrical_signature(board):
        return sorted((f["reference"], f["value"], round(f["x"], 5), round(f["y"], 5), round(f["rotation"] % 360, 5),
                       f["layer"], tuple(sorted((p["number"], p["net"]) for p in f["pads"]))) for f in board["footprints"])

    def _blocked(self, project, revision, reason, details=None):
        result = {"status": "blocked", "revision": revision, "reason": reason, "details": details}
        self.store.event(project, "operation_blocked", result)
        return result

    def verify_revision(self, project, revision):
        with self.store.lock(project):
            return self._verify(project, revision)

    def _verify(self, project, revision):
        data, folder = self._verified(project, revision)
        run_id = "v-" + uuid.uuid4().hex[:12]
        evidence = folder / "verification" / run_id
        evidence.mkdir(parents=True)
        # Engines run against a disposable complete project snapshot, never a sealed revision.
        design = evidence / "design"
        shutil.copytree(folder / "design", design)
        board_path = design / data["board"]
        board = read_board(board_path)
        intent = read_json(folder / "constraints.json")
        audit = audit_constraints(board, intent)
        persisted_widths = _audit_persisted_track_minima(board, board_path)
        persisted_widths.update(revision=revision, revision_digest=data["digest"])
        write_json(evidence / "persisted-track-minima.json", persisted_widths)
        drc = self.toolchain.run_drc(board_path, evidence / "drc.json")
        schematic = board_path.with_suffix(".kicad_sch")
        erc = {"status": "blocked", "reason": "Matching root schematic is missing"}
        connectivity = {"status": "blocked", "reason": "Matching root schematic is missing"}
        if schematic.exists():
            erc = self.toolchain.run_erc(schematic, evidence / "erc.json")
            netlist_result = self.toolchain.export_netlist(schematic, evidence / "netlist.xml")
            if netlist_result["status"] == "ok":
                connectivity = inspect_netlist(evidence / "netlist.xml", board)
                connectivity["operation"] = netlist_result
            else:
                connectivity = netlist_result
        try:
            reasons = project_rule_issues(board_path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            reasons = ["Project rule evidence is invalid or unavailable: " + str(error)]
        reasons.extend(report_rule_issues(drc, "DRC"))
        if not _audit_passed(persisted_widths):
            reasons.append("Persisted project track minima are violated or unavailable")
        if drc["status"] != "ok" or drc.get("errors", 1) or drc.get("unconnected", 1):
            reasons.append("DRC unavailable, errors present, or connections incomplete")
        if not _audit_passed(audit) or not board["outline"]["supported"] or board["unsupported"]:
            reasons.append("Physical constraints or geometry coverage do not pass")
        if intent["critical_nets"]:
            reasons.append("Critical-net engineering review is not implemented in v0.1")
        if intent["release"]["require_erc"]:
            reasons.extend(report_rule_issues(erc, "ERC"))
            if erc["status"] != "ok" or erc.get("errors", 1):
                reasons.append("ERC unavailable or errors present")
        if (schematic.exists() or intent["release"]["require_erc"]) and connectivity["status"] != "passed":
            reasons.append("Schematic-to-board connectivity and component values/footprints are not verified")
        if design_files(design) != data["files"]:
            reasons.append("Engine changed the verification design snapshot")
        self._verified(project, revision)
        reports = {p.relative_to(evidence).as_posix(): digest(p) for p in evidence.iterdir() if p.is_file()}
        result = {"verification_id": run_id, "revision": revision, "revision_digest": data["digest"], "created": now(),
                  "status": "passed" if not reasons else "blocked", "reasons": reasons, "drc": drc, "erc": erc,
                  "constraints": audit, "persisted_track_minima": persisted_widths,
                  "connectivity": connectivity, "evidence_hashes": reports,
                  "dependency_scope": _dependency_scope(data["files"]),
                  "coverage": "ERC/DRC and declared geometric constraints; no SI/PI, thermal, EMC, certification or physical testing",
                  "package_scope": "schematic_and_board" if intent["release"]["require_erc"] else "board_only"}
        write_json(evidence / "result.json", result)
        self.store.event(project, "verification_completed", {"revision": revision, "verification_id": run_id, "status": result["status"], "sha256": digest(evidence / "result.json")})
        return result

    def compare_revisions(self, project, before, after):
        a, af = self._verified(project, before)
        b, bf = self._verified(project, after)
        result = compare_boards(af / "design" / a["board"], bf / "design" / b["board"])
        result.update({"before": before, "after": after, "constraints_changed": a["constraints_hash"] != b["constraints_hash"],
                       "requires_new_verification": a["digest"] != b["digest"],
                       "changed_design_files": sorted(n for n in set(a["files"]) | set(b["files"]) if a["files"].get(n) != b["files"].get(n))})
        result["impact"] = analyze_impact(
            read_board(af / "design" / a["board"]), read_board(bf / "design" / b["board"]),
            result, read_json(bf / "constraints.json"), result["changed_design_files"], result["constraints_changed"])
        return result

    def build_release(self, project, revision):
        with self.store.lock(project):
            # Fresh verification is deliberately mandatory; cached pass flags cannot release a board.
            verification = self._verify(project, revision)
            if verification["status"] != "passed":
                return {"status": "blocked", "verification": verification}
            data, folder = self._verified(project, revision)
            release_id = "release-" + uuid.uuid4().hex[:12]
            release = self.store.project_dir(project) / "releases" / release_id
            release.mkdir(parents=True)
            shutil.copytree(folder / "design", release / "design")
            fabrication = self.toolchain.export_manufacturing(release / "design" / data["board"], release / "manufacturing")
            write_json(release / "export-operations.json", fabrication)
            if fabrication["status"] != "ok":
                return self._blocked(project, revision, "Manufacturing export failed; no release archive produced", fabrication)
            self._verified(project, revision)
            if design_files(release / "design") != data["files"]:
                return self._blocked(project, revision, "Manufacturing engine modified its input design")
            components = verification["connectivity"].get("components", [])
            if components:
                populated = [c for c in components if not c["exclude_from_board"] and not c["dnp"]]
                positions = release / "manufacturing" / "positions.csv"
                if not positions.is_file():
                    return self._blocked(project, revision, "Position file is missing; assembly references cannot be checked")
                with positions.open(encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                refs = [row.get("Ref", "") for row in rows]
                expected_refs = {c["reference"] for c in populated}
                if not all(refs) or len(refs) != len(set(refs)) or set(refs) != expected_refs:
                    return self._blocked(project, revision, "DNP/populated references differ between schematic BOM and positions", {
                        "missing_positions": sorted(expected_refs - set(refs)),
                        "unexpected_positions": sorted(set(refs) - expected_refs),
                        "duplicate_or_invalid_references": not all(refs) or len(refs) != len(set(refs))})
                with (release / "manufacturing" / "bom-engineering.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["Reference", "Value", "Footprint", "MPN", "LCSC", "DNP"])
                    for c in populated:
                        writer.writerow([c["reference"], c["value"], c["footprint"], c["fields"].get("MPN", ""), c["fields"].get("LCSC", ""), c["dnp"]])
            shutil.copy2(folder / "constraints.json", release / "constraints.json")
            shutil.copytree(folder / "verification" / verification["verification_id"], release / "verification")
            write_json(release / "revision.json", data)
            files = {p.relative_to(release).as_posix(): digest(p) for p in release.rglob("*") if p.is_file()}
            manifest = {"schema_version": 1, "release_id": release_id, "project": project, "revision": revision,
                        "revision_digest": data["digest"], "created": now(), "files": files,
                        "package_scope": verification["package_scope"], "coverage": verification["coverage"],
                        "dependency_scope": verification["dependency_scope"],
                        "assembly_status": "DNP excluded; populated BOM/position references checked when schematic is present. "
                                           "MPN availability, manual-placement exceptions and pick-place rotations require assembly review; "
                                           "not approved for direct SMT production"}
            write_json(release / "manifest.json", manifest)
            archive = release.with_suffix(".zip")
            with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(release.rglob("*")):
                    if path.is_file():
                        bundle.write(path, path.relative_to(release))
            result = {"status": "released", "release_id": release_id, "archive": str(archive), "sha256": digest(archive), "manifest": manifest}
            self.store.event(project, "release_created", {"release_id": release_id, "revision": revision, "sha256": result["sha256"]})
            return result

    def verify_release(self, archive_path):
        archive = Path(archive_path).resolve(strict=True)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Duplicate archive entries")
            if sum(i.file_size for i in bundle.infolist()) > 500_000_000:
                raise ValueError("Release archive exceeds verification size limit")
            import json
            manifest = json.loads(bundle.read("manifest.json"))
            expected = manifest["files"]
            differences = sorted(set(names) ^ (set(expected) | {"manifest.json"}))
            for name, checksum in expected.items():
                if name in names and hashlib.sha256(bundle.read(name)).hexdigest() != checksum:
                    differences.append(name)
        return {"status": "verified" if not differences else "failed", "differences": differences, "sha256": digest(archive),
                "scope": "Content integrity, not cryptographic publisher authentication"}
