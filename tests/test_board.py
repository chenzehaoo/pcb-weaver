from copy import deepcopy
import json
from pathlib import Path

import pytest
import sexpdata

from pcb_weaver.board import compare_boards, read_board, write_placements


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "two-layer" / "two-layer.kicad_pcb"


def tag(node):
    return str(node[0]) if isinstance(node, list) and node else ""


def children(node, name):
    return [item for item in node if tag(item) == name]


def fixture_ast():
    return sexpdata.loads(EXAMPLE.read_text(encoding="utf-8"))


def save_ast(tmp_path, ast, name="test.kicad_pcb"):
    path = tmp_path / name
    path.write_text(sexpdata.dumps(ast), encoding="utf-8")
    return path


def test_original_example_contract():
    board = read_board(EXAMPLE)
    assert len(board["footprints"]) == 4
    assert board["outline"] == {"bounds": [20, 20, 70, 55], "supported": True}
    assert board["unsupported"] == []
    assert board["copper_layers"] == ["F.Cu", "B.Cu"]
    assert board["tracks"] == board["vias"] == 0
    assert board["track_items"] == board["via_items"] == []
    assert {net["name"] for net in board["nets"]} == {"VIN", "FILTERED", "GND"}
    assert board["footprints"][0]["locked"]
    assert board["footprints"][0]["pads"][0]["x"] == pytest.approx(23.73)


def test_ast_preserved_translation_and_source_immutable(tmp_path):
    ast = fixture_ast()
    fp = children(ast, "footprint")[2]
    fp.append(sexpdata.loads('(property "custom-note" "retain (quotes) and units")'))
    source = save_ast(tmp_path, ast)
    original = source.read_bytes()
    output = tmp_path / "moved.kicad_pcb"
    written = write_placements(source, output, [{"reference": "R1", "x": 44, "y": 32, "rotation": 0}])
    assert source.read_bytes() == original
    reloaded = sexpdata.loads(output.read_text())
    updated = children(reloaded, "footprint")[2]
    children(fp, "at")[0][1:3] = [44, 32]
    assert reloaded == ast
    assert updated[-1] == fp[-1]
    assert written == read_board(output)
    delta = compare_boards(source, output)
    assert [item["reference"] for item in delta["moved"]] == ["R1"]
    assert not delta["connectivity_changed"]
    assert delta["moved"][0]["distance_mm"] == pytest.approx(32 ** 0.5)


@pytest.mark.parametrize("x,expected", [
    (39.04550508, 39.045505),
    (39.0455058, 39.045506),
    (-39.04550508, -39.045505),
    (39.045505, 39.045505),
])
def test_written_placement_uses_native_nanometre_grid(tmp_path, x, expected):
    source_bytes = EXAMPLE.read_bytes()
    output = tmp_path / "quantized.kicad_pcb"
    written = write_placements(EXAMPLE, output, [{"reference": "R1", "x": x, "y": 28.12345678}])
    fp = next(item for item in written["footprints"] if item["reference"] == "R1")
    assert fp["x"] == expected
    assert fp["y"] == 28.123457
    assert written == read_board(output)
    assert EXAMPLE.read_bytes() == source_bytes
    assert not compare_boards(EXAMPLE, output)["connectivity_changed"]


@pytest.mark.parametrize("placement,match", [
    ({"reference": "J1", "x": 26, "y": 35}, "Locked"),
    ({"reference": "MISSING", "x": 26, "y": 35}, "Unknown"),
    ({"reference": "R1", "x": 40, "y": 28, "rotation": 90}, "Rotation"),
    ({"reference": "R1", "x": float("nan"), "y": 28}, "Non-finite"),
    ({"reference": "R1", "x": 40, "y": 28, "layer": "B.Cu"}, "Unsupported"),
])
def test_reject_invalid_placements(tmp_path, placement, match):
    output = tmp_path / "out.kicad_pcb"
    with pytest.raises(ValueError, match=match):
        write_placements(EXAMPLE, output, [placement])
    assert not output.exists()


def test_no_source_or_existing_destination_overwrite(tmp_path):
    with pytest.raises(ValueError, match="new copy"):
        write_placements(EXAMPLE, EXAMPLE, [])
    dest = tmp_path / "exists.kicad_pcb"
    dest.write_text("existing")
    with pytest.raises(ValueError, match="new copy"):
        write_placements(EXAMPLE, dest, [])
    assert dest.read_text() == "existing"


