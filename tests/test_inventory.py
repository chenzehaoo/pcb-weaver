"""Inventory tests are read-model checks, not native electrical acceptance."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier, Event

import pytest
import sexpdata

from pcb_weaver import board, inventory
from pcb_weaver.inventory import build_inventory
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, write_json


ROOT = Path(__file__).resolve().parents[1]
BASE = '''(kicad_pcb (version 20240108)
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
 (gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts"))
 (net 0 "") (net 1 "N1")
 (footprint "Test:R_0603" (layer "F.Cu") (at 10 10 90)
  (property "Reference" "R1") (property "Value" "Vendor-looking-123")
  (fp_rect (start -1 -1) (end 1 1) (layer "F.CrtYd"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "N1"))
  (pad "1" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 1 "N1"))))'''


def make_engine(tmp_path, text=BASE, constraints=None):
    engine = EngineeringService(tmp_path / "managed")
    revision, folder, _ = engine._new("demo")
    (folder / "design" / "board.kicad_pcb").write_text(text, encoding="utf-8")
    write_json(folder / "constraints.json", constraints or {})
    engine._seal("demo", revision, folder, "board.kicad_pcb", None, "test_fixture")
    return engine, revision, folder


def append(text, *nodes):
    ast = sexpdata.loads(text)
    ast.extend(sexpdata.loads(n) for n in nodes)
    return sexpdata.dumps(ast)


def add_properties(text, pairs):
    ast = sexpdata.loads(text)
    fp = board._children(ast, "footprint")[0]
    fp.extend([sexpdata.Symbol("property"), k, v] for k, v in pairs)
    return sexpdata.dumps(ast)


def record_metadata(engine, revision, folder, fields, **changes):
    data, _ = engine._verified("demo", revision)
    directory = folder / "verification" / "v-metadata"
    write_json(directory / "netlist.json", {"fixture": "metadata, not EDA evidence"})
    result = {"verification_id": "v-metadata", "revision": revision, "revision_digest": data["digest"],
              "status": "blocked", "created": "2026-09-07T00:00:00Z",
              "evidence_hashes": {"netlist.json": digest(directory / "netlist.json")},
              "connectivity": {"status": "failed", "components": [
                  {"reference": "R1", "fields": fields, **changes}]}}
    write_json(directory / "result.json", result)
    engine.store.event("demo", "verification_completed", {
        "verification_id": "v-metadata", "revision": revision, "status": result["status"],
        "sha256": digest(directory / "result.json")})
    return directory


def test_unknown_metadata_repeated_pads_and_determinism(tmp_path):
    engine, revision, folder = make_engine(tmp_path)
    result = build_inventory(engine, "demo", revision)
    assert result == build_inventory(engine, "demo", revision)
    assert result["schema_version"] == "1.0" and result["units"] == "mm"
    assert result["summary"] == dict(component_count=1, pad_count=2, net_count=1,
                                     track_count=0, via_count=0, layer_count=2, package_count=1)
    component = result["components"][0]
    assert component["category"] == "resistor"
    assert component["category_basis"]["verified"] is False
    assert all(component[key] is None for key in ("manufacturer", "mpn", "datasheet"))
    assert result["coverage"]["metadata_missing"] == dict(manufacturer=1, mpn=1, datasheet=1)
    pads = result["nets"][0]["pads"]
    assert len(pads) == 2 and pads[0]["number"] == pads[1]["number"] == "1"
    assert pads[0]["id"] != pads[1]["id"]
    assert (pads[1]["x"], pads[1]["y"]) == (10, 8)
    assert result["nets"][0]["connectivity_status"] == "not_evaluated"
    assert result["sources"]["board"]["sha256"] == digest(folder / "design" / "board.kicad_pcb")


def test_board_metadata_is_additive_without_geometry_changes(tmp_path):
    ast = sexpdata.loads(BASE)
    fp = board._children(ast, "footprint")[0]
    fp.extend([sexpdata.loads(n) for n in ['(uuid "component-uuid")', '(attr smd dnp)']])
    pad = board._children(fp, "pad")[0]
    pad.extend([sexpdata.loads(n) for n in ['(uuid "pad-uuid")', '(pinfunction "VCC")', '(pintype "power_in")']])
    engine, revision, _ = make_engine(tmp_path, sexpdata.dumps(ast))
    component = build_inventory(engine, "demo", revision)["components"][0]
    assert component["uuid"] == "component-uuid" and component["dnp"] is True
    assert component["pads"][0]["uuid"] == "pad-uuid"
    assert component["pads"][0]["pin_function"] == "VCC"
    assert component["pads"][0]["pin_type"] == "power_in"
    old = board._inspect(sexpdata.loads(BASE))["footprints"][0]
    for key in ("bounds", "x", "y", "rotation", "layer", "locked", "through_hole_bounds", "courtyard"):
        assert component[key] == old[key]


def test_routed_only_declared_only_and_unassigned_objects(tmp_path):
    text = append(BASE, '(net 2 "ROUTE")', '(net 3 "DECLARED")',
                  '(segment (start 1 1) (end 4 5) (width 0.25) (layer "B.Cu") (net 2))',
                  '(segment (start 1 1) (end 1 3) (width 0.5) (layer "F.Cu") (net 2))',
                  '(via (at 4 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))',
                  '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 0))')
    constraints = {"critical_nets": ["ROUTE"], "net_rules": [{"nets": ["ROUTE"], "min_width_mm": 0.2}],
                   "uninterpreted_declaration": "retained"}
    engine, revision, _ = make_engine(tmp_path, text, constraints)
    result = build_inventory(engine, "demo", revision)
    nets = {n["name"]: n for n in result["nets"]}
    assert set(nets) == {"N1", "ROUTE", "DECLARED"}
    net = nets["ROUTE"]
    assert (net["pad_count"], net["track_count"], net["via_count"], net["length_mm"]) == (0, 2, 1, 7)
    assert (net["min_width_mm"], net["max_width_mm"]) == (0.25, 0.5)
    assert net["layers"] == ["B.Cu", "F.Cu"]
    assert net["constraints"]["critical"] and result["constraints"] == constraints
    assert net["connectivity_status"] == "not_evaluated"
    assert result["coverage"]["unassigned_track_count"] == 1
    assert result["tracks"][0]["id"] == "track:0" and result["vias"][0]["id"] == "via:0"
    assert sum(l["track_count"] for l in result["layers"]) == 3
    assert nets["DECLARED"]["min_width_mm"] is None


def test_arcs_are_counted_and_length_is_explicitly_partial(tmp_path):
    text = append(BASE, '(arc (start 1 1) (mid 2 2) (end 3 1) (width 0.2) (layer "F.Cu") (net "ARC"))')
    engine, revision, _ = make_engine(tmp_path, text)
    result = build_inventory(engine, "demo", revision)
    assert result["summary"]["track_count"] == len(result["tracks"]) == 1
    arc = result["tracks"][0]
    assert arc["kind"] == "arc" and arc["length_mm"] is None and not arc["geometry_supported"]
    assert result["coverage"]["unknown_length_track_count"] == 1
    assert result["coverage"]["length_status"] == "partial"
    assert all(n["length_status"] == "partial" for n in result["nets"])
    assert result["coverage"]["unsupported_geometry"]
    assert any(n["name"] == "ARC" and n["track_count"] == 1 for n in result["nets"])


def test_empty_board_has_no_synthetic_nets(tmp_path):
    engine, revision, _ = make_engine(tmp_path, '(kicad_pcb (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))')
    result = build_inventory(engine, "demo", revision)
    assert result["nets"] == result["components"] == result["tracks"] == result["vias"] == []
    assert result["summary"]["net_count"] == 0


def test_via_only_net_spans_four_layers_and_zone_only_is_partial(tmp_path):
    text = BASE.replace('(31 "B.Cu" signal)', '(31 "B.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)')
    text = append(text,
                  '(via (at 4 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VIA_ONLY"))',
                  '(zone (net "ZONE_ONLY") (layer "F.Cu"))')
    engine, revision, _ = make_engine(tmp_path, text)
    result = build_inventory(engine, "demo", revision)
    nets = {n["name"]: n for n in result["nets"]}
    assert nets["VIA_ONLY"]["layers"] == ["B.Cu", "F.Cu", "In1.Cu", "In2.Cu"]
    assert nets["VIA_ONLY"]["via_count"] == 1 and nets["VIA_ONLY"]["track_count"] == 0
    assert nets["ZONE_ONLY"]["length_status"] == "partial"
    assert result["coverage"]["zone_count"] == 1


def test_no_implicit_limit_and_no_revision_or_ledger_mutation(tmp_path):
    nodes = [f'(segment (start {i} 1) (end {i} 2) (width 0.2) (layer "F.Cu") (net 1))' for i in range(1501)]
    engine, revision, folder = make_engine(tmp_path, append(BASE, *nodes))
    history = engine.store.history("demo")
    hashes = {p: digest(p) for p in folder.rglob("*") if p.is_file()}
    result = build_inventory(engine, "demo", revision)
    assert result["summary"]["track_count"] == len(result["tracks"]) == 1501
    assert result["tracks"][-1]["id"] == "track:1500"
    assert result["nets"][0]["length_mm"] == 1501
    assert engine.store.history("demo") == history
    assert {p: digest(p) for p in folder.rglob("*") if p.is_file()} == hashes


@pytest.mark.parametrize("payload", ['<script>alert(1)</script>', '=HYPERLINK("evil")',
                                       'https://example.invalid/secret', r'C:\private\secret.txt',
                                       '../../secret', '$(curl evil)', '\x00bad\ntext'])
def test_strings_are_preserved_as_data_not_opened(tmp_path, monkeypatch, payload):
    text = add_properties(BASE, [("MPN", payload), ("Datasheet", payload), ("Manufacturer", payload)])
    engine, revision, _ = make_engine(tmp_path, text)
    def forbidden(*args, **kwargs):
        raise AssertionError("Inventory must not execute EDA or access network")
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    component = build_inventory(engine, "demo", revision)["components"][0]
    assert component["mpn"] == component["datasheet"] == component["manufacturer"] == payload
    assert json.loads(json.dumps(component))["properties"]["MPN"] == payload


def test_verified_fields_have_sources_and_conflicts_never_guess(tmp_path):
    text = add_properties(BASE, [("MPN", "BOARD-1"), ("MPN", "BOARD-2")])
    engine, revision, folder = make_engine(tmp_path, text)
    record_metadata(engine, revision, folder, {"Manufacturer": "Verified Vendor", "MPN": "SCHEMATIC-3"}, value="other")
    result = build_inventory(engine, "demo", revision)
    component = result["components"][0]
    assert component["manufacturer"] == "Verified Vendor" and component["mpn"] is None
    assert len(component["metadata_sources"]["mpn"]) == 3
    assert {c["field"] for c in component["metadata_conflicts"]} == {"value", "mpn"}
    assert component["metadata_sources"]["manufacturer"][0]["verification_id"] == "v-metadata"
    assert result["sources"]["verification"]["connectivity_status"] == "failed"


@pytest.mark.parametrize("target", ["design/board.kicad_pcb", "constraints.json"])
def test_tampered_revision_rejected(tmp_path, target):
    engine, revision, folder = make_engine(tmp_path)
    with (folder / target).open("a", encoding="utf-8") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="changed outside"):
        build_inventory(engine, "demo", revision)


@pytest.mark.parametrize("target", ["result.json", "netlist.json"])
def test_tampered_metadata_evidence_rejected(tmp_path, target):
    engine, revision, folder = make_engine(tmp_path)
    directory = record_metadata(engine, revision, folder, {"MPN": "AUTHENTIC"})
    with (directory / target).open("a", encoding="utf-8") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="evidence is invalid"):
        build_inventory(engine, "demo", revision)


def test_mutation_during_inventory_rejected_by_final_verification(tmp_path, monkeypatch):
    engine, revision, folder = make_engine(tmp_path)
    original = inventory._inventory
    def mutate(*args):
        result = original(*args)
        with (folder / "design" / "board.kicad_pcb").open("a", encoding="utf-8") as stream:
            stream.write(" ")
        return result
    monkeypatch.setattr(inventory, "_inventory", mutate)
    with pytest.raises(ValueError, match="changed outside"):
        build_inventory(engine, "demo", revision)


def test_hash_of_consumed_snapshot_is_checked(tmp_path, monkeypatch):
    engine, revision, _ = make_engine(tmp_path)
    original = Path.read_bytes
    def changed_read(path):
        raw = original(path)
        return raw + b" " if path.name == "board.kicad_pcb" else raw
    monkeypatch.setattr(Path, "read_bytes", changed_read)
    with pytest.raises(ValueError, match="source hash"):
        build_inventory(engine, "demo", revision)


def test_constraint_snapshot_hash_is_checked(tmp_path, monkeypatch):
    engine, revision, _ = make_engine(tmp_path)
    original = Path.read_bytes
    def changed_read(path):
        raw = original(path)
        return raw + b" " if path.name == "constraints.json" else raw
    monkeypatch.setattr(Path, "read_bytes", changed_read)
    with pytest.raises(ValueError, match="constraints hash"):
        build_inventory(engine, "demo", revision)


def test_revision_metadata_change_during_read_is_rejected(tmp_path, monkeypatch):
    engine, revision, _ = make_engine(tmp_path)
    original = engine._verified
    calls = 0
    def verified(*args):
        nonlocal calls
        calls += 1
        data, folder = original(*args)
        if calls >= 3:
            data = {**data, "digest": "changed"}
        return data, folder
    monkeypatch.setattr(engine, "_verified", verified)
    with pytest.raises(ValueError, match="revision or metadata changed"):
        build_inventory(engine, "demo", revision)


def test_metadata_mutation_during_read_is_rejected(tmp_path, monkeypatch):
    engine, revision, folder = make_engine(tmp_path)
    directory = record_metadata(engine, revision, folder, {"MPN": "AUTHENTIC"})
    original = inventory._inventory
    def mutate(*args):
        result = original(*args)
        with (directory / "result.json").open("a", encoding="utf-8") as stream:
            stream.write(" ")
        return result
    monkeypatch.setattr(inventory, "_inventory", mutate)
    with pytest.raises(ValueError, match="revision or metadata changed"):
        build_inventory(engine, "demo", revision)


def test_sealed_revision_read_finishes_while_long_writer_keeps_lock(tmp_path):
    engine, revision, _ = make_engine(tmp_path)
    expected = build_inventory(engine, "demo", revision)
    locked, release = Event(), Event()
    def writer():
        with engine.store.lock("demo"):
            # Model the actual long-job lock and unpublished child revision;
            # no native router or artificial 30-minute delay is needed.
            engine._new("demo", revision)
            locked.set()
            assert release.wait(15)
    with ThreadPoolExecutor(max_workers=2) as pool:
        job = pool.submit(writer)
        try:
            assert locked.wait(10)
            reader = pool.submit(build_inventory, engine, "demo", revision)
            assert reader.result(timeout=10) == expected
            assert not job.done()
        finally:
            release.set()
        job.result(timeout=10)


def test_parallel_reads_overlap_without_lock_or_file_writes(tmp_path, monkeypatch):
    engine, revision, folder = make_engine(tmp_path)
    record_metadata(engine, revision, folder, {"MPN": "READ-ONLY"})
    expected = build_inventory(engine, "demo", revision)
    history = engine.store.history("demo")
    project_dir = engine.store.project_dir("demo")
    before = {p: digest(p) for p in project_dir.rglob("*") if p.is_file()}
    rendezvous = Barrier(4, timeout=10)
    original = inventory._inventory
    def overlap(*args):
        rendezvous.wait()
        return original(*args)
    def forbidden_lock(*args):
        raise AssertionError("Read-only inventory must not request a write lock")
    monkeypatch.setattr(inventory, "_inventory", overlap)
    monkeypatch.setattr(engine.store, "lock", forbidden_lock)
    with ThreadPoolExecutor(max_workers=4) as pool:
        readers = [pool.submit(build_inventory, engine, "demo", revision) for _ in range(4)]
        assert all(reader.result(timeout=15) == expected for reader in readers)
    assert engine.store.history("demo") == history
    assert {p: digest(p) for p in project_dir.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("target", ["design/board.kicad_pcb", "constraints.json",
                                   "verification/v-metadata/result.json", "verification/v-metadata/netlist.json"])
def test_concurrent_mutation_still_fails_closed(tmp_path, monkeypatch, target):
    engine, revision, folder = make_engine(tmp_path)
    record_metadata(engine, revision, folder, {"MPN": "AUTHENTIC"})
    snapshot_ready, changed = Event(), Event()
    original = inventory._inventory
    def pause_after_snapshot(*args):
        result = original(*args)
        snapshot_ready.set()
        assert changed.wait(10)
        return result
    monkeypatch.setattr(inventory, "_inventory", pause_after_snapshot)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(build_inventory, engine, "demo", revision)
        try:
            assert snapshot_ready.wait(10)
            with (folder / target).open("a", encoding="utf-8") as stream:
                stream.write(" ")
        finally:
            changed.set()
        with pytest.raises(ValueError, match="changed outside|revision or metadata changed"):
            reader.result(timeout=10)


def test_unsealed_staging_revision_cannot_be_read(tmp_path):
    engine, revision, _ = make_engine(tmp_path)
    staging, _, _ = engine._new("demo", revision)
    with pytest.raises(ValueError):
        build_inventory(engine, "demo", staging)


def test_real_160_component_fixture_without_native_execution(tmp_path):
    source = ROOT / "examples/system-controller/kit-dev-coldfire-xilinx_5213.kicad_pcb"
    before = digest(source)
    engine = EngineeringService(tmp_path / "managed")
    imported = engine.import_project("system", str(source))
    result = build_inventory(engine, "system", imported["revision"]["id"])
    assert result["summary"] == dict(component_count=160, pad_count=825, net_count=278,
                                     track_count=0, via_count=0, layer_count=4, package_count=35)
    assert len(result["components"]) == 160
    assert sum(n["pad_count"] for n in result["nets"]) + result["coverage"]["unassigned_pad_count"] == 825
    assert result["coverage"]["truncated"] is False
    assert all(n["connectivity_status"] == "not_evaluated" for n in result["nets"])
    assert digest(source) == before


@pytest.mark.skipif(
    not (ROOT / "data/projects/system-mcp-acceptance/revisions/r-9b5f5075e65e4931/revision.json").exists(),
    reason="Archived native routed revision is not bundled in every checkout",
)
def test_archived_real_routed_revision_and_verified_metadata_are_compatible():
    engine = EngineeringService(ROOT / "data")
    result = build_inventory(engine, "system-mcp-acceptance", "r-9b5f5075e65e4931")
    assert result["summary"] == dict(component_count=160, pad_count=825, net_count=278,
                                     track_count=2644, via_count=164, layer_count=4, package_count=35)
    assert len(result["tracks"]) == 2644 and len(result["vias"]) == 164
    assert result["sources"]["board"]["sha256"] == "846bbc6518ffada24cfa21945051e4b565964cff6dc36454ba875aa4dcc6e605"
    assert result["sources"]["verification"] == {
        "status": "blocked", "verification_id": "v-0dfa4d2c10e0", "connectivity_status": "passed"}
    assert all(n["connectivity_status"] == "not_evaluated" for n in result["nets"])
    assert sum(n["track_count"] for n in result["nets"]) == 2644
    assert sum(n["via_count"] for n in result["nets"]) == 164
    assert result["coverage"]["metadata_missing"] == dict(manufacturer=160, mpn=160, datasheet=160)
    chips = [c for c in result["components"] if c["category"] == "integrated_circuit"]
    assert chips and all(c["mpn"] is None and c["category_basis"]["verified"] is False for c in chips)
