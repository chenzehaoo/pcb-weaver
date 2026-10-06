"""Copy-only geometry and mocked native gates; not native acceptance evidence."""
from copy import deepcopy
from pathlib import Path
import os
import subprocess

import pytest
import sexpdata

from pcb_weaver import completion_cleanup as module
from pcb_weaver.board import _tag, _children
from pcb_weaver.repair_geometry import _read, _serialize
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, read_json
from test_repair import check, report
from test_completion import setup


EXAMPLE = Path(__file__).resolve().parents[1] / "examples/manufacturing-demo/two-layer.kicad_pcb"


def segment(identity, start, end, layer="F.Cu", net=2):
    return sexpdata.loads(f'(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) '
                          f'(width 0.2) (layer "{layer}") (net {net}) (uuid "{identity}"))')


def joint(identity="v1", at=(45, 37), layer="F.Cu"):
    x, y = at
    return [segment(identity+"a", (x-2, y), at, layer),
            segment(identity+"b", at, (x+2, y+1), layer),
            sexpdata.loads(f'(via (at {x} {y}) (size 0.6) (drill 0.3) '
                           f'(layers "F.Cu" "B.Cu") (net 2) (uuid "{identity}"))')]


def finding(identity="v1", kind="via_dangling", severity="warning"):
    return {"type": kind, "severity": severity, "description": kind,
            "items": [{"uuid": identity}]}


@pytest.fixture
def boards(tmp_path):
    original, source, output = [tmp_path / (name+".kicad_pcb") for name in ("original", "source", "output")]
    original.write_bytes(EXAMPLE.read_bytes())
    ast = _read(original)[0]
    source.write_bytes(_serialize(ast + joint()))
    return original, source, output


def remove(boards, ids=None, **kwargs):
    original, source, output = boards
    return module.remove_redundant_vias(source, output, ["v1"] if ids is None else ids,
        source_sha256=kwargs.pop("source_sha256", digest(source)), original=original, **kwargs)


def test_batch_exact_retained_ast_hashes_and_partitions(boards):
    original, source, output = boards
    source.write_bytes(_serialize(_read(source)[0] + joint("v2", (53, 37), "B.Cu")))
    original_bytes, source_bytes = original.read_bytes(), source.read_bytes()
    before = _read(source)[0]
    proof = remove(boards, ["v1", "v2"])
    assert _read(output)[0] == [n for n in before if _tag(n) != "via"]
    assert original.read_bytes() == original_bytes and source.read_bytes() == source_bytes
    assert proof["partitions_preserved"] and proof["retained_ast_preserved"]
    assert proof["retained_partitions"]["VIN"] == [["v1a", "v1b"], ["v2a", "v2b"]]
    assert proof["output_sha256"] == digest(output)
    assert proof["requires_native_verification"] and not proof["manufacturing_authorized"]
    assert all("via" in row["removed_node"] for row in proof["contacts"])


@pytest.mark.parametrize("fault", ["locked", "cross_layer", "different_net", "near_endpoint",
    "third_segment", "other_via", "pad", "unknown", "segment", "old_copper", "zone", "group", "arc"])
def test_unsafe_candidates_fail_closed_without_source_changes(boards, fault):
    original, source, output = boards
    ast = _read(source)[0]
    a, b, via = ast[-3:]
    identities = ["v1"]
    if fault == "locked":
        via.append(sexpdata.Symbol("locked"))
    elif fault == "cross_layer":
        _children(b, "layer")[0][1] = "B.Cu"
    elif fault == "different_net":
        _children(b, "net")[0][1] = 1
    elif fault == "near_endpoint":
        _children(b, "start")[0][1] += .000001
    elif fault == "third_segment":
        ast.append(segment("extra", (45, 37.39), (45, 39), "B.Cu"))
    elif fault == "other_via":
        ast.append(joint("extra", (45.5, 37))[-1])
    elif fault == "pad":
        ast = ast[:-3] + joint(at=(23.73, 35))
    elif fault == "unknown":
        identities = ["unknown"]
    elif fault == "segment":
        identities = ["v1a"]
    elif fault == "old_copper":
        original.write_bytes(_serialize(_read(original)[0] + [a]))
    elif fault in {"zone", "group", "arc"}:
        ast.append([sexpdata.Symbol(fault)])
    source.write_bytes(_serialize(ast))
    before = source.read_bytes(), original.read_bytes()
    with pytest.raises(ValueError):
        remove(boards, identities)
    assert not output.exists()
    assert (source.read_bytes(), original.read_bytes()) == before


