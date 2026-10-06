"""Run with KiCad's Python, not the application's virtualenv."""

import json
from pathlib import Path
import sys

import pcbnew


def snapshot(path):
    board = pcbnew.LoadBoard(str(path))
    return {"version": pcbnew.GetBuildVersion(), "path": str(path),
            "layers": board.GetCopperLayerCount(),
            "footprints": [{"reference": fp.GetReference(),
                            "layer": board.GetLayerName(fp.GetLayer()),
                            "pads": [{"number": pad.GetNumber(),
                                      "x": pcbnew.ToMM(pad.GetPosition().x),
                                      "y": pcbnew.ToMM(pad.GetPosition().y),
                                      "rotation": pad.GetOrientationDegrees(),
                                      "size": [pcbnew.ToMM(pad.GetSize().x), pcbnew.ToMM(pad.GetSize().y)],
                                      "drill_size": [pcbnew.ToMM(pad.GetDrillSize().x), pcbnew.ToMM(pad.GetDrillSize().y)]}
                                     for pad in fp.Pads()]}
                           for fp in board.GetFootprints()]}


if __name__ == "__main__":
    path = Path(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == "--asymmetric":
        board = pcbnew.LoadBoard(str(path))
        for index, fp in enumerate(board.GetFootprints()):
            fp.SetLocked(False)
            for pad_index, pad in enumerate(fp.Pads()):
                pad.SetPosition(fp.GetPosition() + pcbnew.VECTOR2I(
                    pcbnew.FromMM(0.37 + pad_index * 1.3), pcbnew.FromMM(0.61 + pad_index * 0.8)))
            fp.SetOrientationDegrees([37, 90, 173, 270][index % 4])
            if index % 2 == 0:
                fp.Flip(fp.GetPosition(), False)
        path = Path(sys.argv[3])
        pcbnew.SaveBoard(str(path), board)
    print(json.dumps(snapshot(path)))
