import json
import shutil
from types import SimpleNamespace

import pytest
import sexpdata

from pcb_weaver.board import read_board
from pcb_weaver.native_bridge import _verify_track_minima
from pcb_weaver.service import EngineeringService, _audit_persisted_track_minima, design_files
from pcb_weaver.storage import digest, read_json, write_json
from test_service import EXAMPLE, FakeTools, schematic


def entry(minimum=0.6, legacy=False):
    value = {"class": "PCBWeaver_GND", "minimums": {"track_width": 0.6}}
    if not legacy:
        value["declared_min_width_mm"] = minimum
    return value


@pytest.fixture
def routed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "demo.kicad_pcb"
    shutil.copy2(EXAMPLE, path)
    tree = sexpdata.loads(path.read_text())
    tree.append(sexpdata.loads('(segment (start 30 30) (end 31 30) (width 0.3) (layer "F.Cu") (net 1))'))
    path.write_text(sexpdata.dumps(tree), encoding="utf-8")
    schematic(path.with_suffix(".kicad_sch"))
    service = EngineeringService(tmp_path / "managed")
    service.toolchain = FakeTools(read_board(path))
    return service, path


def project_with(metadata):
    return {"board": {"design_settings": {"rules": {"min_track_width": 0.25}}},
            "net_settings": {"meta": {"version": 3}, "classes": [
                {"name": "Default", "track_width": 0.25}, {"name": "PCBWeaver_GND", "track_width": 0.6}],
                "netclass_assignments": {"GND": "PCBWeaver_GND"}},
            "pcb_weaver_net_rules": metadata}


def legacy_revision(routed, metadata):
    service, path = routed
    parent = service.import_project("demo", str(path))["revision"]
    revision, folder, _ = service._new("demo", parent["id"])
    project_path = (folder / "design" / parent["board"]).with_suffix(".kicad_pro")
    project = read_json(project_path)
    project["pcb_weaver_net_rules"] = metadata
    # Model invalid metadata already sealed by an older version, including NaN.
    project_path.write_text(json.dumps(project), encoding="utf-8")
    return service._seal("demo", revision, folder, parent["board"], parent["id"], "legacy-import")


def assert_blocked_before_export(service, revision):
    check = service.verify_revision("demo", revision)
    assert check["constraints"]["passed"] is True
    assert check["drc"]["errors"] == check["erc"]["errors"] == 0
    assert check["connectivity"]["status"] == "passed"
    assert check["status"] == "blocked"
    assert check["persisted_track_minima"]["passed"] is False
    release = service.build_release("demo", revision)
    assert release["status"] == "blocked"
    assert release["verification"]["verification_id"] != check["verification_id"]
    assert release["verification"]["persisted_track_minima"]["passed"] is False
    assert service.toolchain.exports == 0
    assert not list(service.store.root.rglob("*.zip"))
    return check


@pytest.mark.parametrize("legacy", [False, True])
def test_already_routed_import_cannot_bypass_retained_floor(routed, legacy):
    service, path = routed
    write_json(path.with_suffix(".kicad_pro"), project_with({"GND": entry(legacy=legacy)}))
    originals = design_files(path.parent)
    data = service.import_project("demo", str(path))["revision"]
    assert read_board(service.store.revision_dir("demo", data["id"]) / "design" / path.name)["tracks"] == 1
    check = assert_blocked_before_export(service, data["id"])
    width = check["persisted_track_minima"]
    assert width["violations"] == [{"code": "persisted_track_width", "track_index": 0, "net": "GND",
                                     "actual_mm": 0.3, "required_mm": 0.6, "actual_nm": 300000, "required_nm": 600000}]
    assert width["minimum_sources"]["GND"] == ("legacy.minimums.track_width" if legacy else "declared_min_width_mm")
    folder = service.store.revision_dir("demo", data["id"])
    evidence = folder / "verification" / check["verification_id"] / "persisted-track-minima.json"
    assert check["evidence_hashes"][evidence.name] == digest(evidence)
    assert width["revision"] == data["id"] and width["revision_digest"] == data["digest"]
    assert width["board_sha256"] == data["files"][path.name]
    assert width["project_sha256"] == data["files"][path.with_suffix(".kicad_pro").name]
    assert design_files(path.parent) == originals
    assert service._verified("demo", data["id"])[0] == data


@pytest.mark.parametrize("metadata", [None, [], "invalid", {"GND": None}, {"GND": []},
    {"GND": {}}, {"GND": {"minimums": None}}, {"GND": {"minimums": {}}},
    {"GND": {"minimums": {"track_width": None}}}, {"GND": {"minimums": {"track_width": 0}}},
    {"GND": {"minimums": {"track_width": -0.1}}}, {"GND": {"minimums": {"track_width": True}}},
    {"GND": {"minimums": {"track_width": "0.6"}}},
    {"": entry()}, {"missing-net": entry()}])
def test_invalid_legacy_metadata_blocks_verify_and_release(routed, metadata):
    service, _ = routed
    data = legacy_revision(routed, metadata)
    check = assert_blocked_before_export(service, data["id"])
    assert check["persisted_track_minima"]["unknowns"]


@pytest.mark.parametrize("value", [0, -0.1, None, True, "0.6", float("nan"), float("inf"), float("-inf"), 0.0000001])
def test_invalid_explicit_value_never_falls_back_to_valid_legacy(routed, value):
    service, _ = routed
    data = legacy_revision(routed, {"GND": entry(value)})
    check = assert_blocked_before_export(service, data["id"])
    assert check["persisted_track_minima"]["unknowns"]


