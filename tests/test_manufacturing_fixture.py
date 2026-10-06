"""The positive manufacturing fixture changes paste intent, never electrical rules."""
from pathlib import Path

import sexpdata


ROOT = Path(__file__).resolve().parents[1] / "examples"


def without_paste(value):
    if not isinstance(value, list):
        return value
    return [without_paste(item) for item in value
            if item not in ("F.Paste", "B.Paste") and not (
                isinstance(item, list) and len(item) > 1
                and item[0] in (34, 35) and item[1] in ("F.Paste", "B.Paste"))]


def test_manufacturing_fixture_only_adds_declared_paste():
    original, prepared = ROOT / "routing-demo", ROOT / "manufacturing-demo"
    edited = {"two-layer.kicad_pcb", "Original.pretty/PassiveFixture.kicad_mod"}
    for path in original.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(original).as_posix()
        target = prepared / relative
        if relative in edited:
            before = sexpdata.loads(path.read_text(encoding="utf-8"))
            after = sexpdata.loads(target.read_text(encoding="utf-8"))
            assert before != after
            assert without_paste(after) == before
        else:
            assert target.read_bytes() == path.read_bytes()