def test_tracks_vias_are_real_and_block_movement(tmp_path):
    ast = fixture_ast()
    ast.append(sexpdata.loads('(segment (start 25 35) (end 28 39) (width 0.24) (layer "F.Cu") (net 2))'))
    ast.append(sexpdata.loads('(via (at 28 39) (size 0.6) (drill 0.2) (layers "F.Cu" "B.Cu") (net 2))'))
    source = save_ast(tmp_path, ast)
    board = read_board(source)
    assert board["tracks"] == board["vias"] == 1
    assert board["routed_length_mm"] == 5
    assert board["track_items"][0] == {"net": "VIN", "width_mm": 0.24, "start": [25, 35],
                                       "end": [28, 39], "layer": "F.Cu", "length_mm": 5}
    assert board["via_items"][0]["net"] == "VIN"
    assert board["via_items"][0]["drill_mm"] == 0.2
    with pytest.raises(ValueError, match="Routed"):
        write_placements(source, tmp_path / "out.kicad_pcb", [{"reference": "R1", "x": 41, "y": 28}])
    assert write_placements(source, tmp_path / "copy.kicad_pcb", [])["tracks"] == 1


def test_duplicate_reference_rejected(tmp_path):
    ast = fixture_ast()
    ast.append(deepcopy(children(ast, "footprint")[0]))
    with pytest.raises(ValueError, match="Duplicate"):
        read_board(save_ast(tmp_path, ast))


def test_empty_net_does_not_join_pads(tmp_path):
    ast = fixture_ast()
    for fp in children(ast, "footprint"):
        for pad in children(fp, "pad"):
            children(pad, "net")[0][1:] = [0, ""]
    assert read_board(save_ast(tmp_path, ast))["nets"] == []


def test_eco_uses_pad_identity_and_names_not_codes(tmp_path):
    ast = fixture_ast()
    for node in children(ast, "net"):
        node[1] += 10
    for fp in children(ast, "footprint"):
        for pad in children(fp, "pad"):
            children(pad, "net")[0][1] += 10
    renumbered = save_ast(tmp_path, ast)
    assert not compare_boards(EXAMPLE, renumbered)["connectivity_changed"]
    resistor = children(ast, "footprint")[2]
    pads = children(resistor, "pad")
    pads[0][-1], pads[1][-1] = pads[1][-1], pads[0][-1]
    swapped = save_ast(tmp_path, ast, "swapped.kicad_pcb")
    delta = compare_boards(EXAMPLE, swapped)
    assert delta["connectivity_changed"]
    assert len(delta["connection_changes"]) == 2
    assert not delta["moved"]


def test_rotation_transforms_pad_centres_counterclockwise_on_screen(tmp_path):
    ast = fixture_ast()
    fp = children(ast, "footprint")[2]
    children(fp, "at")[0].append(90)
    for pad in children(fp, "pad"):
        children(pad, "at")[0].append(90)
    board = read_board(save_ast(tmp_path, ast))
    resistor = board["footprints"][2]
    assert resistor["pads"][0]["x"] == pytest.approx(40)
    assert resistor["pads"][0]["y"] == pytest.approx(29.2)
    assert resistor["bounds"] == pytest.approx([38.975, 25.975, 41.025, 30.025])


@pytest.mark.parametrize("addition", [
    '(gr_circle (center 40 35) (end 42 35) (layer "Edge.Cuts"))',
    '(gr_arc (start 30 20) (mid 35 15) (end 40 20) (layer "Edge.Cuts"))',
    '(zone (net 1) (net_name "GND") (layer "F.Cu"))',
    '(gr_line (start 20 22) (end 40 22) (layer "F.Cu"))',
])
def test_unsupported_geometry_never_silently_certified(tmp_path, addition):
    ast = fixture_ast()
    ast.append(sexpdata.loads(addition))
    source = save_ast(tmp_path, ast)
    assert read_board(source)["unsupported"]
    with pytest.raises(ValueError, match="Unsupported geometry"):
        write_placements(source, tmp_path / "out.kicad_pcb", [{"reference": "R1", "x": 41, "y": 28}])


def test_outline_four_edges_requires_closed_rectangle(tmp_path):
    ast = fixture_ast()
    ast.remove(children(ast, "gr_rect")[0])
    for x1, y1, x2, y2 in [(20, 20, 70, 20), (70, 20, 70, 55), (70, 55, 20, 55), (20, 55, 20, 20)]:
        ast.append(sexpdata.loads(f'(gr_line (start {x1} {y1}) (end {x2} {y2}) (layer "Edge.Cuts"))'))
    assert read_board(save_ast(tmp_path, ast))["outline"]["supported"]
    children(ast[-1], "end")[0][2] = 21
    assert not read_board(save_ast(tmp_path, ast, "open.kicad_pcb"))["outline"]["supported"]


