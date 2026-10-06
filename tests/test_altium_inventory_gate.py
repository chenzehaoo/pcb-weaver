"""Offline inventory consistency tests, never native routing/DRC acceptance."""
import configparser
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("altium_inventory_gate", ROOT / "scripts/altium_inventory_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
EVIDENCE = ROOT / "docs/validation/altium-native/a201905ecc7b4c3eb819e531d52c5f1e/result.json"


@pytest.fixture
def inventory(tmp_path):
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    source = tmp_path / "original.PcbDoc"
    source.write_bytes(b"offline test board snapshot, not a native PCB")
    folder = tmp_path / "inventory"
    folder.mkdir()
    board = folder / "snapshot.PcbDoc"
    board.write_bytes(source.read_bytes())
    data.update(source=str(source), board=str(board), source_sha256=gate._sha(source))
    data["native"]["job"]["board"] = str(board)
    return folder / "result.json", data


def persist(inventory):
    path, data = inventory
    path.write_text(json.dumps(data), encoding="utf-8")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict({section: {key: str(value) for key, value in row.items()}
                      for section, row in data.get("native", {}).items()})
    with (path.parent / "response.ini").open("w", encoding="utf-8") as stream:
        parser.write(stream)
    return path


def invalid(inventory):
    result = gate.validate_inventory(persist(inventory))
    assert result["status"] == "blocked" and not result["inventory_valid"], result
    assert not result["full_autoroute"]["authorized"] and result["errors"]
    assert "integrity" not in result
    return result


def test_real_inventory_shape_counts_coverage_and_no_false_authorization(inventory):
    path = persist(inventory)
    inputs = [path, path.parent / "response.ini", Path(inventory[1]["source"]), Path(inventory[1]["board"])]
    before = {p: p.read_bytes() for p in inputs}
    result = gate.validate_inventory(path, expected_source_sha256=inventory[1]["source_sha256"])
    assert result["inventory_valid"], result
    assert result["counts"] == {"components": 28, "pads": 200, "nets": 27, "rules": 40}
    assert result["status"] == result["full_autoroute"]["status"] == "blocked"
    assert not result["full_autoroute"]["authorized"] and not result["manufacturing_authorized"]
    assert result["coverage"]["standalone_pads"] == 2
    assert result["coverage"]["unassigned_pads"] > 0
    assert result["coverage"]["rules_drc_enabled"] == 40
    assert all(result["coverage"][key] == "not_captured" for key in gate.MISSING_SEMANTICS)
    assert result["coverage"]["rule_scope_evaluation"] == "not_performed"
    assert result["native_drc"] == "not_verified_by_inventory"
    assert result["integrity"]["snapshot_byte_identical_to_original"]
    assert result["integrity"]["caller_sha256_pin_checked"]
    assert result["rule_metadata"][0] == inventory[1]["native"]["rule.0"]
    assert {p: p.read_bytes() for p in inputs} == before


@pytest.mark.parametrize("kind,key", [("component", "designator"), ("net", "name")])
@pytest.mark.parametrize("case_change", [False, True])
def test_duplicate_and_case_ambiguous_names(inventory, kind, key, case_change):
    native = inventory[1]["native"]
    value = native[kind+".0"][key]
    native[kind+".1"][key] = value.lower() if case_change else value
    invalid(inventory)


@pytest.mark.parametrize("field,value", [("component", "ghost"), ("net", "ghost"),
    ("component", "u1"), ("net", "gnd"), ("net", ""), ("component", "")])
def test_dangling_or_empty_pad_references(inventory, field, value):
    inventory[1]["native"]["pad.2"][field] = value
    invalid(inventory)


@pytest.mark.parametrize("section,field", [("component.0", "x"), ("component.0", "rotation"),
    ("pad.0", "y"), ("pad.0", "hole_size")])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-inf", "1_000", "", True, None])
def test_nonfinite_or_malformed_numeric_values(inventory, section, field, value):
    inventory[1]["native"][section][field] = value
    invalid(inventory)


@pytest.mark.parametrize("section,field,value", [("pad.0", "x", "1.5"),
    ("pad.0", "hole_size", "-1"), ("component.0", "layer", "-1"),
    ("rule.0", "priority", "0"), ("rule.0", "kind", "1.1"),
    ("rule.0", "drc_enabled", "yes"), ("rule.0", "scope1", "")])
def test_invalid_native_metadata(inventory, section, field, value):
    inventory[1]["native"][section][field] = value
    invalid(inventory)


def test_disabled_rule_retained_not_mistaken_for_drc_execution(inventory):
    inventory[1]["native"]["rule.0"]["drc_enabled"] = "False"
    result = gate.validate_inventory(persist(inventory))
    assert result["inventory_valid"]
    assert result["coverage"]["rules_drc_disabled"] == 1
    assert result["status"] == "blocked" and result["native_drc"] != "passed"


@pytest.mark.parametrize("fault", ["outer_status", "mode", "request", "job", "completion", "units",
    "board", "section_gap", "section_alias", "unknown_section", "missing_field", "empty_inventory"])
def test_section_and_request_contract(inventory, fault):
    _, data = inventory
    native = data["native"]
    if fault == "outer_status":
        data["status"] = "running"
    elif fault == "mode":
        data["mode"] = "DRC"
    elif fault == "request":
        native["job"]["request"] = "other"
    elif fault in {"job", "completion"}:
        del native[fault]
    elif fault == "units":
        native["job"]["units"] = "mm"
    elif fault == "board":
        native["job"]["board"] = data["source"]
    elif fault == "section_gap":
        del native["pad.5"]
    elif fault == "section_alias":
        native["pad.00"] = native.pop("pad.0")
    elif fault == "unknown_section":
        native["layerstack"] = {"complete": "true"}
    elif fault == "missing_field":
        del native["rule.0"]["scope2"]
    else:
        data["native"] = {key: native[key] for key in ("job", "completion")}
    invalid(inventory)


@pytest.mark.parametrize("fault", ["source", "snapshot", "sha", "unchanged", "missing", "alias", "outside"])
def test_file_hash_binding(inventory, fault):
    path, data = inventory
    if fault in {"source", "snapshot"}:
        Path(data["source" if fault == "source" else "board"]).write_bytes(b"changed")
    elif fault == "sha":
        data["source_sha256"] = "0" * 64
    elif fault == "unchanged":
        data["source_unchanged"] = "true"
    elif fault == "missing":
        data["source"] = str(path.parent / "missing.PcbDoc")
    elif fault == "alias":
        data["source"] = data["board"]
    else:
        data["board"] = data["source"]
    invalid(inventory)


def test_external_hash_pin(inventory):
    result = gate.validate_inventory(persist(inventory), expected_source_sha256="0" * 64)
    assert not result["inventory_valid"] and "pin" in result["errors"][0]


def test_hardlinked_snapshot_is_not_an_independent_copy(inventory):
    _, data = inventory
    board = Path(data["board"])
    board.unlink()
    board.hardlink_to(data["source"])
    invalid(inventory)


def test_unreviewed_numeric_rule_fields_do_not_upgrade_coverage(inventory):
    inventory[1]["native"]["rule.0"]["clearance"] = "200000"
    result = gate.validate_inventory(persist(inventory))
    assert result["inventory_valid"]
    assert result["coverage"]["rule_numeric_values"] == "not_captured"
    assert not result["full_autoroute"]["authorized"]


@pytest.mark.parametrize("fault", ["mismatch", "duplicate_json", "duplicate_ini", "default_ini", "missing_ini"])
def test_raw_evidence_consistency(inventory, fault):
    path = persist(inventory)
    response = path.parent / "response.ini"
    if fault == "mismatch":
        response.write_text(response.read_text().replace("designator = U2", "designator = Different"))
    elif fault == "duplicate_json":
        path.write_text(path.read_text().replace('"status": "completed"', '"status": "failed", "status": "completed"', 1))
    elif fault == "duplicate_ini":
        response.write_text(response.read_text() + "\n[job]\nrequest=other\n")
    elif fault == "default_ini":
        response.write_text("[DEFAULT]\nrequest=other\n" + response.read_text())
    else:
        response.unlink()
    result = gate.validate_inventory(path)
    assert not result["inventory_valid"] and result["status"] == "blocked"


def test_second_hash_check_detects_change_during_validation(inventory, monkeypatch):
    path = persist(inventory)
    real_sha, calls = gate._sha, []
    def changed(file):
        if str(file) == inventory[1]["source"]:
            calls.append(file)
            if len(calls) > 1:
                return "0" * 64
        return real_sha(file)
    monkeypatch.setattr(gate, "_sha", changed)
    result = gate.validate_inventory(path)
    assert not result["inventory_valid"] and "during validation" in result["errors"][0]


def test_cli_reports_blocked_without_writing(inventory, capsys):
    path = persist(inventory)
    assert gate.main([str(path)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["inventory_valid"] and not result["full_autoroute"]["authorized"]
