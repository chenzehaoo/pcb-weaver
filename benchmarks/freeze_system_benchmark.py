"""Seal benchmark inputs against their recorded native evidence, not as a release."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pcb_weaver.service import design_files
from pcb_weaver.storage import canonical, digest


def main():
    output = ROOT / "benchmarks/FROZEN.json"
    if output.exists():
        raise ValueError("This benchmark is already frozen; create a new explicitly versioned baseline")
    project = ROOT / "examples/system-controller"
    evidence = ROOT / "benchmarks/results/system-controller-frozen"
    baseline = json.loads((evidence / "baseline.json").read_text())
    all_files = {p.relative_to(project).as_posix(): digest(p) for p in project.rglob("*") if p.is_file()}
    if all_files != baseline["source_hashes"]:
        raise ValueError("Engineering inputs differ from the recorded native baseline")
    for name, checksum in baseline["artifacts"].items():
        if digest(evidence / name) != checksum:
            raise ValueError("Native evidence changed: " + name)
    design = design_files(project)
    constraint_hash = digest(project / "constraints.json")
    checks = baseline["checks"]
    result = {
        "schema_version": 1, "benchmark": "system-controller", "frozen_at": datetime.now(timezone.utc).isoformat(),
        "input_directory": "examples/system-controller", "root_board": "kit-dev-coldfire-xilinx_5213.kicad_pcb",
        "source_commit": "286b0611feca00727bf70bfa184ec2c28a745dc3", "license": "CC-BY-SA-4.0",
        "all_input_files": all_files, "design_files": design, "constraints_sha256": constraint_hash,
        "input_digest": hashlib.sha256(canonical({"files": design, "constraints": constraint_hash}).encode()).hexdigest(),
        "digest_scope": "Original benchmark inputs; service rule compilation may create a different managed revision digest",
        "native_evidence": "benchmarks/results/system-controller-frozen/baseline.json",
        "native_evidence_sha256": digest(evidence / "baseline.json"), "native_checks": checks,
        "kicad_version": baseline["kicad_version"],
        "manufacturing_release_passed": False,
        "next_stage": "Main-thread compatibility fixes, constrained placement, entirely new native routing and release-gate acceptance",
        "known_core_gaps_at_freeze": ["KiCad escaped net names: {slash} versus XML /",
                                       "14 backside capacitors need mirrored geometry support",
                                       "J201.3 needs directional slotted-drill envelope support",
                                       "Conservative placement audit still requires real optimization, not forced pass"]}
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"frozen_at": result["frozen_at"], "input_digest": result["input_digest"],
                      "design_files": len(design), "all_input_files": len(all_files), "native_checks": checks}, indent=2))


if __name__ == "__main__":
    main()