@pytest.mark.parametrize("ids", [[], ["v1", "v1"], [""], "v1", [str(i) for i in range(33)]])
def test_invalid_batch(boards, ids):
    with pytest.raises(ValueError):
        remove(boards, ids)


def test_hash_and_output_alias_guards(boards):
    original, source, output = boards
    with pytest.raises(ValueError, match="hash"):
        remove(boards, source_sha256="stale")
    with pytest.raises(ValueError, match="new"):
        remove((original, source, source))
    output.write_bytes(b"keep")
    with pytest.raises(ValueError, match="new"):
        remove(boards)
    assert output.read_bytes() == b"keep"


def test_partition_disagreement_retains_unaccepted_proposal(boards, monkeypatch):
    original, source, output = boards
    real = module._retained_partitions
    monkeypatch.setattr(module, "_retained_partitions", lambda path, net, removed:
                        [["split"]] if path == output else real(path, net, removed))
    with pytest.raises(ValueError, match="partitions"):
        remove(boards)
    assert output.exists()


@pytest.mark.parametrize("change", ["duplicate", "excluded", "two_items", "error", "empty_uuid"])
def test_bad_native_finding_selection(change):
    row = finding()
    rows = [row]
    if change == "duplicate":
        rows.append(deepcopy(row))
    elif change == "excluded":
        row["excluded"] = True
    elif change == "two_items":
        row["items"].append({"uuid": "v2"})
    elif change == "error":
        row["severity"] = "error"
    else:
        row["items"][0]["uuid"] = ""
    with pytest.raises(ValueError):
        module.dangling_via_ids({"violations": rows})


@pytest.fixture
def gate(tmp_path, monkeypatch):
    engine = EngineeringService(tmp_path / "workspace")
    original = engine.import_project("p", str(EXAMPLE))["revision"]["id"]
    rid, folder, data = engine._new("p", original)
    source = folder / "design" / data["board"]
    source.write_bytes(_serialize(_read(source)[0] + joint()))
    engine._seal("p", rid, folder, data["board"], original, "route", {})
    state = {"native_calls": [], "bindings": [], "fault": None}

    def verified_check(revision):
        value = check(1)
        metadata, root = engine._verified("p", revision)
        value.update(verification_id="v-"+revision, evidence_hashes={"drc.json": "mock", "erc.json": "mock"})
        value["drc"]["source_sha256"] = digest(root / "design" / metadata["board"])
        return value

    baseline, before = verified_check(original), verified_check(rid)

    def native(p, revision):
        assert revision not in {original, rid}, "Only the new child may be verified"
        state["native_calls"].append(revision)
        value = verified_check(revision)
        if state["fault"] == "missing":
            value["drc"].update(unconnected=2, errors=2)
        elif state["fault"] == "quality":
            value["constraints"]["passed"] = False
        elif state["fault"] in {"erc_count", "drc_count"}:
            value[state["fault"][:3]]["warnings"] = 1
        return value

    def bound_report(e, p, revision, value):
        state["bindings"].append(revision)
        if state["fault"] == "binding" and revision == rid:
            raise ValueError("Repair verification changed or is no longer authentic")
        if state["fault"] == "child_binding" and revision not in {original, rid}:
            raise ValueError("Repair requires authenticated native DRC evidence")
        raw = report([] if revision == original else [("v1a", "v1b")])
        if revision == rid:
            raw["violations"] = [finding()]
        elif revision != original:
            if state["fault"] == "missing":
                raw["unconnected_items"] *= 2
            elif state["fault"] == "summary":
                raw["unconnected_items"] = []
            elif state["fault"] == "new_warning":
                raw["violations"] = [finding("v1a", "track_dangling")]
            elif state["fault"] == "target_remains":
                raw["violations"] = [finding()]
        return raw

    def erc(e, p, revision, value):
        rows = []
        if state["fault"] == "erc" and revision not in {original, rid}:
            rows = [{"uuid_path": "/", "violations": [finding("symbol", "pin_warning")]}]
        return {"$schema": "https://schemas.kicad.org/erc.v1.json", "sheets": rows}

    monkeypatch.setattr(engine, "_verify", native)
    monkeypatch.setattr(module, "_report", bound_report)
    monkeypatch.setattr(module, "_erc_report", erc)
    state.update(engine=engine, original=original, revision=rid, before=before, baseline=baseline,
                 source=source, folder=folder)
    return state


