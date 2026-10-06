from contextlib import contextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("accepted", [False, True])
def test_native_result_is_persisted_and_reported_outside_project_lock(tmp_path, monkeypatch, accepted):
    script = Path(__file__).resolve().parents[1] / "scripts" / "accept_joint_repair.py"
    spec = importlib.util.spec_from_file_location("joint_acceptance_runner", script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    parent, child = tmp_path / "parent", tmp_path / "child"
    for folder in (parent, child):
        (folder / "design").mkdir(parents=True)
    source = parent / "design" / "board.kicad_pcb"
    source.write_bytes(b"board")
    state = {"locked":False, "reported":False}

    @contextmanager
    def lock(project):
        assert not state["locked"], "Project lock is not reentrant"
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    engine = SimpleNamespace(
        store=SimpleNamespace(lock=lock),
        _verified=lambda p,r: ({"board":"board.kicad_pcb"}, child if r == "child" else parent),
        _verify=lambda p,r: {"verification_id":r},
        _new=lambda p,r: ("child",child,{}),
        _seal=lambda *a,**kw: {},
        toolchain=SimpleNamespace(inspect_board=lambda p: {"nets":{"/AN5":{},"/AN6":{}}}),
    )
    monkeypatch.setattr(runner, "PARENT_SHA", runner.digest(source))
    monkeypatch.setattr(runner, "EngineeringService", lambda *a:engine)
    monkeypatch.setattr(runner, "load_runtime", lambda *a:(tmp_path,{}))
    monkeypatch.setattr(runner, "quality_issues", lambda c:[])
    monkeypatch.setattr(runner, "_net_index", lambda p:{"pad":"/AN5"})
    monkeypatch.setattr(runner, "_report", lambda *a:{"unconnected_items":[{"items":[{"uuid":"pad"}]}]})
    monkeypatch.setattr(runner, "_erc_report", lambda *a:{})
    monkeypatch.setattr(runner, "_erc_violations", lambda *a:set())
    monkeypatch.setattr(runner, "compare_checks", lambda *a:{"accepted":accepted,
        "reasons":[] if accepted else ["New native warning"], "after_by_net":{"GND":4,"+3.3V":3}})

    def search(source, work, *args):
        assert state["locked"]
        state["work"] = work
        work.mkdir(parents=True)
        (work / "merged.kicad_pcb").write_bytes(b"candidate")
        return {"status":"proposed", "steps":[], "output_sha256":runner.digest(work / "merged.kicad_pcb")}

    def report(engine, project, revision):
        assert not state["locked"]
        with lock(project):
            result = runner.read_json(state["work"] / "result.json")
            assert result["comparison"]["accepted"] == accepted
            assert result["status"] == ("improved" if accepted else "blocked")
            state["reported"] = True

    monkeypatch.setattr(runner, "propose_joint", search)
    monkeypatch.setattr(runner.catalog, "generate_report", report)
    monkeypatch.setattr("sys.argv", [str(script),"--root",str(tmp_path)])
    runner.main()
    assert state["reported"] and not state["locked"]
