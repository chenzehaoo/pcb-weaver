"""Standalone helper for KiCad's own Python runtime (including Python 3.10).

Only stdlib is imported until an operation actually needs pcbnew. Run this file
by its absolute path; the KiCad runtime does not need pcb-weaver installed.
"""

import argparse
from collections import Counter
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import zipfile


# Official release asset 206469028 has an unspecified manifest version. Its
# exact digest was verified against the release download during integration.
PINNED_RELEASES = {
    "251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9": "2.4.1",
    "d7fd0f63f52e6d74b0fad6715f87ca9f0ffd7109d66b2a584638000270592ecf": "2.0.1",
    "9084a4888937a7f31f857ecc12aa7a37407f51160e4d2892dff9c9bb47ae3102": "1.9.0",
}

NETCLASS_GETTERS = {"track_width": "GetTrackWidth", "clearance": "GetClearance",
                    "via_diameter": "GetViaDiameter", "via_drill": "GetViaDrill",
                    "microvia_diameter": "GetuViaDiameter", "microvia_drill": "GetuViaDrill",
                    "diff_pair_width": "GetDiffPairWidth", "diff_pair_gap": "GetDiffPairGap",
                    "diff_pair_via_gap": "GetDiffPairViaGap"}


def _layer_stack(board):
    layers = [board.GetLayerName(layer) for layer in board.GetEnabledLayers().CuStack()]
    if len(layers) not in (2, 4, 6, 8) or len(layers) != board.GetCopperLayerCount():
        raise ValueError("Only native 2/4/6/8-copper-layer stacks are supported")
    return layers


def _net_values(pcbnew, board):
    result = {}
    board.SynchronizeNetsAndNetClasses(False)
    names = {str(name) for name in board.GetAllNetClasses()}
    for name, net in board.GetNetsByName().items():
        name = str(name)
        if not name:
            continue
        netclass = net.GetNetClassSlow()
        class_name = net.GetNetClassName()
        members = [class_name] if class_name in names else [part.strip() for part in class_name.split(",")]
        verified_members = not any("," in member for member in names) and all(member in names for member in members)
        result[name] = {"class_name": class_name, "class_names": members,
                       "class_membership_verified": verified_members, **{
            key: pcbnew.ToMM(getattr(netclass, getter)()) for key, getter in NETCLASS_GETTERS.items()}}
    return result


def _copper_snapshot(pcbnew, board):
    """Exact geometry multiset; no tolerance hides a moved or missing old track."""
    rows = []
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            position = item.GetPosition()
            row = ("via", item.GetNetname(), position.x, position.y, item.GetWidth(),
                   item.GetDrillValue(), board.GetLayerName(item.TopLayer()),
                   board.GetLayerName(item.BottomLayer()), int(item.GetViaType()), item.IsLocked())
        else:
            if isinstance(item, pcbnew.PCB_ARC):
                raise ValueError("Existing copper arcs have not been verified for lossless DSN/SES routing")
            ends = sorted(((item.GetStart().x, item.GetStart().y), (item.GetEnd().x, item.GetEnd().y)))
            row = ("segment", item.GetNetname(), board.GetLayerName(item.GetLayer()),
                   tuple(ends[0]), tuple(ends[1]), item.GetWidth(), item.IsLocked())
        rows.append(row)
    return Counter(rows)


def _polygon_points(poly):
    def chain_points(chain):
        if chain.ArcCount():
            raise ValueError("Curved zone contours have not been verified for lossless routing")
        return [(chain.CPoint(i).x, chain.CPoint(i).y) for i in range(chain.PointCount())]
    return [(chain_points(poly.COutline(i)), [chain_points(poly.CHole(i, j))
             for j in range(poly.HoleCount(i))]) for i in range(poly.OutlineCount())]