def cleanup(gate, **kwargs):
    return module.cleanup_dangling_vias(gate["engine"], "p", gate["revision"], gate["before"],
                                      gate["original"], gate["baseline"], **kwargs)


def test_child_only_gate_accepts_same_missing_retains_proof(gate):
    original_bytes = gate["source"].read_bytes()
    result = cleanup(gate)
    assert result["status"] == "improved", result
    assert result["revision"] == result["candidate_revision"]
    assert gate["native_calls"] == [result["revision"]]
    assert gate["bindings"].count(gate["revision"]) == 2
    assert gate["original"] in gate["bindings"]
    assert result["proof"]["partitions_preserved"] and result["comparison"]["accepted"]
    assert result["comparison"]["before_by_net"] == result["comparison"]["after_by_net"] == {"VIN": 1}
    assert read_json(Path(result["evidence_path"]))["candidate_revision"] == result["revision"]
    assert gate["source"].read_bytes() == original_bytes
    assert not result["manufacturing_authorized"]


@pytest.mark.parametrize("fault", ["missing", "new_warning", "erc", "erc_count", "drc_count", "quality",
                                   "target_remains", "summary", "child_binding"])
def test_native_rejection_keeps_parent_and_failed_child(gate, fault):
    gate["fault"] = fault
    result = cleanup(gate)
    assert result["status"] == "blocked", result
    assert result["revision"] == gate["revision"]
    assert len(gate["native_calls"]) == 1
    assert result["candidate_revision"] == gate["native_calls"][0]
    assert result["proof"]["output_sha256"]
    assert Path(result["evidence_path"]).is_file()
    gate["engine"]._verified("p", result["candidate_revision"])


@pytest.mark.parametrize("fault", ["binding", "stale_source", "nonrouting", "old_copper"])
def test_preflight_rejection_never_runs_native(gate, fault):
    gate["fault"] = fault
    if fault == "stale_source":
        gate["before"]["drc"]["source_sha256"] = "stale"
    elif fault in {"nonrouting", "old_copper"}:
        from pcb_weaver import completion
        if fault == "nonrouting":
            real = completion._preserved_state
            def changed(engine, project, revision):
                return {**real(engine, project, revision), "changed": revision}
            # Model an authenticated but differently placed source revision.
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(completion, "_preserved_state", changed)
                result = cleanup(gate)
            assert result["status"] == "blocked" and not gate["native_calls"]
            return
        gate["original"], gate["baseline"] = gate["revision"], gate["before"]
    result = cleanup(gate)
    assert result["status"] == "blocked", result
    assert not gate["native_calls"]
    assert "candidate_revision" not in result


@pytest.mark.parametrize("allowed", [0, 1])
def test_existing_budget_boundary_never_schedules_late_native(gate, allowed):
    calls = []
    def checkpoint():
        calls.append(True)
        return len(calls) <= allowed
    result = cleanup(gate, checkpoint=checkpoint)
    assert result["status"] == "blocked" and not gate["native_calls"]
    assert ("candidate_revision" in result) == bool(allowed)
    assert Path(result["evidence_path"]).is_file()


def test_evidence_directory_link_rejected_before_mkdir(gate, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = gate["folder"] / "repairs"
    if os.name == "nt":
        # Native PowerShell junction creation does not require symlink privileges.
        command = ("$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction -Path '"
                   + str(link).replace("'", "''") + "' -Target '"
                   + str(outside).replace("'", "''") + "' | Out-Null")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], check=True)
        assert link.is_junction()
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="link or junction"):
        cleanup(gate)
    assert not list(outside.iterdir()) and not gate["native_calls"]


