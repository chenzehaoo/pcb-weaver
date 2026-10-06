"""Offline DSN structure/binding checks; no GUI, conversion or routing execution."""
import configparser
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "docs/validation/altium-native/9dcb104cde14458ba7425ff6f306aa0e"
spec = importlib.util.spec_from_file_location("altium_dsn_audit", ROOT / "scripts/altium_dsn_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.fixture
def evidence(tmp_path):
    data = json.loads((EXPORT / "result.json").read_text())
    inventory = json.loads(audit.INVENTORY.read_text())
    source = tmp_path / "original.PcbDoc"
    source.write_bytes(b"offline byte fixture, not native PCB content")
    folder, inv_folder = tmp_path / "export", tmp_path / "inventory"
    folder.mkdir()
    inv_folder.mkdir()
    board, inv_board = folder / "WiFi.PcbDoc", inv_folder / "WiFi.PcbDoc"
    board.write_bytes(source.read_bytes())
    inv_board.write_bytes(source.read_bytes())
    sha = audit._sha(source.read_bytes())
    data.update(source=str(source), board=str(board), source_sha256=sha, source_snapshot_sha256=sha)
    data["native"]["job"]["board"] = str(board)
    inventory.update(source=str(source), board=str(inv_board), source_sha256=sha)
    inventory["native"]["job"]["board"] = str(inv_board)
    inv_path = inv_folder / "result.json"
    inv_path.write_text(json.dumps(inventory))
    (folder / "Job.pas").write_bytes(b"offline script hash fixture\r\n")
    state = {"path": folder / "result.json", "data": data, "inventory": inv_path,
             "inventory_sha256": audit._sha(inv_path.read_bytes()), "dsn": (EXPORT / "export.dsn").read_bytes()}
    persist(state)
    return state


def persist(state):
    folder, data = state["path"].parent, state["data"]
    (folder / "export.dsn").write_bytes(state["dsn"])
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict(data["native"])
    with (folder / "response.ini").open("w") as stream:
        parser.write(stream)
    data["response_sha256"] = audit._sha((folder / "response.ini").read_bytes())
    data["template_sha256"] = audit._sha((folder / "Job.pas").read_text().encode("ascii"))
    data["artifacts"] = {"export.dsn": audit._sha(state["dsn"])}
    state["path"].write_text(json.dumps(data))


def run(state):
    return audit.audit_export(state["path"], inventory_path=state["inventory"], inventory_sha256=state["inventory_sha256"])


def rejected(state):
    result = run(state)
    assert result["status"] == "blocked" and not result["audit_valid"], result
    assert result["errors"] and not result["full_autoroute_authorized"]
    return result


def dump(node):
    if isinstance(node, list):
        return "(" + " ".join(dump(child) for child in node) + ")"
    return json.dumps(str(node)) if isinstance(node, audit.Quoted) else node


def test_real_export_shape_mapping_and_always_blocked(evidence):
    before = {file: file.read_bytes() for file in evidence["path"].parent.parent.rglob("*") if file.is_file()}
    result = run(evidence)
    assert result["audit_valid"], result
    assert result["counts"] == {"layers": 4, "nets": 27, "placements": 28, "images": 28, "padstacks": 26,
        "image_pins": 198, "network_pins": 111, "rule_scopes": 1, "boundaries": 2, "keepouts": 7,
        "polygons": 5, "wires": 240, "vias": 86}
    assert result["mapping"]["matched_components"] == 28
    assert result["mapping"]["matched_pad_names"] == 198
    assert result["mapping"]["matching_assigned_pad_nets"] == 111
    assert result["mapping"]["pad_net_mismatches"] == []
    assert set(result["mapping"]["unmapped_standalone_pads"]) == {"MH1", "MH2"}
    assert result["units"]["resolution"] == ["MIL", "10000"] and result["units"]["unit_declarations"] == []
    assert not result["units"]["equivalence_verified"]
    assert all(row["raw_place"][4:] == ["front", "0"] for row in result["placement_evidence"])
    assert any(row["native_rotation"] != "0" for row in result["placement_evidence"])
    assert len(result["coverage"]["unmapped_enabled_rules"]) == 40
    assert not result["coverage"]["complete"] and not result["full_autoroute_authorized"]
    assert {row["tag"] for row in result["unmapped_constructs"]} == {"grid"}
    assert before == {file: file.read_bytes() for file in before}


def test_parser_preserves_numeric_pin_names_and_quoted_punctuation():
    tree = audit.parse_dsn(b'(pcb "A (test).PcbDoc" (image "part-A" (pin Pad1 001 -1.000000000000001 2)))')
    assert tree[1] == "A (test).PcbDoc" and isinstance(tree[1], audit.Quoted)
    assert tree[2][2][2:] == ["001", "-1.000000000000001", "2"]


@pytest.mark.parametrize("raw", [b"", b"()", b"(pcb", b"(pcb x))", b"(pcb x)(pcb y)", b"x (pcb y)",
    b'(pcb "unclosed)', b'(pcb "a"b)', b'(pcb "bad\\n")', b'(pcb a\x00b)', b'(pcb x ;comment)', b'("pcb" x)',
    b'(session x)', b'(pcb x)' + b'\x00', b'(' * 66 + b'x' + b')' * 66])
def test_strict_s_expression_failures(raw):
    with pytest.raises(ValueError):
        audit.parse_dsn(raw)


@pytest.mark.parametrize("fault", ["resolution", "duplicate_resolution", "duplicate_layer", "image", "stack", "pin",
    "duplicate_pin", "wire_net", "shape_layer", "coordinate", "polygon", "container_atom", "pcb_name"])
def test_structural_reference_and_numeric_faults(evidence, fault):
    tree = audit.parse_dsn(evidence["dsn"])
    structure, library, placement, network, wiring = [audit._one(tree, key) for key in ("structure", "library", "placement", "network", "wiring")]
    if fault == "resolution":
        audit._one(tree, "resolution")[2] = "0"
    elif fault == "duplicate_resolution":
        tree.append(audit._one(tree, "resolution"))
    elif fault == "duplicate_layer":
        structure.append(audit._children(structure, "layer")[0])
    elif fault == "image":
        placement[1][1] = "unknown"
    elif fault == "stack":
        audit._children(library, "image")[0][2][1] = "unknown"
    elif fault in {"pin", "duplicate_pin"}:
        pins = audit._one(audit._children(network, "net")[0], "pins")
        pins.append("ghost-1" if fault == "pin" else pins[1])
    elif fault == "wire_net":
        audit._one(wiring[1], "net")[1] = "unknown"
    elif fault in {"shape_layer", "coordinate"}:
        path = audit._one(wiring[1], "path")
        path[1 if fault == "shape_layer" else 3] = "unknown" if fault == "shape_layer" else "NaN"
    elif fault == "polygon":
        poly = next(node for _, node in audit._walk(tree) if node[0] == "polygon")
        poly.pop()
    elif fault == "container_atom":
        placement.append("unexpected")
    else:
        tree[1] = "other.PcbDoc"
    evidence["dsn"] = dump(tree).encode("utf-8")
    persist(evidence)
    rejected(evidence)


def test_inventory_net_difference_is_reported_not_silently_equated(evidence):
    tree = audit.parse_dsn(evidence["dsn"])
    nets = audit._children(audit._one(tree, "network"), "net")
    pin = audit._one(nets[0], "pins").pop()
    audit._one(nets[1], "pins").append(pin)
    evidence["dsn"] = dump(tree).encode()
    persist(evidence)
    result = run(evidence)
    assert result["audit_valid"] and result["mapping"]["pad_net_mismatches"][0]["pin"] == pin
    assert result["status"] == "blocked"


def test_unknown_construct_retained_as_unmapped_evidence(evidence):
    tree = audit.parse_dsn(evidence["dsn"])
    tree.append(["new_export_scope", "opaque"])
    evidence["dsn"] = dump(tree).encode()
    persist(evidence)
    result = run(evidence)
    assert result["audit_valid"] and any(row["tag"] == "new_export_scope" for row in result["unmapped_constructs"])
    assert not result["coverage"]["complete"]


@pytest.mark.parametrize("file", ["export.dsn", "response.ini", "Job.pas", "WiFi.PcbDoc"])
@pytest.mark.parametrize("fault", ["missing", "tamper"])
def test_required_evidence_files(evidence, file, fault):
    path = evidence["path"].parent / file
    if fault == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"changed")
    rejected(evidence)