def _verify_track_minima(pcbnew, board, source):
    """Nominal netclass widths are not hard minima; explicit rules still are."""
    path = source.with_suffix(".kicad_pro")
    if not path.is_file():
        return {"verified": False, "reason": "No project track minimum", "requires_drc": True}
    project = json.loads(path.read_text(encoding="utf-8"))
    floor = project.get("board", {}).get("design_settings", {}).get("rules", {}).get("min_track_width")
    if floor is None:
        return {"verified": False, "reason": "No project track minimum", "requires_drc": True}
    def minimum(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid declared track minimum")
        return pcbnew.FromMM(value)
    global_min = minimum(floor)
    per_net = {}
    for net, entry in project.get("pcb_weaver_net_rules", {}).items():
        # Older compiler ledgers conflate nominal and minimum; retain their
        # stronger bound until the compiler records an explicit declaration.
        value = entry.get("declared_min_width_mm", entry.get("minimums", {}).get("track_width"))
        per_net[net] = max(global_min, minimum(value))
    observed, count = {}, 0
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            continue
        net, width = str(item.GetNetname()), item.GetWidth()
        required = per_net.get(net, global_min)
        if width < required:
            raise ValueError(f"Routed track below declared minimum on {net}: {pcbnew.ToMM(width):g} < {pcbnew.ToMM(required):g} mm")
        observed[net] = min(observed.get(net, width), width)
        count += 1
    return {"verified": True, "segments_checked": count, "global_minimum_mm": floor,
            "per_net_minimum_mm": {net: pcbnew.ToMM(value) for net, value in per_net.items()},
            "observed_minimum_mm": {net: pcbnew.ToMM(value) for net, value in observed.items()},
            "requires_drc": True, "custom_rule_evaluation": "Requires native DRC"}


def _zone_snapshot(board):
    zones = {}
    for zone in board.Zones():
        layers = list(zone.GetLayerSet().CuStack())
        zones[zone.m_Uuid.AsString()] = {"net": zone.GetNetname(),
            "layers": [board.GetLayerName(layer) for layer in layers],
            "outline": _polygon_points(zone.Outline()),
            "filled": {board.GetLayerName(layer): _polygon_points(zone.GetFilledPolysList(layer))
                       for layer in layers if zone.HasFilledPolysForLayer(layer)}}
    return zones


def _inspect(pcbnew, board, source):
    project = source.with_suffix(".kicad_pro")
    return {"board_sha256": _hash(source), "project_sha256": _hash(project) if project.is_file() else None,
            "copper_layers": _layer_stack(board), "nets": _net_values(pcbnew, board),
            "standard_copper_layers": [board.GetStandardLayerName(i) for i in board.GetEnabledLayers().CuStack()],
            "class_priorities": {str(name): value.GetPriority() for name, value in board.GetAllNetClasses().items()
                                 if hasattr(value, "GetPriority")},
            "existing_copper_items": len(board.GetTracks()), "zones": len(board.Zones()),
            "high_speed_supported": False}


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _destination(sources, destination):
    for source in sources:
        if source.resolve() == destination.resolve() or (
            source.exists() and destination.exists() and source.samefile(destination)
        ):
            raise ValueError("Source and destination must differ")
        if not source.is_file() or not source.stat().st_size:
            raise ValueError("Missing or empty input: " + str(source))
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists: " + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)


def _copy_sidecars(source, destination):
    """Keep project and custom rules with the new board's basename."""
    copies = []
    for extension in (".kicad_pro", ".kicad_dru"):
        original = source.with_suffix(extension)
        target = destination.with_suffix(extension)
        if not original.is_file() or original.resolve() == target.resolve():
            continue
        if target.exists() or target.is_symlink():
            if not target.is_file() or _hash(original) != _hash(target):
                raise ValueError("Conflicting destination sidecar: " + str(target))
        else:
            copies.append((original, target))
    for original, target in copies:
        shutil.copy2(original, target)


def _verify_project_netclasses(pcbnew, board, source):
    """Check loaded native values, not merely the existence of a sidecar."""
    path = source.with_suffix(".kicad_pro")
    if not path.is_file():
        return {"project_loaded": False, "netclasses_verified": False, "classes": {}}
    project = json.loads(path.read_text(encoding="utf-8"))
    settings = project.get("net_settings", {})
    declared = settings.get("classes", [])
    if not isinstance(declared, list):
        raise ValueError("Project net_settings.classes must be an array")
    verified = {}
    if declared:
        board.SynchronizeNetsAndNetClasses(False)
        loaded = {str(name): value for name, value in board.GetAllNetClasses().items()}
        for entry in declared:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ValueError("Malformed declared netclass")
            name = entry["name"]
            if name not in loaded or name in verified:
                raise ValueError("Native netclass missing or duplicated: " + name)
            values = {}
            for field, getter in NETCLASS_GETTERS.items():
                value = entry.get(field)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("Invalid netclass value: " + name + "." + field)
                actual = getattr(loaded[name], getter)()
                expected = pcbnew.FromMM(value)
                if actual != expected:
                    raise ValueError("Project netclass was not applied: {}.{} declared={}mm native={}mm; "
                                     "check net_settings.meta schema in the source project".format(
                                         name, field, value, pcbnew.ToMM(actual)))
                values[field] = pcbnew.ToMM(actual)
            verified[name] = values
    effective = _net_values(pcbnew, board) if project.get("pcb_weaver_net_rules") else {}
    for net, expected in project.get("pcb_weaver_net_rules", {}).items():
        if net not in effective:
            raise ValueError("Compiled net not present in native board: " + net)
        for field, minimum in expected["minimums"].items():
            if pcbnew.FromMM(effective[net][field]) < pcbnew.FromMM(minimum):
                raise ValueError("Compiled per-net rule was not applied: " + net + "." + field)
        if not set(expected.get("required_memberships", [])).issubset(effective[net]["class_names"]):
            raise ValueError("Compiled net lost an original netclass membership: " + net)
    return {"project_loaded": True, "netclasses_verified": bool(declared), "classes": verified,
            "effective_nets": effective,
            "net_settings_meta": settings.get("meta")}


def _session_placement_resolution_mm(path):
    """Read the placement resolution with a quoted-token-aware S-expression scan."""
    stack, resolutions = [], []
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|[()]|[^\s()"]+', path.read_text(encoding="utf-8")):
        token = match.group()
        if token == "(":
            stack.append([])
        elif token == ")":
            if not stack:
                raise ValueError("Unbalanced SES expression")
            form = stack.pop()
            if form[:1] == ["resolution"] and stack and stack[-1][:1] == ["placement"]:
                if len(form) != 3:
                    raise ValueError("Malformed SES placement resolution")
                units = {"um": 0.001, "mm": 1.0, "cm": 10.0, "mil": 0.0254, "inch": 25.4}
                divisor = float(form[2])
                if form[1] not in units or not math.isfinite(divisor) or divisor <= 0:
                    raise ValueError("Unsupported SES placement resolution")
                resolutions.append(units[form[1]] / divisor)
        elif stack and len(stack[-1]) < 4:
            stack[-1].append(token)
    if stack or len(resolutions) != 1:
        raise ValueError("SES must declare exactly one placement resolution")
    if not 0 < resolutions[0] <= 0.0001:
        raise ValueError("SES placement resolution exceeds the 0.0001mm safety ceiling")
    return resolutions[0]


def _placement_snapshot(board):
    snapshots = {}
    for footprint in board.GetFootprints():
        uid = footprint.m_Uuid.AsString()
        if uid in snapshots:
            raise ValueError("Duplicate footprint UUID")
        position = footprint.GetPosition()
        pads = sorted((pad.m_Uuid.AsString(), pad.GetNumber(), pad.GetNetname(),
                       pad.GetPosition().x, pad.GetPosition().y) for pad in footprint.Pads())
        snapshots[uid] = {"reference": footprint.GetReference(), "x": position.x, "y": position.y,
                          "angle": footprint.GetOrientationDegrees(), "layer": footprint.GetLayer(), "pads": pads}
    return snapshots


def _restore_session_placements(pcbnew, board, original, tolerance_mm):
    changed = _placement_snapshot(board)
    if original.keys() != changed.keys():
        raise ValueError("SES changed the footprint UUID set")
    tolerance = pcbnew.FromMM(tolerance_mm)
    maximum = 0
    for uid, before in original.items():
        after = changed[uid]
        angle_delta = (after["angle"] - before["angle"] + 180) % 360 - 180
        if before["reference"] != after["reference"] or before["layer"] != after["layer"] or abs(angle_delta) > 1e-9:
            raise ValueError("SES changed footprint identity, angle or layer: " + before["reference"])
        if [p[:3] for p in before["pads"]] != [p[:3] for p in after["pads"]]:
            raise ValueError("SES changed pad identities or networks: " + before["reference"])
        drift = max(abs(after["x"] - before["x"]), abs(after["y"] - before["y"]))
        if drift > tolerance:
            raise ValueError("SES moved footprint beyond its declared resolution: " + before["reference"])
        maximum = max(maximum, drift)
    for footprint in board.GetFootprints():
        before = original[footprint.m_Uuid.AsString()]
        footprint.SetPosition(pcbnew.VECTOR2I(before["x"], before["y"]))
        footprint.SetOrientationDegrees(before["angle"])
    if _placement_snapshot(board) != original:
        raise ValueError("SES placement/pad geometry could not be restored exactly")
    board.BuildConnectivity()
    return {"restored_exactly": True, "resolution_mm": tolerance_mm,
            "max_axis_drift_mm": pcbnew.ToMM(maximum), "footprints": len(original), "requires_drc": True}


def _sample(pcbnew, layers=2, platform=False, partial=False):
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(layers)
    points = [(20, 20), (60, 20), (60, 45), (20, 45)]
    for start, end in zip(points, points[1:] + points[:1]):
        edge = pcbnew.PCB_SHAPE()
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]), pcbnew.FromMM(start[1])))
        edge.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]), pcbnew.FromMM(end[1])))
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetWidth(pcbnew.FromMM(0.05))
        board.Add(edge)
    net = pcbnew.NETINFO_ITEM(board, "LINK")
    board.Add(net)
    points = [("J1", 28.123457, 32.234567, net), ("J2", 52.765433, 32.234567, net)]
    if platform:
        power = pcbnew.NETINFO_ITEM(board, "POWER")
        board.Add(power)
        points.extend([("P1", 28.0, 25.0, power), ("P2", 52.0, 25.0, power)])
    for reference, x, y, assigned in points:
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetFPID(pcbnew.LIB_ID("PCBWeaver", "TestPoint"))
        footprint.SetReference(reference)
        footprint.SetValue("TEST_POINT")
        pad = pcbnew.PAD(footprint)
        pad.SetNumber("1")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(2), pcbnew.FromMM(2)))
        pad.SetDrillSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
        pad.SetNet(assigned)
        footprint.Add(pad)
        board.Add(footprint)
        footprint.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    if partial:
        copper_layers = list(board.GetEnabledLayers().CuStack())
        for start, end, layer in (((28, 25), (34, 25), copper_layers[0]), ((34, 25), (38, 25), copper_layers[1])):
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]), pcbnew.FromMM(start[1])))
            track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]), pcbnew.FromMM(end[1])))
            track.SetWidth(pcbnew.FromMM(0.6))
            track.SetLayer(layer)
            track.SetNet(power)
            track.SetLocked(True)
            board.Add(track)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(34), pcbnew.FromMM(25)))
        via.SetWidth(pcbnew.FromMM(0.8))
        via.SetDrill(pcbnew.FromMM(0.4))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(copper_layers[0], copper_layers[-1])
        via.SetNet(power)
        via.SetLocked(True)
        board.Add(via)
        zone = pcbnew.ZONE(board)
        zone.SetLayer(copper_layers[-1])
        zone.SetNet(power)
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in ((26, 23), (31, 23), (31, 27), (26, 27)):
            outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        board.Add(zone)
    return board


