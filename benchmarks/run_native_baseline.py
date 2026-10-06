"""Run real KiCad checks on a disposable copy using KiCad's Python runtime."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def rows(report, kind):
    if kind == "erc":
        return [v for sheet in report["sheets"] for v in sheet["violations"]]
    return report["violations"] + report["unconnected_items"] + report["schematic_parity"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.project.resolve(), args.output.resolve()
    if output.exists():
        raise ValueError("Use a fresh evidence directory")
    before = hashes(source)
    output.mkdir(parents=True)
    design = output / "design"
    shutil.copytree(source, design)
    boards = list(design.glob("*.kicad_pcb"))
    if len(boards) != 1:
        raise ValueError("Expected exactly one root board")
    board = boards[0]
    version = subprocess.check_output(["kicad-cli", "version"], text=True).strip()
    commands = []
    operations = [
        ("erc", ["sch", "erc", "--format", "json", "--severity-all", "--exit-code-violations",
                 "--output", str(output / "erc.json"), str(board.with_suffix(".kicad_sch"))]),
        ("drc", ["pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors",
                 "--schematic-parity", "--exit-code-violations", "--output", str(output / "drc.json"), str(board)]),
        ("netlist", ["sch", "export", "netlist", "--format", "kicadxml", "--output", str(output / "netlist.xml"),
                     str(board.with_suffix(".kicad_sch"))]),
    ]
    checks = {}
    for kind, argv in operations:
        start = time.monotonic()
        process = subprocess.run(["kicad-cli", *argv], cwd=design, capture_output=True, text=True, timeout=240)
        commands.append({"kind": kind, "argv": ["kicad-cli", *argv], "cwd": str(design),
                         "returncode": process.returncode, "seconds": round(time.monotonic() - start, 3),
                         "stdout": process.stdout, "stderr": process.stderr})
        if kind in {"erc", "drc"} and (output / (kind + ".json")).exists():
            report = json.loads((output / (kind + ".json")).read_text())
            entries = rows(report, kind)
            checks[kind] = {"by_severity": dict(Counter(v["severity"] for v in entries)),
                            "by_type": dict(Counter(v["type"] for v in entries)),
                            "ignored_checks": report.get("ignored_checks"),
                            "excluded": sum(bool(v.get("excluded")) for v in entries)}
            if kind == "drc":
                checks[kind].update(unconnected=len(report["unconnected_items"]), parity=len(report["schematic_parity"]))
        print(kind, process.returncode, checks.get(kind, {}), flush=True)
    import pcbnew

    native = pcbnew.LoadBoard(str(board))
    footprints = list(native.GetFootprints())
    pads = [pad for fp in footprints for pad in fp.Pads()]
    metrics = {"footprints": len(footprints), "pads": len(pads), "copper_layers": native.GetCopperLayerCount(),
               "nets_with_pads": len({p.GetNetname() for p in pads if p.GetNetname()}),
               "packages": dict(Counter(str(fp.GetFPID().GetLibItemName()) for fp in footprints)),
               "components": [{"reference": fp.GetReference(), "value": fp.GetValue(),
                               "footprint": str(fp.GetFPID().GetLibItemName()), "pads": len(list(fp.Pads())),
                               "side": native.GetLayerName(fp.GetLayer())} for fp in footprints]}
    xml = output / "netlist.xml"
    if xml.exists():
        root = ET.parse(xml).getroot()
        metrics["schematic_components"] = len(root.findall("./components/comp"))
        metrics["schematic_nets"] = len(root.findall("./nets/net"))
        metrics["symbol_pin_types"] = dict(Counter(pin.get("type") for pin in root.findall("./libparts/libpart/pins/pin")))
    unchanged = before == hashes(source)
    result = {"schema": 1, "created": datetime.now(timezone.utc).isoformat(), "kicad_version": version,
              "source": str(source), "source_hashes": before, "source_unchanged": unchanged,
              "engine_snapshot_unchanged": before == hashes(design), "commands": commands,
              "checks": checks, "metrics": metrics, "artifacts": {p.name: sha(p) for p in output.iterdir() if p.is_file()},
              "scope": "Actual native ERC/DRC/netlist baseline, not fabrication or functional signoff"}
    write(output / "baseline.json", result)
    if not unchanged:
        raise RuntimeError("Baseline run modified source")


if __name__ == "__main__":
    main()
