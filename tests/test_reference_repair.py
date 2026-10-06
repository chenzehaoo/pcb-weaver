from copy import deepcopy
import pytest

from pcb_weaver.reference_repair import plans,propose,touches_findings
from pcb_weaver.jobs import JobRequest,_request_payload
from pcb_weaver.repair_geometry import _read,_board,_serialize
from pcb_weaver.storage import digest
from test_grid_route import fixture,FINDING
from test_repair_geometry import segment


def boards(tmp_path):
    source = fixture(tmp_path)
    reference = tmp_path / "reference.kicad_pcb"
    ast = _read(source)[0]
    ast.append(segment("bridge",(34,30),(40,30),.2,2))
    reference.write_bytes(_serialize(ast))
    return source,reference


def test_reference_local_patch_preserves_source_and_has_no_release(tmp_path):
    source,reference = boards(tmp_path)
    before = digest(source)
    candidates = plans(source,reference,["VIN"])
    assert len(candidates) == 1
    assert touches_findings(source,candidates[0],[FINDING])
    work = tmp_path / "work"
    work.mkdir()
    result = propose(source,reference,work,candidates[0])
    assert result["requires_native_verification"] and not result["manufacturing_authorized"]
    assert digest(source) == before
    assert result["patch"]["removed"] == 0 and result["patch"]["added"] == 1
    assert result["patch"]["preservation"]["noncopper_ast_preserved"]


def test_stale_reference_and_oversize_scope_fail_closed(tmp_path):
    source,reference = boards(tmp_path)
    plan = plans(source,reference,["VIN"])[0]
    assert plans(source,reference,["VIN"],maximum_area=.01) == []
    plan["reference_sha256"] = "stale"
    with pytest.raises(ValueError,match="input changed"):
        propose(source,reference,tmp_path,plan)


def test_unrelated_reference_not_used_as_whole_board_replacement(tmp_path):
    source,reference = boards(tmp_path)
    ast = _read(reference)[0]
    ast = [n for n in ast if str(n[0]) != "segment"]
    ast.append(segment("elsewhere",(45,30),(50,30),.2,2))
    reference.write_bytes(_serialize(ast))
    with pytest.raises(ValueError,match="insufficient unchanged"):
        plans(source,reference,["VIN"])


@pytest.mark.parametrize("extra", [{},{"reference_project":"ref"},
    {"reference_project":"ref","reference_revision":"r","release":True},
    {"reference_project":"../escape","reference_revision":"r"}])
def test_reference_request_requires_explicit_immutable_reference(extra):
    with pytest.raises(ValueError):
        JobRequest(project="board",revision="r",operation="reference_repair",**extra)


def test_legacy_request_hash_payload_is_not_changed():
    request = JobRequest(project="board",revision="r",operation="verify")
    assert "reference_project" not in _request_payload(request)
    with pytest.raises(ValueError):
        JobRequest(project="board",revision="r",operation="verify",reference_project="ref",reference_revision="r")
    request = JobRequest(project="board",revision="r",operation="reference_repair",reference_project="ref",reference_revision="good")
    assert _request_payload(request)["reference_revision"] == "good"


@pytest.mark.parametrize("status", ["repaired", "blocked"])
def test_reference_queue_persists_progress_without_releasing(tmp_path, monkeypatch, status):
    from pcb_weaver import catalog, server
    from pcb_weaver.jobs import JobQueue
    from test_repair_protocol import FakeEngine
    engine = FakeEngine(tmp_path)
    calls = []

    def repair(project, revision, reference_project, reference_revision, *, checkpoint, progress):
        checkpoint()
        calls.append((project, revision, reference_project, reference_revision))
        result = {"status": status, "revision": "r-best", "stage": "finished", "attempts": [],
                  "after_unconnected": 0 if status == "repaired" else 1, "manufacturing_authorized": False}
        progress(result)
        return result

    monkeypatch.setattr(engine, "reference_repair_revision", repair, raising=False)
    monkeypatch.setattr(catalog, "generate_report", lambda *args: {"status": "generated"})
    queue = JobQueue(tmp_path, engine=engine)
    monkeypatch.setattr(server, "job_queue", lambda: queue)
    submitted = server.submit_pcb_reference_repair("board", "r-source", "reference", "r-ref")
    assert not calls and submitted["status"] == "queued"
    assert queue.run_once()
    job = JobQueue(tmp_path, engine=engine).get(submitted["id"])
    assert job["status"] == ("completed" if status == "repaired" else "blocked")
    assert job["result"]["revision"] == "r-best"
    assert "release" not in job["result"]["steps"]
    assert job["result"]["steps"]["reference_repair"]["status"] == status
    assert calls == [("board", "r-source", "reference", "r-ref")]


@pytest.mark.parametrize("mismatch", ["constraints", "placement", "rules", "reference_drc"])
def test_reference_incompatibility_stops_before_candidate_creation(tmp_path, monkeypatch, mismatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from pcb_weaver import reference_repair as module
    from pcb_weaver import repair as gates
    from pcb_weaver.models import Constraints
    from pcb_weaver.storage import write_json
    source, reference = boards(tmp_path)
    roots = [tmp_path / "source-rev", tmp_path / "reference-rev"]
    for root, board in zip(roots, (source, reference)):
        (root / "design").mkdir(parents=True)
        (root / "design" / "board.kicad_pcb").write_bytes(board.read_bytes())
        write_json(root / "constraints.json", Constraints().model_dump())
        write_json(root / "design" / "board.kicad_pro", {"rules": "unchanged"})
    if mismatch == "constraints":
        value = Constraints().model_dump()
        value["critical_nets"] = ["VIN"]
        write_json(roots[1] / "constraints.json", value)
    if mismatch == "rules":
        write_json(roots[1] / "design" / "board.kicad_pro", {"rules": "different"})
    signatures = iter(["same", "different" if mismatch == "placement" else "same"])
    check = {"status": "blocked", "drc": {"unconnected": 1}}
    engine = SimpleNamespace(
        store=SimpleNamespace(lock=lambda project: nullcontext(), event=lambda *args: None),
        _verified=lambda project, revision: ({"board": "board.kicad_pcb", "digest": project}, roots[project == "ref"]),
        _electrical_signature=lambda board: next(signatures),
        _verify=lambda *args: check,
        _new=lambda *args: pytest.fail("Incompatible reference created a candidate"))
    monkeypatch.setattr(gates, "quality_issues", lambda check: [])
    monkeypatch.setattr(gates, "_report", lambda *args: {})
    monkeypatch.setattr(gates, "_report_issues", lambda *args: [])
    result = module.repair(engine, "source", "r", "ref", "r")
    assert result["status"] == "blocked" and result["attempts"] == []
    assert not result["manufacturing_authorized"]