@pytest.mark.parametrize("fault", ["status", "mode", "request", "native_board", "completion", "source",
    "snapshot_pin", "inventory_pin", "source_flag", "response_mismatch", "duplicate_json"])
def test_identity_and_hash_binding(evidence, fault):
    data = evidence["data"]
    if fault == "status":
        data["status"] = "failed"
    elif fault == "mode":
        data["mode"] = "DRC"
    elif fault == "request":
        data["native"]["job"]["request"] = "other"
    elif fault == "native_board":
        data["native"]["job"]["board"] = data["source"]
    elif fault == "completion":
        data["native"]["completion"]["status"] = "incomplete"
    elif fault == "source":
        Path(data["source"]).write_bytes(b"changed")
    elif fault == "snapshot_pin":
        data["source_snapshot_sha256"] = "0" * 64
    elif fault == "inventory_pin":
        evidence["inventory"].write_bytes(evidence["inventory"].read_bytes() + b" ")
    elif fault == "source_flag":
        data["source_unchanged"] = "true"
    persist(evidence)
    if fault == "response_mismatch":
        data["native"]["job"]["unbound"] = "x"
        evidence["path"].write_text(json.dumps(data))
    elif fault == "duplicate_json":
        text = evidence["path"].read_text().replace('"status": "completed"', '"status": "failed", "status": "completed"', 1)
        evidence["path"].write_text(text)
    rejected(evidence)


def test_change_during_parse_is_rejected(evidence, monkeypatch):
    original = audit.audit_structure
    def changed(tree, inventory):
        result = original(tree, inventory)
        Path(evidence["data"]["source"]).write_bytes(b"changed during audit")
        return result
    monkeypatch.setattr(audit, "audit_structure", changed)
    rejected(evidence)