def test_inconsistent_backside_metadata_and_custom_pads_report_unknown(tmp_path):
    ast = fixture_ast()
    fp = children(ast, "footprint")[2]
    children(fp, "layer")[0][1] = "B.Cu"
    children(fp, "pad")[0][3] = sexpdata.Symbol("custom")
    unsupported = read_board(save_ast(tmp_path, ast))["unsupported"]
    assert any("side disagrees" in item for item in unsupported)
    assert any("pad shape" in item for item in unsupported)


def test_repeated_electrical_pad_identity_must_have_same_net(tmp_path):
    ast = fixture_ast()
    fp = children(ast, "footprint")[2]
    original = children(fp, "pad")[0]
    duplicate = deepcopy(original)
    fp.append(duplicate)
    same = save_ast(tmp_path, ast)
    assert not read_board(same)["unsupported"]
    children(duplicate, "net")[0][1:] = [1, "GND"]
    conflict = save_ast(tmp_path, ast, "conflict.kicad_pcb")
    assert any("ambiguous" in u for u in read_board(conflict)["unsupported"])
    assert compare_boards(same, conflict)["ambiguous_pad_identities"] == ["R1.1"]


def test_missing_courtyard_is_explicit_unknown(tmp_path):
    ast = fixture_ast()
    fp = children(ast, "footprint")[2]
    fp.remove(children(fp, "fp_rect")[0])
    assert any("courtyard" in u for u in read_board(save_ast(tmp_path, ast))["unsupported"])


def test_net_code_name_conflict_rejected(tmp_path):
    ast = fixture_ast()
    pad = children(children(ast, "footprint")[0], "pad")[0]
    children(pad, "net")[0][2] = "GND"
    with pytest.raises(ValueError, match="mismatch"):
        read_board(save_ast(tmp_path, ast))


def test_offset_drill_cannot_escape_conservative_geometry_check(tmp_path):
    ast = fixture_ast()
    pad = children(children(ast, "footprint")[0], "pad")[0]
    children(pad, "drill")[0].append(sexpdata.loads('(offset 4 0)'))
    assert any("offset drill" in u for u in read_board(save_ast(tmp_path, ast))["unsupported"])


def test_examples_share_design_but_preserve_different_critical_net_intent():
    original = EXAMPLE.parent
    routing = original.parent / "routing-demo"
    for source in original.rglob("*"):
        if source.is_file() and source.name != "constraints.json":
            assert source.read_bytes() == (routing / source.relative_to(original)).read_bytes()
    first = json.loads((original / "constraints.json").read_text())
    second = json.loads((routing / "constraints.json").read_text())
    assert first["critical_nets"] == ["FILTERED"]
    assert second["critical_nets"] == []
    first["critical_nets"] = []
    assert first == second


def test_schematic_instances_values_footprints_and_paths_match_board():
    schematic = sexpdata.loads(EXAMPLE.with_suffix(".kicad_sch").read_text())
    root_uuid = str(children(schematic, "uuid")[0][1])
    fps = {next(n[2] for n in children(fp, "fp_text") if str(n[1]) == "reference"): fp
           for fp in children(fixture_ast(), "footprint")}
    symbols = children(schematic, "symbol")
    assert len(symbols) == len(fps) == 4
    library = {node[1]: node for node in children(children(schematic, "lib_symbols")[0], "symbol")}
    for symbol in symbols:
        properties = {p[1]: p[2] for p in children(symbol, "property")}
        fp = fps[properties["Reference"]]
        assert properties["Value"] == next(p[2] for p in children(fp, "fp_text") if str(p[1]) == "value")
        assert properties["Footprint"] == fp[1]
        instance_uuid = str(children(symbol, "uuid")[0][1])
        assert children(fp, "path")[0][1] == f"/{root_uuid}/{instance_uuid}"
        lib_id = children(symbol, "lib_id")[0][1]
        pins = [pin for unit in children(library[lib_id], "symbol") for pin in children(unit, "pin")]
        assert {str(children(pin, "number")[0][1]) for pin in pins} == {"1", "2"}
        assert all(str(pin[1]) == "passive" for pin in pins)
        assert {str(p[1]) for p in children(fp, "pad")} == {"1", "2"}


def test_canvas_pad_metadata_preserves_actual_shape_and_drill():
    board = read_board(EXAMPLE)
    header = board["footprints"][0]["pads"]
    assert header[0]["shape"] == "rect"
    assert header[1]["shape"] == "circle"
    assert header[0]["size"] == [1.6, 1.6]
    assert header[0]["drill_mm"] == 0.8
    assert header[0]["layers"] == ["*.Cu", "*.Mask"]
    assert header[0]["rotation"] == 0
    assert "drill_mm" not in board["footprints"][2]["pads"][0]