@pytest.mark.parametrize("accepted", [True, False])
def test_completion_cleanup_once_after_clearance_before_assess_and_repair(setup, monkeypatch, accepted):
    engine, original, state = setup
    state.update(normalize=True, outcomes=[1], repair=True, reports={})
    order = []
    route = engine.route_revision

    def routed(*args, **kwargs):
        value = route(*args, **kwargs)
        rid = value["revision"]["id"]
        state["checks"][rid]["drc"].update(errors=2, warnings=1)
        state["reports"][rid] = {**report([("a", "b")]), "violations":
                                 [finding("v1", "clearance", "error"), finding()]}
        return value

    def clearance(project, revision, nets, region):
        order.append("clearance")
        rid = state["child"](revision, "clearance")["id"]
        state["checks"][rid] = check(1)
        state["checks"][rid]["drc"]["warnings"] = 1
        state["reports"][rid] = {**report([("a", "b")]), "violations": [finding()]}
        state["clearance_revision"] = rid
        return {"status": "improved", "revision": rid}

    def via_cleanup(e, p, revision, before, fixture, baseline, *, checkpoint):
        assert revision == state["clearance_revision"] and fixture == original
        assert before["drc"]["errors"] == before["drc"]["unconnected"] == 1
        assert checkpoint()
        order.append("via_cleanup")
        if not accepted:
            return {"status": "blocked", "revision": revision, "reason": "mock gate rejection"}
        rid = state["child"](revision, "via_cleanup")["id"]
        state["checks"][rid] = check(1)
        state["via_revision"] = rid
        return {"status": "improved", "revision": rid, "verification": engine.verify_revision(p, rid)}

    repair = engine.auto_repair_revision

    def repaired(p, revision, options, **callbacks):
        assert revision == state["via_revision"]
        order.append("repair")
        return repair(p, revision, options, **callbacks)

    monkeypatch.setattr(engine, "route_revision", routed)
    monkeypatch.setattr(engine, "repair_clearance", clearance)
    monkeypatch.setattr(engine, "auto_repair_revision", repaired)
    monkeypatch.setattr(module, "clearance_scopes", lambda *a: [
        {"nets": ["VIN"], "region": [30, 30, 32, 32], "source_items": ["v1"], "margin_mm": .8}])
    monkeypatch.setattr(module, "cleanup_dangling_vias", via_cleanup)
    result = engine.complete_revision("p", original,
        {"placement_mode": "preserve", "routing_policy": "normalize_widths"})
    assert order == (["clearance", "via_cleanup", "repair"] if accepted else ["clearance", "via_cleanup"]), result
    assert result["status"] == ("completed" if accepted else "blocked"), result
    attempt = result["attempts"][0]
    assert attempt["via_cleanup"]["status"] == ("improved" if accepted else "blocked")
    if accepted:
        assert result["candidate_assessments"][state["via_revision"]]["accepted"]
        assert attempt["via_cleaned_revision"] == state["via_revision"]
    else:
        assert not result["candidate_assessments"][state["clearance_revision"]]["accepted"]
        assert "repair_options" not in state


@pytest.mark.parametrize("mode,policy", [("preserve", "strict"), ("optimize", "normalize_widths")])
def test_completion_other_modes_never_schedule_via_cleanup(setup, monkeypatch, mode, policy):
    from pcb_weaver import completion
    engine, original, state = setup
    state.update(normalize=policy == "normalize_widths", outcomes=[0])
    real_report = completion._report
    def with_warning(e, p, revision, value):
        raw = real_report(e, p, revision, value)
        return {**raw, "violations": [finding()]} if revision != original else raw
    def forbidden(*args, **kwargs):
        pytest.fail("Via cleanup must only run in preserve + normalize_widths")
    monkeypatch.setattr(completion, "_report", with_warning)
    monkeypatch.setattr(module, "cleanup_dangling_vias", forbidden)
    result = engine.complete_revision("p", original,
        {"placement_mode": mode, "routing_policy": policy, "candidate_count": 1})
    assert result["attempts"]
    assert all("via_cleanup" not in attempt for attempt in result["attempts"])
