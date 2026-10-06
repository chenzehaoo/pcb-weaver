"""Read-only deployment and immutable-board checks, with a JSON audit artifact."""
import argparse
from pathlib import Path

from pcb_weaver import catalog
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, write_json


def run(args):
    source = Path(__file__).resolve().parents[1]
    root = args.root.resolve(strict=True)
    files = ["src/pcb_weaver/"+name for name in (
        "models.py","auto_repair.py","auto_proposal.py","grid_route.py","service.py",
        "jobs.py","server.py","integration.py","web/index.html","web/app.js","web/style.css")]
    files += ["README.md", "docs/AUTO-REPAIR-VALIDATION.md", "skills/pcb-engineering/SKILL.md", "tests/test_auto_repair.py", "tests/test_grid_route.py",
              "tests/test_repair_protocol.py", "scripts/accept_auto_repair.py", "scripts/accept_auto_repair_mcp.py",
              "scripts/check_auto_repair_ui.cjs", "scripts/verify_auto_deployment.py"]
    evidence = {"status":"running","files":{},"revisions":{},"config":{}}
    try:
        for name in files:
            sha = digest(source / name)
            assert digest(root / name) == sha, name
            evidence["files"][name] = sha
        configs = {".mcp.json":"23e3dca394903f0aa48564e7a153dee0471071768abbc63def928ad4435cdfa5",
                   "toolchain.unified.json":"fd6ee5114bf647e1912d281743014694157ca2e1e73c7d059ef96b1be6e5641e"}
        for name,sha in configs.items():
            assert digest(root / name) == sha, name
            evidence["config"][name] = sha
        workspace,config = load_runtime(root / "data",root / "toolchain.unified.json")
        engine = EngineeringService(workspace,config)
        boards = {"r-b8fb758edb12488f":"1c84db8a3ecb559c33c0b01bd62f4ba06f7afa34116e99bca7c16b2e30b06064",
                  "r-1bbf979cb3cc4206":"3f871730ae06e4c6c401555d0be4cb85effb43f7106385bb94387205c14584c5"}
        for revision,sha in boards.items():
            data,folder = engine._verified("system-clearance-acceptance",revision)
            assert digest(folder / "design" / data["board"]) == sha
            evidence["revisions"][revision] = {"board_sha256":sha,"digest":data["digest"]}
        check = catalog.verification(engine,"system-clearance-acceptance","r-1bbf979cb3cc4206")
        assert check["status"] == "passed" and check["drc"]["errors"] == 0 and check["drc"]["unconnected"] == 0
        assert check["erc"]["errors"] == 0 and check["drc"]["warnings"] == 53 and check["erc"]["warnings"] == 16
        evidence.update(status="passed",verification=check)
    except BaseException as error:
        evidence.update(status="failed",error=str(error))
        raise
    finally:
        write_json(args.output,evidence)
    print(f"Deployment passed: {len(files)} files; two immutable boards; unchanged configs; native passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    run(parser.parse_args())
