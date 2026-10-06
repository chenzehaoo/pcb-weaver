"""Opt-in live pcbnew parity: PCB_WEAVER_NATIVE_COMMAND is a JSON argv prefix."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from pcb_weaver.board import compare_boards, read_board, write_placements


ROOT = Path(__file__).resolve().parents[1]
COMMAND = os.environ.get("PCB_WEAVER_NATIVE_COMMAND")
pytestmark = pytest.mark.skipif(not COMMAND, reason="Native KiCad Python command not configured")


def native(path, *extra):
    command = json.loads(COMMAND)

    def mapped(value):
        value = str(value).replace("\\", "/")
        if "wsl.exe" in command[0].lower() and len(value) > 2 and value[1] == ":":
            value = "/mnt/" + value[0].lower() + value[2:]
        return value

    result = subprocess.run(command + [mapped(Path(__file__).with_name("native_geometry_probe.py")),
                                      mapped(path), *(mapped(v) for v in extra)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))


def parity(board, snapshot):
    expected = {fp["reference"]: fp for fp in snapshot["footprints"]}
    count, error = 0, 0.0
    assert len(board["copper_layers"]) == snapshot["layers"]
    assert len(board["footprints"]) == len(expected)
    for fp in board["footprints"]:
        # Sort repeated physical pads without collapsing their electrical identity.
        key = lambda p: (p["number"], p["x"], p["y"])
        actual_pads, native_pads = sorted(fp["pads"], key=key), sorted(expected[fp["reference"]]["pads"], key=key)
        assert len(actual_pads) == len(native_pads)
        for actual, real in zip(actual_pads, native_pads):
            assert actual["number"] == real["number"]
            error = max(error, abs(actual["x"] - real["x"]), abs(actual["y"] - real["y"]))
            assert [actual["x"], actual["y"]] == pytest.approx([real["x"], real["y"]], abs=2e-6)
            assert (actual["rotation"] - real["rotation"] + 180) % 360 - 180 == pytest.approx(0, abs=1e-6)
            assert actual["size"] == pytest.approx(real["size"], abs=1e-6)
            assert actual.get("drill_size", [actual.get("drill_mm", 0)] * 2) == pytest.approx(real["drill_size"], abs=1e-6)
            count += 1
    print(json.dumps({"native_version": snapshot["version"], "pads_verified": count,
                      "maximum_coordinate_error_mm": error}))


def test_coldfire_all_pads_against_live_native():
    path = ROOT / "examples/system-controller/kit-dev-coldfire-xilinx_5213.kicad_pcb"
    if not path.exists():
        pytest.skip("System-controller benchmark not available")
    original = path.read_bytes()
    board = read_board(path)
    assert not board["unsupported"]
    parity(board, native(path))
    assert path.read_bytes() == original


def test_native_flip_asymmetric_pads_and_translation(tmp_path):
    source = ROOT / "examples/two-layer/two-layer.kicad_pcb"
    flipped = tmp_path / "flipped.kicad_pcb"
    snapshot = native(source, "--asymmetric", flipped)
    board = read_board(flipped)
    parity(board, snapshot)
    assert not board["unsupported"]
    moved = tmp_path / "moved.kicad_pcb"
    placements = [{"reference": fp["reference"], "x": fp["x"] + 0.12345678,
                   "y": fp["y"] - 0.23456789} for fp in board["footprints"]]
    updated = write_placements(flipped, moved, placements)
    parity(updated, native(moved))
    assert not compare_boards(board, updated)["connectivity_changed"]
