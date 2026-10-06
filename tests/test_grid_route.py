from copy import deepcopy

import pytest

from pcb_weaver.grid_route import propose
from pcb_weaver.repair_geometry import _read, _board, _serialize
from test_clearance import source_board
from test_repair_geometry import segment, via, tag

RULES = {"track_width": .2, "clearance": .15, "via_diameter": .6, "via_drill": .3}
FINDING = {"items": [{"uuid": "a", "pos": {"x": 32, "y": 30}}, {"uuid": "b", "pos": {"x": 40, "y": 30}}]}


@pytest.mark.parametrize("invalid",["yes",1,None])
def test_exact_terminal_policy_is_explicit_boolean(tmp_path,invalid):
    with pytest.raises(ValueError,match="exact_terminals"):
        propose(fixture(tmp_path),tmp_path / "out.kicad_pcb","VIN",[30,25,45,35],FINDING,RULES,
                exact_terminals=invalid)


@pytest.mark.parametrize("points", [[[0,0]],[[35,float("nan")]],[[35]],[[35,30]]*9])
def test_extra_landing_points_remain_bounded(tmp_path, points):
    with pytest.raises(ValueError):
        propose(fixture(tmp_path),tmp_path / "out.kicad_pcb","VIN",[30,25,45,35],FINDING,RULES,
                exact_edges=True,extra_points=points)


def test_extra_landing_points_still_use_collision_checked_grid(tmp_path):
    result = propose(fixture(tmp_path),tmp_path / "out.kicad_pcb","VIN",[30,25,45,35],FINDING,RULES,
                     exact_edges=True,extra_points=[[36.137,30.013]])
    assert result["status"] == "proposed" and result["extra_points"] == [[36.137,30.013]]


def fixture(tmp_path, layer="F.Cu", barrier=False):
    path = source_board(tmp_path)
    ast = [n for n in _read(path)[0] if tag(n) != "segment"]
    a, b = segment("a", (32, 30), (34, 30), .2, 2), segment("b", (40, 30), (42, 30), .2, 2)
    next(n for n in b if tag(n) == "layer")[1] = layer
    ast.extend([a,b])
    if barrier:
        for i, side in enumerate(["F.Cu", "B.Cu"]):
            wall = segment("wall" + str(i), (37, 24), (37, 36), 1, 1)
            next(n for n in wall if tag(n) == "layer")[1] = side
            ast.append(wall)
    path.write_bytes(_serialize(ast))
    return path


@pytest.mark.parametrize("layer,via", [("F.Cu",False),("B.Cu",True)])
@pytest.mark.parametrize("exact", [False, True])
def test_additive_path_preserves_every_source_item(tmp_path, layer, via, exact):
    path = fixture(tmp_path, layer)
    before = path.read_bytes()
    output = tmp_path / "out.kicad_pcb"
    result = propose(path, output, "VIN", [30,25,45,35], FINDING, RULES, exact_edges=exact)
    assert result["status"] == "proposed", result
    assert bool(result["added_vias"]) == via
    old, new = _read(path)[0], _read(output)[0]
    assert new[:len(old)] == old and path.read_bytes() == before
    added = _board(new, source=True)["copper"][2:]
    assert all(c["geometry"]["net"] == "VIN" for c in added)
    assert all(c["geometry"].get("width", .2) == .2 for c in added)
    assert not result["manufacturing_authorized"] and result["requires_native_verification"]


@pytest.mark.parametrize("exact", [False, True])
def test_fixed_obstacle_wall_blocks_without_output(tmp_path, exact):
    path = fixture(tmp_path, barrier=True)
    output = tmp_path / "out.kicad_pcb"
    result = propose(path, output, "VIN", [30,25,45,35], FINDING, RULES, exact_edges=exact)
    assert result["status"] == "blocked" and not output.exists()


@pytest.mark.parametrize("change", [{"track_width":0},{"clearance":float("nan")},{"via_drill":.8},{"via_diameter":True}])
def test_invalid_native_rules_are_rejected(tmp_path, change):
    with pytest.raises(ValueError):
        propose(fixture(tmp_path), tmp_path / "out.kicad_pcb", "VIN", [30,25,45,35], FINDING, {**RULES, **change})


