"""Read-only inventory of pinned upstream KiCad candidate boards."""
import json
from collections import Counter
from pathlib import Path

import sexpdata


def children(tree, tag):
    return [n for n in tree if isinstance(n, list) and n and str(n[0]) == tag]


def inventory(path):
    tree = sexpdata.loads(path.read_text(encoding="utf-8-sig"))
    footprints = children(tree, "footprint") or children(tree, "module")
    layers = children(tree, "layers")[0]
    return {
        "path": str(path), "footprints": len(footprints),
        "pads": sum(len(children(f, "pad")) for f in footprints),
        "nets": sum(bool(n[2]) for n in children(tree, "net")),
        "copper_layers": [str(n[1]) for n in layers[1:] if str(n[1]).endswith(".Cu")],
        "packages": dict(Counter(str(f[1]) for f in footprints)),
        "tracks": len(children(tree, "segment")), "zones": len(children(tree, "zone")),
        "schematics": sorted(p.name for p in path.parent.glob("*.kicad_sch")),
    }


if __name__ == "__main__":
    root = Path(__file__).parent / "upstream" / "kicad-source" / "demos"
    results = []
    for path in sorted(root.rglob("*.kicad_pcb")):
        try:
            results.append(inventory(path))
        except Exception as exc:
            results.append({"path": str(path), "parse_error": type(exc).__name__})
    print(json.dumps(results, indent=2))