def test_explicit_floor_is_not_confused_with_nominal_legacy_width(routed):
    service, path = routed
    write_json(path.with_suffix(".kicad_pro"), project_with({"GND": entry(0.3)}))
    data = service.import_project("demo", str(path))["revision"]
    check = service.verify_revision("demo", data["id"])
    assert check["status"] == "passed"
    assert check["persisted_track_minima"]["per_net_minimum_mm"] == {"GND": 0.3}
    release = service.build_release("demo", data["id"])
    assert release["status"] == "released"
    assert service.toolchain.exports == 1


def test_global_project_floor_overrides_smaller_explicit_floor(routed):
    _, path = routed
    project = project_with({"GND": entry(0.2)})
    project["board"]["design_settings"]["rules"]["min_track_width"] = 0.4
    write_json(path.with_suffix(".kicad_pro"), project)
    check = _audit_persisted_track_minima(read_board(path), path)
    assert check["passed"] is False
    assert check["violations"][0]["required_nm"] == 400000


@pytest.mark.parametrize("width", [0.599999, 0.6, 0.600001])
@pytest.mark.parametrize("legacy", [False, True])
def test_width_comparison_matches_native_oracle_at_one_nm(routed, width, legacy):
    _, path = routed
    write_json(path.with_suffix(".kicad_pro"), project_with({"GND": entry(legacy=legacy)}))
    board = read_board(path)
    board["track_items"][0]["width_mm"] = width
    check = _audit_persisted_track_minima(board, path)
    via = type("Via", (), {})
    pcbnew = SimpleNamespace(PCB_VIA=via, FromMM=lambda v: round(v * 1e6), ToMM=lambda v: v / 1e6)
    native = SimpleNamespace(GetTracks=lambda: [SimpleNamespace(GetNetname=lambda: "GND", GetWidth=lambda: round(width * 1e6))])
    try:
        oracle = _verify_track_minima(pcbnew, native, path)
    except ValueError:
        assert check["passed"] is False
    else:
        assert check["passed"] is oracle["verified"] is True
        assert check["per_net_minimum_mm"] == oracle["per_net_minimum_mm"]


@pytest.mark.parametrize("mutation", ["missing_tracks", "incomplete_tracks", "missing_net", "unknown_net", "missing_width", "nonfinite_width", "sub_nm_width"])
def test_incomplete_track_geometry_is_unknown_not_passed(routed, mutation):
    _, path = routed
    write_json(path.with_suffix(".kicad_pro"), project_with({}))
    board = read_board(path)
    if mutation == "missing_tracks":
        del board["track_items"]
    elif mutation == "incomplete_tracks":
        board["tracks"] += 1
    elif mutation == "missing_net":
        del board["track_items"][0]["net"]
    elif mutation == "unknown_net":
        board["track_items"][0]["net"] = "not-a-net"
    elif mutation == "missing_width":
        del board["track_items"][0]["width_mm"]
    else:
        board["track_items"][0]["width_mm"] = float("nan") if mutation == "nonfinite_width" else 0.3000001
    check = _audit_persisted_track_minima(board, path)
    assert check["passed"] is False and check["unknowns"]


@pytest.mark.parametrize("raw", ["null", "[]", "{}", "not json",
    '{"board":null}', '{"pcb_weaver_net_rules":{},"pcb_weaver_net_rules":{}}'])
def test_invalid_project_document_is_unknown_not_passed(routed, raw):
    _, path = routed
    path.with_suffix(".kicad_pro").write_text(raw, encoding="utf-8")
    check = _audit_persisted_track_minima(read_board(path), path)
    assert check["passed"] is False and check["unknowns"]


def test_one_revisions_success_cannot_cover_another_revisions_floor(routed):
    service, path = routed
    first = service.import_project("demo", str(path))["revision"]
    assert service.verify_revision("demo", first["id"])["status"] == "passed"
    write_json(path.with_suffix(".kicad_pro"), project_with({"GND": entry()}))
    second = service.import_project("demo", str(path), parent_revision=first["id"])["revision"]
    check = assert_blocked_before_export(service, second["id"])
    assert check["revision_digest"] == second["digest"] != first["digest"]
    assert service.verify_revision("demo", first["id"])["status"] == "passed"


@pytest.mark.parametrize("value", [None, 0, -0.1, True, "0.25", float("nan"), float("inf"), 0.0000001])
def test_legacy_invalid_global_floor_blocks_even_without_per_net_rules(routed, value):
    service, path = routed
    parent = service.import_project("demo", str(path))["revision"]
    revision, folder, _ = service._new("demo", parent["id"])
    project_path = (folder / "design" / parent["board"]).with_suffix(".kicad_pro")
    project = read_json(project_path)
    project["board"]["design_settings"]["rules"]["min_track_width"] = value
    project_path.write_text(json.dumps(project), encoding="utf-8")
    child = service._seal("demo", revision, folder, parent["board"], parent["id"], "legacy-import")
    check = assert_blocked_before_export(service, child["id"])
    assert check["persisted_track_minima"]["unknowns"]


@pytest.mark.parametrize("raw", [None, "null", "[]", '{"board":null}', "not json"])
def test_missing_or_invalid_sealed_project_cannot_release(routed, raw):
    service, path = routed
    parent = service.import_project("demo", str(path))["revision"]
    revision, folder, _ = service._new("demo", parent["id"])
    project_path = (folder / "design" / parent["board"]).with_suffix(".kicad_pro")
    if raw is None:
        project_path.unlink()
    else:
        project_path.write_text(raw, encoding="utf-8")
    child = service._seal("demo", revision, folder, parent["board"], parent["id"], "legacy-import")
    check = assert_blocked_before_export(service, child["id"])
    assert check["persisted_track_minima"]["unknowns"]