def test_unknown_pair_and_existing_output_rejected(tmp_path):
    path = fixture(tmp_path)
    changed = deepcopy(FINDING)
    changed["items"][0]["uuid"] = "unknown"
    with pytest.raises(ValueError):
        propose(path, tmp_path / "out.kicad_pcb", "VIN", [30,25,45,35], changed, RULES)
    with pytest.raises(ValueError):
        propose(path, path, "VIN", [30,25,45,35], FINDING, RULES)


@pytest.mark.parametrize("half_gap,expected", [(.57, "proposed"), (.54, "blocked")])
def test_stationary_via_uses_real_envelope_not_trace_sweep(tmp_path, half_gap, expected):
    path = fixture(tmp_path, "B.Cu")
    ast = _read(path)[0]
    for i, layer in enumerate(["F.Cu", "B.Cu"]):
        for j, y in enumerate([30 - half_gap, 30 + half_gap]):
            wall = segment(f"wall-{i}-{j}", (29, y), (46, y), .2, 1)
            next(n for n in wall if tag(n) == "layer")[1] = layer
            ast.append(wall)
    path.write_bytes(_serialize(ast))
    output = tmp_path / "out.kicad_pcb"
    result = propose(path, output, "VIN", [30,25,45,35], FINDING, RULES)
    assert result["status"] == expected
    if expected == "proposed":
        vias = [c["geometry"] for c in _board(_read(output)[0], source=True)["copper"]
                if c["geometry"]["kind"] == "via"]
        assert vias
        assert all(half_gap - abs(v["at"][1] - 30) - .1 - v["size"] / 2 >= RULES["clearance"] for v in vias)
    else:
        assert not output.exists()


def test_reserved_corridors_block_without_becoming_board_copper(tmp_path):
    path = fixture(tmp_path)
    original = path.read_bytes()
    reservations = [{"layer": l, "width":1, "points":[[37,24],[37,36]]} for l in ["F.Cu", "B.Cu"]]
    result = propose(path, tmp_path / "out.kicad_pcb", "VIN", [30,25,45,35], FINDING,
                     RULES, exact_edges=True, reservations=reservations)
    assert result["status"] == "blocked"
    assert path.read_bytes() == original
    assert not (tmp_path / "out.kicad_pcb").exists()


@pytest.mark.parametrize("refine", [None, {"region":[35.5,28.5,37.5,31], "step":.02}])
def test_exact_edges_preserve_continuous_obstacle_clearance(tmp_path, refine):
    from shapely.geometry import LineString
    path = fixture(tmp_path)
    ast = _read(path)[0]
    ast.append(segment("obstacle", (36, 29), (37, 30.3), .2, 1))
    path.write_bytes(_serialize(ast))
    output = tmp_path / "out.kicad_pcb"
    result = propose(path, output, "VIN", [30,25,45,35], FINDING, RULES, exact_edges=True, refine=refine)
    assert result["status"] == "proposed"
    wall = LineString([(36,29),(37,30.3)])
    old_ids = {c["id"] for c in _board(ast, source=True)["copper"]}
    for c in _board(_read(output)[0], source=True)["copper"]:
        g = c["geometry"]
        if c["id"] not in old_ids and g["kind"] == "segment" and g["layer"] == "F.Cu":
            assert LineString([g["start"],g["end"]]).distance(wall) >= .1 + g["width"] / 2 + RULES["clearance"]


@pytest.mark.parametrize("refine,exact", [({"region":[35,28,38,31], "step":.01},False),
    ({"region":[29,28,38,31], "step":.01},True), ({"region":[35,28,38,31], "step":.001},True),
    ({"region":[35,28,38,31], "step":float("nan")},True)])
def test_invalid_refinement_rejected(tmp_path, refine, exact):
    with pytest.raises(ValueError, match="Refinement"):
        propose(fixture(tmp_path), tmp_path / "out.kicad_pcb", "VIN", [30,25,45,35], FINDING,
                RULES, exact_edges=exact, refine=refine)


