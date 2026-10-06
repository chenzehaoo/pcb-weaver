import pytest

from pcb_weaver.joint_escape import relocate
from pcb_weaver.repair_geometry import _read, _board, _serialize, _field
from test_via_escape import board
from test_repair_geometry import segment


def test_joint_reservation_preserves_remote_copper_and_inputs(tmp_path):
    source = board(tmp_path)
    original = source.read_bytes()
    output = tmp_path / "out.kicad_pcb"
    result = relocate(source, output, [[37,32]], [37,31.5], diameter=.2, net="VIN")
    assert result["status"] == "proposed" and result["vertices_after"][0][1] > 32.1
    assert result["changed_ids"] == ["lead","move"]
    assert result["requires_native_verification"] and not result["manufacturing_authorized"]
    old = {c["id"]:c for c in _board(_read(source)[0],source=True)["copper"]}
    new = {c["id"]:c for c in _board(_read(output)[0],source=True)["copper"]}
    for key in ["a","b"]:
        assert old[key]["node"] == new[key]["node"]
    assert new["move"]["geometry"]["drill"] == old["move"]["geometry"]["drill"]
    assert new["lead"]["geometry"]["width"] == .4
    assert new["lead"]["geometry"]["end"] == [38,32]
    assert source.read_bytes() == original


@pytest.mark.parametrize("reverse", [False, True])
def test_long_incident_trace_only_adjusts_split_local_part(tmp_path, reverse):
    source = board(tmp_path)
    ast = _read(source)[0]
    lead = next(c for c in _board(ast,source=True)["copper"] if c["id"] == "lead")
    _field(lead["node"],"end",2)[1:] = [37,40]
    if reverse:
        _field(lead["node"],"start",2)[1:] = [37,40]
        _field(lead["node"],"end",2)[1:] = [37,32]
    source.write_bytes(_serialize(ast))
    output = tmp_path / "out.kicad_pcb"
    result = relocate(source, output, [[37,32]], [37,31.5], diameter=.2, net="VIN")
    assert result["status"] == "proposed" and len(result["split_near_ids"]) == 1
    copper = {c["id"]:c["geometry"] for c in _board(_read(output)[0],source=True)["copper"]}
    assert copper["lead"]["start"] == [37,34]
    assert copper["lead"]["end"] == [37,40]
    assert copper[result["split_near_ids"][0]]["end"] == [37,34]


def test_reversed_node_endpoints_are_not_canonical_geometry_fields(tmp_path):
    source = board(tmp_path)
    ast = _read(source)[0]
    lead = next(c for c in _board(ast,source=True)["copper"] if c["id"] == "lead")
    _field(lead["node"],"start",2)[1:] = [38,32]
    _field(lead["node"],"end",2)[1:] = [37,32]
    source.write_bytes(_serialize(ast))
    output = tmp_path / "out.kicad_pcb"
    result = relocate(source, output, [[37,32]], [37,31.5], diameter=.2, net="VIN")
    new = next(c for c in _board(_read(output)[0],source=True)["copper"] if c["id"] == "lead")
    assert _field(new["node"],"start",2)[1:] == [38,32]
    assert _field(new["node"],"end",2)[1:] == result["vertices_after"][0]


def test_reserved_via_must_clear_fixed_copper_on_every_layer(tmp_path):
    source = board(tmp_path)
    ast = _read(source)[0]
    obstacle = segment("inner", (36,31.5),(38,31.5),.2,1)
    _field(obstacle,"layer",1)[1] = "B.Cu"
    ast.append(obstacle)
    source.write_bytes(_serialize(ast))
    output = tmp_path / "out.kicad_pcb"
    result = relocate(source, output, [[37,32]], [37,31.5], diameter=.2, net="VIN")
    assert result["status"] == "blocked" and not output.exists()


def test_locked_selected_vertex_rejected(tmp_path):
    with pytest.raises(ValueError, match="Locked"):
        relocate(board(tmp_path,"(locked yes)"), tmp_path / "out.kicad_pcb", [[37,32]], [37,31.5])


@pytest.mark.parametrize("vertices", [[[37,32],[37,32]],[[999,999]],[]])
def test_ambiguous_or_missing_vertices_rejected(tmp_path, vertices):
    with pytest.raises(ValueError):
        relocate(board(tmp_path), tmp_path / "out.kicad_pcb", vertices, [37,31.5])


def test_failed_optimizer_does_not_write_candidate(tmp_path):
    output = tmp_path / "out.kicad_pcb"
    result = relocate(board(tmp_path), output, [[37,32]], [37,32], max_move=.1, net="VIN")
    assert result["status"] == "blocked" and not output.exists()
