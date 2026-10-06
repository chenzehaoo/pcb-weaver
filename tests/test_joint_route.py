from copy import deepcopy

import pytest

from pcb_weaver import joint_route
from pcb_weaver.repair_geometry import _read, _board
from pcb_weaver.storage import read_json
from test_grid_route import fixture, FINDING, RULES


def request():
    return {"net": "VIN", "region": [30,25,45,35], "finding": deepcopy(FINDING), "rules": RULES}


def test_joint_additive_candidate_is_not_manufacturing_authorization(tmp_path):
    source = fixture(tmp_path)
    original = source.read_bytes()
    result = joint_route.propose_joint(source, tmp_path / "joint", [request()], [30,25,45,35], [], .1)
    assert result["status"] == "proposed"
    assert not result["manufacturing_authorized"]
    assert result["requires_native_verification"]
    assert source.read_bytes() == original
    assert result["patch"]["preservation"]["retained_copper_ast_preserved"]


def test_failed_rework_only_removes_explicit_id_in_working_copy(tmp_path, monkeypatch):
    source = fixture(tmp_path)
    original = source.read_bytes()
    folder = tmp_path / "joint"

    def blocked(working, *args, **kwargs):
        ids = {c["id"] for c in _board(_read(working)[0], source=True)["copper"]}
        assert "a" not in ids and "b" in ids
        return {"status":"blocked", "reason":"No path"}

    monkeypatch.setattr(joint_route, "propose", blocked)
    result = joint_route.propose_joint(source, folder, [request()], [30,25,45,35], ["a"])
    assert result["status"] == "blocked"
    assert source.read_bytes() == original
    assert not (folder / "merged.kicad_pcb").exists()
    assert read_json(folder / "scope.json")["remove_ids"] == ["a"]


def test_invalid_and_outside_scope_rejected_before_search(tmp_path):
    source = fixture(tmp_path)
    with pytest.raises(ValueError, match="distinct"):
        joint_route.propose_joint(source, tmp_path / "joint", [request(),request()], [30,25,45,35], [])
    with pytest.raises(ValueError, match="exceed"):
        joint_route.propose_joint(source, tmp_path / "joint", [request()], [31,25,45,35], [])
    assert not (tmp_path / "joint").exists()


def test_scope_tampering_prevents_merge(tmp_path, monkeypatch):
    source = fixture(tmp_path)
    real = joint_route.propose
    folder = tmp_path / "joint"

    def changed(*args, **kwargs):
        result = real(*args, **kwargs)
        (folder / "scope.json").write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(joint_route, "propose", changed)
    with pytest.raises(ValueError, match="scope changed"):
        joint_route.propose_joint(source, folder, [request()], [30,25,45,35], [], .1)
    assert not (folder / "merged.kicad_pcb").exists()
