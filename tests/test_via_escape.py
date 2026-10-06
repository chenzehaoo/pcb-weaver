from copy import deepcopy

import pytest

from pcb_weaver.via_escape import relocate
from pcb_weaver.repair_geometry import _read, _board, _serialize, _field
from test_grid_route import fixture
from test_repair_geometry import via, segment


def board(tmp_path, extra=""):
    source = fixture(tmp_path)
    ast = _read(source)[0]
    ast.extend([via("move", (37,32), extra=extra), segment("lead", (37,32), (38,32))])
    source.write_bytes(_serialize(ast))
    return source


def test_relocation_preserves_width_drill_far_end_and_other_items(tmp_path):
    source = board(tmp_path)
    before = source.read_bytes()
    output = tmp_path / "out.kicad_pcb"
    result = relocate(source, output, "move", [[36,31.5],[39,31.5]])
    assert result["status"] == "proposed"
    assert result["after"][1] > 32.1
    assert result["changed_ids"] == ["lead", "move"]
    assert result["requires_native_verification"] and not result["manufacturing_authorized"]
    old = {c["id"]:c for c in _board(_read(source)[0], source=True)["copper"]}
    new = {c["id"]:c for c in _board(_read(output)[0], source=True)["copper"]}
    for key in old:
        expected = deepcopy(old[key]["node"])
        if key == "move":
            _field(expected,"at",2)[1:] = result["after"]
        elif key == "lead":
            _field(expected,"start",2)[1:] = result["after"]
        assert expected == new[key]["node"]
    assert source.read_bytes() == before


def test_locked_via_rejected(tmp_path):
    with pytest.raises(ValueError, match="locked"):
        relocate(board(tmp_path,"(locked yes)"), tmp_path / "out.kicad_pcb", "move", [[36,31.5],[39,31.5]])


def test_impossible_corridor_does_not_emit_output(tmp_path):
    output = tmp_path / "out.kicad_pcb"
    result = relocate(board(tmp_path), output, "move", [[35,32],[40,32]], max_move=.1)
    assert result["status"] == "blocked" and not output.exists()


@pytest.mark.parametrize("max_move,clearance", [(0,.15),(.6,.15),(.3,0),(.3,float("nan"))])
def test_invalid_scope_rejected(tmp_path, max_move, clearance):
    with pytest.raises(ValueError):
        relocate(board(tmp_path), tmp_path / "out.kicad_pcb", "move", [[36,31.5],[39,31.5]],
                 max_move=max_move, clearance=clearance)