def replace_courtyard(ast, primitives):
    fp = children(ast, "footprint")[2]
    fp.remove(children(fp, "fp_rect")[0])
    fp.extend(sexpdata.loads(text) for text in primitives)
    return fp


def test_closed_concave_multisegment_courtyard_has_conservative_envelope(tmp_path):
    ast = fixture_ast()
    vertices = [(-3, -2), (3, -2), (3, 0), (0, 0), (0, 2), (-3, 2)]
    primitives = [f'(fp_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "F.CrtYd") (width 0.05))'
                  for a, b in zip(vertices, vertices[1:] + vertices[:1])]
    replace_courtyard(ast, primitives)
    board = read_board(save_ast(tmp_path, ast))
    assert not board["unsupported"]
    assert board["footprints"][2]["bounds"] == pytest.approx([36.975, 25.975, 43.025, 30.025])
    assert board["footprints"][2]["courtyard"]["contours"] == 1


@pytest.mark.parametrize("primitives,contours", [
    (['(fp_poly (pts (xy -3 -2) (xy 3 -2) (xy 2 2) (xy -2 2)) (layer "F.CrtYd") (width 0.05))'], 1),
    (['(fp_circle (center 0 0) (end 3 0) (layer "F.CrtYd") (width 0.05))'], 1),
    (['(fp_rect (start -3 -2) (end -1 2) (layer "F.CrtYd") (width 0.05))',
      '(fp_rect (start 1 -2) (end 3 2) (layer "F.CrtYd") (width 0.05))'], 2),
])
def test_closed_polygon_circle_and_multiple_courtyards(tmp_path, primitives, contours):
    ast = fixture_ast()
    replace_courtyard(ast, primitives)
    board = read_board(save_ast(tmp_path, ast))
    assert not board["unsupported"]
    assert board["footprints"][2]["courtyard"]["contours"] == contours
    assert board["footprints"][2]["bounds"][0] <= 37
    assert board["footprints"][2]["bounds"][2] >= 43


@pytest.mark.parametrize("primitives", [
    ['(fp_line (start -3 -2) (end 3 -2) (layer "F.CrtYd") (width 0.05))'],
    ['(fp_poly (pts (xy -3 -2) (xy 3 2) (xy -3 2) (xy 3 -2)) (layer "F.CrtYd") (width 0.05))'],
    ['(fp_arc (start -3 0) (mid 0 3) (end 3 0) (layer "F.CrtYd") (width 0.05))'],
    ['(fp_curve (pts (xy -3 -2) (xy 3 -2) (xy 3 2) (xy -3 2)) (layer "F.CrtYd") (width 0.05))'],
])
def test_incomplete_or_complex_courtyards_are_explicitly_unsupported(tmp_path, primitives):
    ast = fixture_ast()
    replace_courtyard(ast, primitives)
    board = read_board(save_ast(tmp_path, ast))
    assert any("courtyard" in issue for issue in board["unsupported"])
    assert not board["footprints"][2]["courtyard"]["supported"]


def test_duplicate_layer_ordinals_and_false_copper_names_are_detected(tmp_path):
    ast = fixture_ast()
    layers = children(ast, "layers")[0]
    layers.append([31, "In1.Cu", sexpdata.Symbol("signal")])
    layers.append([2, "In3.Cu", sexpdata.Symbol("signal")])
    board = read_board(save_ast(tmp_path, ast))
    assert any("ordinals" in issue for issue in board["unsupported"])
    assert any("consecutive" in issue for issue in board["unsupported"])


def test_stackup_must_match_declared_copper_layers(tmp_path):
    ast = fixture_ast()
    children(ast, "setup")[0].append(sexpdata.loads('(stackup (layer "F.Cu") (layer "In1.Cu") (layer "B.Cu"))'))
    board = read_board(save_ast(tmp_path, ast))
    assert any("stackup" in issue for issue in board["unsupported"])
    assert board["stackup_copper_layers"] == ["F.Cu", "In1.Cu", "B.Cu"]


def test_pad_on_undeclared_inner_layer_is_unsupported(tmp_path):
    ast = fixture_ast()
    pad = children(children(ast, "footprint")[2], "pad")[0]
    children(pad, "layers")[0][1] = "In1.Cu"
    assert any("undeclared copper" in issue for issue in read_board(save_ast(tmp_path, ast))["unsupported"])
