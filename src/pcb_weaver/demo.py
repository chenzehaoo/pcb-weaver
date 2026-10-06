"""An executable demonstration, using the same service operations as MCP."""
from .catalog import generate_report
from .storage import read_json, write_json


def run_demo(engine, example, project, route=False):
    boards = sorted(example.glob("*.kicad_pcb"))
    if len(boards) != 1:
        raise ValueError("Demo directory must contain exactly one KiCad board")
    imported = engine.import_project(project, str(boards[0]), read_json(example / "constraints.json"))
    initial = imported["revision"]["id"]
    plan = engine.plan_layout(project, initial, 3)
    feasible = [c for c in plan["candidates"] if c["feasible"]]
    current = initial
    applied = None
    if feasible:
        applied = engine.apply_layout(project, initial, plan["plan_id"], feasible[0]["id"])
        current = applied["revision"]["id"]
    routing = engine.route_revision(project, current, 5) if route else {"status": "not_requested"}
    if routing["status"] == "routed_unverified":
        current = routing["revision"]["id"]
    verification = engine.verify_revision(project, current) if route else {"status": "not_requested"}
    release = engine.build_release(project, current) if verification["status"] == "passed" else {"status": "blocked", "reason": "Verification has not passed"}
    report = generate_report(engine, project, current)
    result = {"status": "completed" if not route or release["status"] == "released" else "blocked",
              "project": project, "initial_revision": initial, "final_revision": current,
              "plan": plan, "applied": applied, "routing": routing, "verification": verification,
              "release": release, "report": report, "eco": engine.compare_revisions(project, initial, current)}
    write_json(engine.store.project_dir(project) / "demo-result.json", result)
    return result