@pytest.mark.parametrize("costs", [{"F.Cu":0}, {"F.Cu":float("inf")}, {"F.Cu":True}, {"In7.Cu":2}])
def test_invalid_layer_preferences_rejected(tmp_path, costs):
    with pytest.raises(ValueError, match="layer costs"):
        propose(fixture(tmp_path), tmp_path / "out.kicad_pcb", "VIN", [30,25,45,35], FINDING,
                RULES, layer_costs=costs)


def test_layer_cost_is_distinct_from_geometric_trace_length(tmp_path):
    result = propose(fixture(tmp_path), tmp_path / "out.kicad_pcb", "VIN", [30,25,45,35],
                     FINDING, RULES, exact_edges=True, layer_costs={"F.Cu":2})
    assert result["status"] == "proposed"
    assert result["added_vias"] == 0
    assert result["weighted_cost"] == pytest.approx(2 * result["trace_length_mm"])


@pytest.mark.parametrize("with_via", [False, True])
def test_terminal_islands_only_join_through_real_shared_layers(tmp_path, with_via):
    source = fixture(tmp_path, "B.Cu")
    ast = _read(source)[0]
    tail = segment("back-tail", (40,30), (40,33), .2, 2)
    next(n for n in tail if tag(n) == "layer")[1] = "B.Cu"
    ast.extend([tail, segment("front-tail", (36,33), (40,33), .2, 2),
                segment("disconnected", (32,34), (33,34), .2, 2)])
    if with_via:
        ast.append(via("bridge", (40,33), .8, 2))
    source.write_bytes(_serialize(ast))
    result = propose(source, tmp_path / "out.kicad_pcb", "VIN", [30,25,45,35], FINDING,
                     RULES, exact_edges=True, expand_terminals=True)
    assert result["status"] == "proposed"
    assert result["terminal_component_items"] == [1,4 if with_via else 2]
    if with_via:
        assert result["added_vias"] == 0


@pytest.mark.parametrize("gap,expected", [(.4,"proposed"),(.39999,"blocked")])
def test_power_width_can_use_rule_boundary_but_not_undersized_clearance(tmp_path, gap, expected):
    source = fixture(tmp_path)
    ast = _read(source)[0]
    for i, layer in enumerate(["F.Cu","B.Cu"]):
        for j, y in enumerate([30-gap,30+gap]):
            wall = segment(f"power-wall-{i}-{j}", (29,y), (46,y), .1, 1)
            next(n for n in wall if tag(n) == "layer")[1] = layer
            ast.append(wall)
    source.write_bytes(_serialize(ast))
    original = source.read_bytes()
    output = tmp_path / "out.kicad_pcb"
    result = propose(source, output, "VIN", [30,25,45,35], FINDING, {**RULES,"track_width":.4},
                     exact_edges=True, allow_rule_boundary=True)
    assert result["status"] == expected
    assert source.read_bytes() == original
    if expected == "proposed":
        assert result["added_vias"] == 0
        old = {c["id"] for c in _board(ast,source=True)["copper"]}
        assert all(c["geometry"]["width"] == .4 for c in _board(_read(output)[0],source=True)["copper"] if c["id"] not in old)


def test_boundary_mode_cannot_skip_continuous_checks(tmp_path):
    with pytest.raises(ValueError, match="continuous"):
        propose(fixture(tmp_path), tmp_path / "out.kicad_pcb", "VIN", [30,25,45,35], FINDING,
                RULES, allow_rule_boundary=True)


def test_external_terminal_landing_retains_positive_copper_overlap(tmp_path):
    from shapely.geometry import LineString
    source = fixture(tmp_path)
    output = tmp_path / "out.kicad_pcb"
    result = propose(source, output, "VIN", [30,25,45,35], FINDING, {**RULES,"track_width":.4},
                     exact_edges=True, allow_rule_boundary=True)
    assert result["status"] == "proposed" and not result["added_vias"]
    points = [p[1:] for p in result["path"]]
    copper = LineString(points).buffer(.2)
    for terminal in [[(32,30),(34,30)],[(40,30),(42,30)]]:
        assert copper.intersection(LineString(terminal).buffer(.1)).area > 0
    assert result["terminal_overlap_mm"] == .025
