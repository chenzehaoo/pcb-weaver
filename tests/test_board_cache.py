"""Bounded parsing reuse must never share mutable geometry or hide file edits."""
import pytest
from pcb_weaver import board
from pcb_weaver.repair_geometry import _read,_serialize


@pytest.fixture(autouse=True)
def empty_cache():
    board._cached_ast.cache_clear()
    yield
    board._cached_ast.cache_clear()


def test_readers_share_parsing_but_not_mutable_ast(tmp_path,monkeypatch):
    path = tmp_path / "board.kicad_pcb"
    path.write_text('(kicad_pcb (net 1 "A"))',encoding="ascii")
    real,calls = board.sexpdata.loads,[]
    def counted(*args,**kwargs):
        calls.append(1)
        return real(*args,**kwargs)
    monkeypatch.setattr(board.sexpdata,"loads",counted)
    first = board._load(path)
    first[1][2] = "mutated"
    first[0]._val = "mutated-symbol"
    second,sha = _read(path)
    assert str(second[0]) == "kicad_pcb"
    assert second[1][2] == "A" and len(calls) == 1
    path.write_text('(kicad_pcb (net 1 "B"))',encoding="ascii")
    third,new_sha = _read(path)
    assert third[1][2] == "B" and new_sha != sha and len(calls) == 2
    path.write_text('(kicad_pcb (',encoding="ascii")
    with pytest.raises(ValueError,match="Invalid board"):
        _read(path)


def test_serialization_primes_independent_read_cache(tmp_path,monkeypatch):
    ast = board._parse_ast('(kicad_pcb (net 1 "A"))')
    payload = _serialize(ast)
    path = tmp_path / "new.kicad_pcb"
    path.write_bytes(payload)
    monkeypatch.setattr(board.sexpdata,"loads",lambda *args,**kwargs:pytest.fail("Reparsed validated bytes"))
    copied,_ = _read(path)
    copied[1][2] = "changed"
    assert _read(path)[0] == ast


def test_cache_is_bounded_and_large_inputs_bypass_it(monkeypatch):
    for i in range(5):
        board._parse_ast(f'(kicad_pcb (net 1 "N{i}"))')
    assert board._cached_ast.cache_info().currsize == 2
    board._cached_ast.cache_clear()
    monkeypatch.setattr(board,"_AST_CACHE_CHAR_LIMIT",10)
    text = '(kicad_pcb (net 1 "large"))'
    assert board._parse_ast(text) == board._parse_ast(text)
    assert board._cached_ast.cache_info().currsize == 0
