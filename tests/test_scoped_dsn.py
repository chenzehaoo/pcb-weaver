
import pytest

from pcb_weaver.scoped_dsn import prepare
from pcb_weaver.repair_dsn import _parse, _one, _children


DSN = '''(pcb "fixture.dsn" (parser (string_quote "))
 (structure (rule (width 200) (clearance 150)))
 (network (net A (pins U1-1 J1-1)) (net B (pins U1-2 J1-2))
 (class Default A B (circuit (use_via VIA)) (rule (width 200) (clearance 150)))))'''


def test_class_split_preserves_rules_and_pin_assignments(tmp_path):
    source, output = tmp_path / "in.dsn", tmp_path / "out.dsn"
    source.write_text(DSN)
    proof = prepare(source, output, ["A"])
    old, new = _parse(DSN), _parse(output.read_text())
    original = _children(_one(old, "network"), "class")[0]
    groups = _children(_one(new, "network"), "class")
    assert len(groups) == 2
    assert [str(g[1]) for g in groups] == ["Default", "pcb_weaver_target_0"]
    assert [str(n) for n in groups[0][2:] if not isinstance(n, list)] == ["B"]
    assert [str(n) for n in groups[1][2:] if not isinstance(n, list)] == ["A"]
    assert [n for n in groups[1][2:] if isinstance(n, list)] == [n for n in original[2:] if isinstance(n, list)]
    assert _children(_one(new, "network"), "net") == _children(_one(old, "network"), "net")
    assert source.read_text() == DSN and proof["ignore_classes"] == ["Default"]
    assert proof["physical_rules_preserved"] and not proof["runtime_scope_verified"]
    with pytest.raises(ValueError):
        prepare(source, output, ["A"])


@pytest.mark.parametrize("mutation,nets", [
    (lambda s: s, []), (lambda s: s, ["C"]), (lambda s: s, ["A", "A"]),
    (lambda s: s.replace("Default A B", "Default A A B"), ["A"]),
    (lambda s: s.replace("Default A B", "Default A"), ["A"]),
    (lambda s: s.replace("Default A B", '"Default,Other" A B'), ["A"]),
    (lambda s: s.replace("Default A B", "pcb_weaver_target_0 A B"), ["A"]),
    (lambda s: s.replace("(circuit", "(unknown"), ["A"]),
])
def test_ambiguous_scope_is_rejected_without_output(tmp_path, mutation, nets):
    source, output = tmp_path / "in.dsn", tmp_path / "out.dsn"
    source.write_text(mutation(DSN))
    with pytest.raises(ValueError):
        prepare(source, output, nets)
    assert not output.exists()


def test_unverified_runtime_scope_is_blocked_before_native_execution(tmp_path, monkeypatch):
    from test_toolchain import unified_test_toolchain
    tc = unified_test_toolchain(monkeypatch)
    source, output = tmp_path / "in.dsn", tmp_path / "out.ses"
    source.write_text(DSN)
    def execute(*args, **kwargs):
        pytest.fail("Unverified scope must not execute a native process")
    monkeypatch.setattr(tc, "_execute", execute)
    result = tc.route(source, output, passes=1, nets=["A"])
    assert result["status"] == "blocked" and "not runtime-verified" in result["reason"]
    assert not output.exists()
