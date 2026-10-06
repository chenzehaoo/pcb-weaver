from pathlib import Path
from xml.etree import ElementTree as ET

from pcb_weaver.board import read_board
from pcb_weaver.report import board_svg


def test_system_report_uses_real_pad_sizes_and_slot_drill():
    source = Path(__file__).resolve().parents[1] / "examples/system-controller/kit-dev-coldfire-xilinx_5213.kicad_pcb"
    board = read_board(source)
    document = ET.fromstring(board_svg(board))
    ns = {"s": "http://www.w3.org/2000/svg"}
    pads = document.findall(".//s:g[@transform]", ns)
    assert len(pads) == 825
    pad = next(p for f in board["footprints"] if f["reference"] == "J201" for p in f["pads"] if p["number"] == "3")
    group = next(g for g in pads if g.attrib["transform"].startswith(f'translate({pad["x"]} {pad["y"]})'))
    hole = next(e for e in group if e.attrib.get("fill") == "#122c28")
    assert [float(hole.attrib["width"]), float(hole.attrib["height"])] == pad["drill_size"]
    assert len({(e.attrib.get("width"), e.attrib.get("height")) for e in document.findall(".//s:rect", ns)}) > 20
