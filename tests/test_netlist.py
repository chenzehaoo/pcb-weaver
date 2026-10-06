from copy import deepcopy
import xml.etree.ElementTree as ET

import pytest

from pcb_weaver.netlist import inspect_netlist


@pytest.fixture
def board():
    return {"footprints": [{"reference": "R1", "value": "10k", "footprint": "R:R_0603",
                            "pads": [{"number": "1", "net": "VIN"}, {"number": "2", "net": "GND"}]}]}


def netlist(tmp_path, value="10k", footprint="R:R_0603", code="123"):
    root = ET.fromstring(f'''<export><components><comp ref="R1"><value>{value}</value>
        <footprint>{footprint}</footprint></comp></components><nets>
        <net code="{code}" name="VIN"><node ref="R1" pin="1"/></net>
        <net code="99" name="GND"><node ref="R1" pin="2"/></net></nets></export>''')
    path = tmp_path / "netlist.xml"
    ET.ElementTree(root).write(path, encoding="utf-8")
    return path


@pytest.mark.parametrize("code", ["1", "875"])
def test_matching_components_and_nets_ignore_numeric_codes(tmp_path, board, code):
    result = inspect_netlist(netlist(tmp_path, code=code), board)
    assert result["status"] == "passed"
    assert result["differences"] == result["component_differences"] == []


@pytest.mark.parametrize("field,changed", [("value", "1k"), ("footprint", "R:R_0805")])
def test_component_attribute_mismatch_has_explicit_values(tmp_path, board, field, changed):
    result = inspect_netlist(netlist(tmp_path, **{field: changed}), board)
    assert result["status"] == "failed"
    assert result["differences"] == []
    assert result["component_differences"] == [{"reference": "R1", "field": field,
                                               "schematic_value": changed,
                                               "board_value": board["footprints"][0][field]}]


def test_missing_footprint_assignment_is_not_inferred(tmp_path, board):
    result = inspect_netlist(netlist(tmp_path, footprint=""), board)
    assert result["status"] == "failed"
    assert result["component_differences"][0]["schematic_value"] == ""


def test_pad_net_mismatch_still_blocks(tmp_path, board):
    board["footprints"][0]["pads"][0]["net"] = "OTHER"
    result = inspect_netlist(netlist(tmp_path), board)
    assert result["status"] == "failed"
    assert result["differences"] == [{"reference": "R1", "pad": "1", "schematic_net": "VIN", "board_net": "OTHER"}]


def test_missing_component_still_blocks(tmp_path):
    result = inspect_netlist(netlist(tmp_path), {"footprints": []})
    assert result["status"] == "failed"
    assert result["missing_references"] == ["R1"]


def test_excluded_schematic_component_does_not_require_board_attributes(tmp_path):
    path = netlist(tmp_path)
    tree = ET.parse(path)
    ET.SubElement(tree.find("./components/comp"), "property", name="exclude_from_board")
    tree.write(path)
    assert inspect_netlist(path, {"footprints": []})["status"] == "passed"


def test_duplicate_schematic_reference_rejected(tmp_path, board):
    path = netlist(tmp_path)
    tree = ET.parse(path)
    tree.find("components").append(deepcopy(tree.find("./components/comp")))
    tree.write(path)
    with pytest.raises(ValueError, match="duplicate"):
        inspect_netlist(path, board)


@pytest.mark.parametrize("xml", ["<export/>", "<unrelated><components/><nets/></unrelated>",
                                 '<!DOCTYPE export [<!ENTITY x "x">]><export><components/><nets/></export>'])
def test_invalid_export_cannot_pass(tmp_path, board, xml):
    path = tmp_path / "invalid.xml"
    path.write_text(xml)
    with pytest.raises(ValueError):
        inspect_netlist(path, board)


def test_kicad_embedded_slash_is_compared_without_changing_the_board(tmp_path, board):
    path = netlist(tmp_path)
    tree = ET.parse(path)
    tree.find("./nets/net").set("name", "/module/CLKIN/EXTAL")
    tree.write(path)
    board["footprints"][0]["pads"][0]["net"] = "/module/CLKIN{slash}EXTAL"
    result = inspect_netlist(path, board)
    assert result["status"] == "passed"
    assert result["name_normalization"] == {"/module/CLKIN{slash}EXTAL": "/module/CLKIN/EXTAL"}
    assert board["footprints"][0]["pads"][0]["net"] == "/module/CLKIN{slash}EXTAL"


def test_escaping_cannot_merge_two_board_network_identities(tmp_path, board):
    board["footprints"][0]["pads"][0]["net"] = "/A{slash}B"
    board["footprints"][0]["pads"][1]["net"] = "/A/B"
    with pytest.raises(ValueError, match="Ambiguous"):
        inspect_netlist(netlist(tmp_path), board)


@pytest.mark.parametrize("prefix", ["$", "~", "^", "_"])
def test_formatting_and_variables_are_not_treated_as_escape_tokens(tmp_path, board, prefix):
    path = netlist(tmp_path)
    tree = ET.parse(path)
    tree.find("./nets/net").set("name", prefix + "{slash}")
    tree.write(path)
    board["footprints"][0]["pads"][0]["net"] = prefix + "{slash}"
    result = inspect_netlist(path, board)
    assert result["status"] == "passed" and result["name_normalization"] == {}
