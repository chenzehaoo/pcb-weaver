import pytest

from pcb_weaver.controlled_neckdown import widen
from pcb_weaver.repair_geometry import _read, _board, _serialize, _field
from test_grid_route import fixture
from test_repair_geometry import segment


def proposal(tmp_path):
    source = fixture(tmp_path)
    ast = _read(source)[0]
    ast.append(segment("new", (34,30), (40,30), .3, 2))
    routed = tmp_path / "routed.kicad_pcb"
    routed.write_bytes(_serialize(ast))
    return source, routed


def test_widening_changes_only_new_copper(tmp_path):
    source, routed = proposal(tmp_path)
    original = source.read_bytes()
    result = widen(source, routed, tmp_path / "out.kicad_pcb", "VIN", .4, .2, .15)
    assert result["status"] == "proposed" and result["widened_segments"] == 1
    assert result["escape_length_mm"] == 0
    copper = {c["id"]:c["geometry"] for c in _board(_read(tmp_path / "out.kicad_pcb")[0],source=True)["copper"]}
    assert copper["new"]["width"] == .4
    assert copper["a"]["width"] == copper["b"]["width"] == .2
    assert source.read_bytes() == original


def test_changed_original_copper_is_rejected(tmp_path):
    source, routed = proposal(tmp_path)
    ast = _read(routed)[0]
    _field(_board(ast,source=True)["copper"][0]["node"],"width",1)[1] = .1
    routed.write_bytes(_serialize(ast))
    with pytest.raises(ValueError, match="existing copper"):
        widen(source, routed, tmp_path / "out.kicad_pcb", "VIN", .4, .2, .15)


def test_landing_regions_use_one_union_per_copper_layer(tmp_path,monkeypatch):
    import shapely
    from functools import reduce
    source,routed = proposal(tmp_path)
    real = shapely.union_all
    batches = []
    def batched(shapes):
        batches.append(len(shapes))
        merged = real(shapes)
        sequential = reduce(lambda a,b:a.union(b),shapes,shapely.GeometryCollection())
        assert merged.symmetric_difference(sequential).area < 1e-10
        return merged
    monkeypatch.setattr(shapely,"union_all",batched)
    result = widen(source,routed,tmp_path / "out.kicad_pcb","VIN",.4,.2,.15)
    assert result["status"] == "proposed"
    assert len(batches) == len(_board(_read(source)[0],source=True)["layers"])
    assert max(batches) > 1


def test_narrow_route_outside_pad_escape_is_rejected(tmp_path):
    source, routed = proposal(tmp_path)
    ast = _read(routed)[0]
    ast.append(segment("foreign", (34,30.36), (40,30.36), .1, 1))
    routed.write_bytes(_serialize(ast))
    with pytest.raises(ValueError, match="Unselected net"):
        widen(source, routed, tmp_path / "out.kicad_pcb", "VIN", .4, .2, .15)


@pytest.mark.parametrize("minimum,escape", [(.35,.3),(.2,.1),(.2,.4)])
def test_minimum_rules_are_not_relaxed(tmp_path, minimum, escape):
    source, routed = proposal(tmp_path)
    with pytest.raises(ValueError):
        widen(source, routed, tmp_path / "out.kicad_pcb", "VIN", .4, minimum, .15, escape=escape)


@pytest.mark.parametrize("long_wall", [False, True])
def test_explicit_bottleneck_mode_still_enforces_length_budget(tmp_path, long_wall):
    source = fixture(tmp_path)
    ast = _read(source)[0]
    ast.append(segment("wall", (35.5,30.36), (41 if long_wall else 36.5,30.36), .1, 1))
    source.write_bytes(_serialize(ast))
    ast.append(segment("new", (34,30), (42,30), .3, 2))
    routed = tmp_path / "routed.kicad_pcb"
    routed.write_bytes(_serialize(ast))
    output = tmp_path / "out.kicad_pcb"
    result = widen(source, routed, output, "VIN", .4, .2, .15, allow_bottlenecks=True)
    if long_wall:
        assert result["status"] == "blocked" and not output.exists()
        assert result["escape_length_mm"] > 4
    else:
        assert result["status"] == "proposed"
        assert result["allow_bottlenecks"] and result["maximum_escape_length_mm"] == 4
        assert 0 < result["escape_length_mm"] < 2


@pytest.mark.parametrize("long_wall", [False, True])
def test_split_restoration_bounds_short_escapes_and_rejects_long_ones(tmp_path, monkeypatch, long_wall):
    from pcb_weaver import controlled_neckdown
    source = fixture(tmp_path)
    ast = _read(source)[0]
    ast.append(segment("wall", (34,30.36), (40 if long_wall else 35,30.36), .1, 1))
    source.write_bytes(_serialize(ast))
    original = source.read_bytes()
    ast.append(segment("new", (34,30), (40,30), .3, 2))
    routed = tmp_path / "routed.kicad_pcb"
    routed.write_bytes(_serialize(ast))
    real = controlled_neckdown.read_board

    def pads(path):
        board = real(path)
        board["footprints"] = [{"pads":[{"net":"VIN","x":37 if long_wall else 34.5,"y":30,
            "shape":"rect","size":[10 if long_wall else 1,1],"rotation":0,"layers":["F.Cu"]}]}]
        return board

    monkeypatch.setattr(controlled_neckdown,"read_board",pads)
    output = tmp_path / "out.kicad_pcb"
    result = widen(source, routed, output, "VIN", .4, .2, .15)
    assert source.read_bytes() == original
    if long_wall:
        assert result["status"] == "blocked" and not output.exists()
        assert result["escape_length_mm"] > 4
    else:
        assert result["status"] == "proposed"
        assert 0 < result["escape_length_mm"] < 2
        assert result["widened_segments"] == 1
