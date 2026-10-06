"""Explicit diagnostic candidate from the completed, rejected system-controller SES."""
import json
from pathlib import Path
import shutil

from pcb_weaver import catalog
from pcb_weaver.board import read_board
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.ses_widths import raise_minima
from pcb_weaver.storage import digest, read_json, write_json


def main():
    root = Path(__file__).resolve().parents[1]
    workspace, config = load_runtime(root / "data", root / "toolchain.unified.json")
    service = EngineeringService(workspace, config)
    project, parent = "system-controller", "r-48eb2d3661aa4575"
    failed = workspace / "projects" / project / "revisions" / "r-ea660b0b6de74c8d" / "routing"
    operations = read_json(failed / "operations.json")
    route = operations[1]
    ses = failed / "board.ses"
    if (route["status"] != "ok" or not route["effective_settings_verified"] or
            route["ses_sha256"] != digest(ses) or route["dsn_sha256"] != digest(failed / "board.dsn") or
            route["router_result"]["final_state"] != "COMPLETED" or
            digest(Path(route["router_result_path"])) != route["router_result_sha256"]):
        raise ValueError("Completed router evidence is missing or changed")
    ses_hash = digest(ses)
    evidence = {"project": project, "parent": parent, "source_job": "job-537ad4d30aa74ea3",
                "manufacturing_authorized": False, "source_ses_sha256": ses_hash}
    with service.store.lock(project):
        old, parent_folder = service._verified(project, parent)
        if digest(failed.parent / "design" / old["board"]) != old["files"][old["board"]]:
            raise ValueError("SES routing input belongs to a different layout")
        if digest(failed.parent / "constraints.json") != old["constraints_hash"]:
            raise ValueError("SES routing intent differs from the saved parent")
        child, folder, old = service._new(project, parent)
        source = folder / "design" / old["board"]
        board = read_board(source)
        if board["tracks"] or board["vias"]:
            raise ValueError("This recovery supports the recorded unrouted layout only")
        rules = read_json(source.with_suffix(".kicad_pro"))
        minimum = rules["board"]["design_settings"]["rules"]["min_track_width"]
        per_net = {net: entry.get("declared_min_width_mm", entry.get("minimums", {}).get("track_width"))
                   for net, entry in rules.get("pcb_weaver_net_rules", {}).items()}
        output = folder / "width-recovery"
        output.mkdir()
        adjusted = output / "minimum-width.ses"
        audit = raise_minima(ses, adjusted, minimum, per_net, {n["name"] for n in board["nets"]})
        write_json(output / "width-audit.json", audit)
        evidence["width_audit"] = audit
        result = service.toolchain.import_ses(source, adjusted, output / old["board"])
        evidence["native_import"] = result
        if result["status"] == "ok":
            routed = output / old["board"]
            if service._electrical_signature(board) != service._electrical_signature(read_board(routed)):
                raise ValueError("Recovery changed placement or net assignments")
            shutil.copy2(routed, source)
            service._verified(project, parent)
            revision = service._seal(project, child, folder, old["board"], parent, "repair_candidate",
                        {"recovery_method": "raise_ses_track_minima", "source_ses_sha256": ses_hash,
                         "width_audit_sha256": digest(output / "width-audit.json")})
            evidence["candidate_revision"] = revision["id"]
            evidence["verification"] = service._verify(project, child)
            evidence["status"] = evidence["verification"]["status"]
        else:
            evidence["status"] = "blocked"
        service._verified(project, parent)
        if digest(ses) != ses_hash:
            raise ValueError("Original SES changed")
        evidence["parent_preserved"] = True
        write_json(output / "result.json", evidence)
    if evidence.get("candidate_revision"):
        catalog.generate_report(service, project, evidence["candidate_revision"])
    record = root / "docs" / "validation" / ("system-width-recovery-" + child + ".json")
    write_json(record, evidence)
    print(json.dumps({"status": evidence["status"], "candidate": evidence.get("candidate_revision"),
                      "changes": len(evidence["width_audit"]["changes"]), "evidence": str(record)}), flush=True)


if __name__ == "__main__":
    main()