def perform(operation, paths):
    version = None
    try:
        partial = operation.startswith("sample-partial-")
        fanout_sample = operation == "sample-fanout-4"
        platform_layers = int(operation.rsplit("-", 1)[-1]) if operation.startswith("sample-platform-") or partial or fanout_sample else None
        if platform_layers is not None:
            operation = "sample-board"
        paths = [Path(p).resolve() for p in paths]
        if operation == "jar-info":
            with zipfile.ZipFile(paths[0]) as jar:
                bad_entry = jar.testzip()
                if bad_entry is not None:
                    raise ValueError("Corrupt JAR entry: " + bad_entry)
                text = jar.read("META-INF/MANIFEST.MF").decode("utf-8")
                text = text.replace("\r\n ", "").replace("\n ", "")
                attributes = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)
                main_class = attributes.get("Main-Class")
                if main_class not in ("app.freerouting.Freerouting", "app.freerouting.gui.MainApplication"):
                    raise ValueError("Unknown executable Freerouting entry point")
                class_name = main_class.replace(".", "/") + ".class"
                header = jar.read(class_name)[:8]
                if len(header) != 8 or header[:4] != b"\xca\xfe\xba\xbe":
                    raise ValueError("Invalid Freerouting main class bytecode")
            text = text.replace("\r\n ", "").replace("\n ", "")
            attributes = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)
            version = attributes.get("Implementation-Version") or attributes.get("Specification-Version")
            manifest_version = version
            digest = _hash(paths[0])
            version_source = "manifest"
            if not version or version == "unspecified":
                version = PINNED_RELEASES.get(digest)
                version_source = "pinned_release_sha256" if version else "unknown"
            if not version or (main_class == "app.freerouting.gui.MainApplication" and version != "1.9.0"):
                return {"status": "blocked", "reason": "Expected an executable Freerouting JAR with version metadata", "version": version}
            major = int.from_bytes(header[6:8], "big") if header[:4] == b"\xca\xfe\xba\xbe" else None
            return {"status": "ok", "version": version, "sha256": digest,
                    "main_class": main_class,
                    "manifest_version": manifest_version, "version_source": version_source,
                    "build_revision": attributes.get("BuiltRevision") or attributes.get("Build-Revision"),
                    "minimum_java": major - 44 if major is not None else None}
        try:
            pcbnew = importlib.import_module("pcbnew")
        except (ImportError, OSError) as exc:
            return {"status": "blocked", "reason": "KiCad pcbnew is unavailable in this Python runtime: " + str(exc), "version": None}
        version = pcbnew.GetBuildVersion()
        match = re.search(r"\b(\d+)\.", version)
        if not match or int(match[1]) not in (8, 9, 10):
            return {"status": "blocked", "reason": "Native bridge supports KiCad 8-10 SWIG bindings", "version": version}
        requirements = {
            "inspect": ("LoadBoard",),
            "export-dsn": ("LoadBoard", "ExportSpecctraDSN"),
            "import-ses": ("LoadBoard", "ImportSpecctraSES", "SaveBoard"),
            "fill-zones": ("LoadBoard", "ZONE_FILLER", "SaveBoard"),
            "sample-board": ("BOARD", "SaveBoard", "FOOTPRINT", "PAD"),
        }
        capabilities = {name: all(callable(getattr(pcbnew, item, None)) for item in items)
                        for name, items in requirements.items()}
        if operation == "doctor":
            return {"status": "ok" if all(capabilities.values()) else "blocked", "version": version,
                    "capabilities": capabilities, "reason": None if all(capabilities.values()) else "Required pcbnew helpers are missing"}
        if operation == "inspect":
            if len(paths) != 1 or not paths[0].is_file():
                raise ValueError("inspect requires one board")
            board = pcbnew.LoadBoard(str(paths[0]))
            return {"status": "ok", "version": version, "project_rules": _verify_project_netclasses(pcbnew, board, paths[0]),
                    **_inspect(pcbnew, board, paths[0])}
        if not capabilities.get(operation):
            return {"status": "blocked", "reason": "Native operation unavailable: " + operation, "version": version}
        expected = 3 if operation == "import-ses" else 1 if operation == "sample-board" else 2
        if len(paths) != expected:
            raise ValueError("Incorrect number of paths for " + operation)
        sources, output = paths[:-1], paths[-1]
        _destination(sources, output)
        protected = list(sources)
        if sources:
            protected.extend(path for path in (sources[0].with_suffix(".kicad_pro"), sources[0].with_suffix(".kicad_dru")) if path.is_file())
        hashes = {str(path): _hash(path) for path in protected}
        if sources and operation in ("import-ses", "fill-zones"):
            _copy_sidecars(sources[0], output)
        board = _sample(pcbnew, platform_layers or 2, platform_layers is not None, partial) if operation == "sample-board" else pcbnew.LoadBoard(str(sources[0]))
        if fanout_sample:
            for footprint in board.GetFootprints():
                for pad in footprint.Pads():
                    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                    pad.SetDrillSize(pcbnew.VECTOR2I(0, 0))
                    pad_layers = pcbnew.LSET()
                    for layer in (pcbnew.F_Cu, pcbnew.F_Mask, pcbnew.F_Paste):
                        pad_layers.AddLayer(layer)
                    pad.SetLayerSet(pad_layers)
                if footprint.GetReference().endswith("2"):
                    footprint.Flip(footprint.GetPosition(), False)
        if board is None:
            raise RuntimeError("KiCad could not load the board")
        rules = _verify_project_netclasses(pcbnew, board, sources[0]) if sources else {}
        placement_audit = None
        layers = _layer_stack(board)
        copper_audit = None
        width_audit = None
        if operation == "export-dsn":
            existing = _copper_snapshot(pcbnew, board)
            if not pcbnew.ExportSpecctraDSN(board, str(output)):
                raise RuntimeError("KiCad DSN export returned false")
            copper_audit = {"input_items": sum(existing.values()), "requires_import_verification": True}
        else:
            if operation == "import-ses":
                resolution = _session_placement_resolution_mm(sources[1])
                original = _placement_snapshot(board)
                old_copper, old_zones = _copper_snapshot(pcbnew, board), _zone_snapshot(board)
                if not pcbnew.ImportSpecctraSES(board, str(sources[1])):
                    raise RuntimeError("KiCad SES import returned false")
                placement_audit = _restore_session_placements(pcbnew, board, original, resolution)
                missing = old_copper - _copper_snapshot(pcbnew, board)
                if missing or old_zones != _zone_snapshot(board) or layers != _layer_stack(board):
                    raise ValueError("SES did not preserve existing copper, zones or layer stack; result rejected")
                width_audit = _verify_track_minima(pcbnew, board, sources[0])
                copper_audit = {"input_items": sum(old_copper.values()), "preserved_exactly": True,
                                "zones_preserved": len(old_zones), "requires_drc": True}
            if operation == "fill-zones":
                board.BuildConnectivity()
                if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
                    raise RuntimeError("KiCad zone fill returned false")
            # Skip project settings writes: the caller owns revision sidecars.
            if not pcbnew.SaveBoard(str(output), board, True):
                raise RuntimeError("KiCad board save returned false")
        if not output.is_file() or not output.stat().st_size:
            raise RuntimeError("Native operation produced no nonempty output")
        if any(_hash(path) != hashes[str(path)] for path in protected):
            raise RuntimeError("Native operation modified an input")
        return {"status": "ok", "version": version, "output_path": str(output), "sha256": _hash(output),
                "input_hashes": hashes, "project_loaded": rules.get("project_loaded", False),
                "project_rules": rules, "placement_preservation": placement_audit,
                "track_width_audit": width_audit,
                "copper_layers": layers, "copper_preservation": copper_audit, "high_speed_supported": False}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        return {"status": "blocked", "reason": str(exc), "version": version}
    except Exception as exc:
        return {"status": "failed", "reason": type(exc).__name__ + ": " + str(exc), "version": version}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("doctor", "inspect", "export-dsn", "import-ses", "fill-zones", "sample-board", "jar-info",
                                              "sample-platform-2", "sample-platform-4", "sample-platform-6", "sample-platform-8",
                                              "sample-fanout-4",
                                              "sample-partial-2", "sample-partial-4", "sample-partial-6", "sample-partial-8"))
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    result = perform(args.operation, args.paths)
    print("PCB_WEAVER_RESULT=" + json.dumps(result), flush=True)
    return {"ok": 0, "failed": 1, "blocked": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
