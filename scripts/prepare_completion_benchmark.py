"""Create an attributed unrouted benchmark derivative, without altering its circuit."""
import argparse
from pathlib import Path
import shutil
from uuid import uuid4
import sexpdata

from pcb_weaver.board import read_board
from pcb_weaver.models import Constraints
from pcb_weaver.repair_geometry import _read, _serialize
from pcb_weaver.storage import digest, write_json, read_json
from pcb_weaver.compiler import CRITICAL_CHECKS


def children(node,name):
    return [x for x in node if isinstance(x,list) and x and str(x[0]) == name]


def prepare(args):
    source = args.source.resolve(strict=True)
    target = args.output.resolve()
    if target.exists():
        raise ValueError("Use a new output directory; never overwrite a benchmark input")
    original = read_board(source)
    shutil.copytree(source.parent,target)
    board = target / source.name
    ast = _read(board)[0]
    removed = {}
    edge_points = []
    for item in ast:
        if not isinstance(item,list) or not item:
            continue
        layer = children(item,"layer")
        if layer and layer[0][1] == "Edge.Cuts":
            for key in ("start","end","mid"):
                for point in children(item,key):
                    edge_points.append(point[1:3])
    if not edge_points:
        raise ValueError("No outline points; board extent must not be invented")
    for item in list(ast):
        if not isinstance(item,list) or not item:
            continue
        kind = str(item[0])
        layer = children(item,"layer")
        if kind in {"segment","via","arc","zone","group"} or (layer and layer[0][1] == "Edge.Cuts"):
            removed[kind] = removed.get(kind,0)+1
            ast.remove(item)
        elif kind.startswith("gr_") and layer and str(layer[0][1]).endswith(".Cu"):
            layer[0][1] = "Dwgs.User"
            removed["copper_graphics_to_documentation"] = removed.get("copper_graphics_to_documentation",0)+1
    bounds = [min(p[0] for p in edge_points),min(p[1] for p in edge_points),
              max(p[0] for p in edge_points),max(p[1] for p in edge_points)]
    ast.append(sexpdata.loads(f'(gr_rect (start {bounds[0]} {bounds[1]}) (end {bounds[2]} {bounds[3]}) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts") (uuid "{uuid4()}"))'))
    board.write_bytes(_serialize(ast))
    changed = read_board(board)
    assert changed["footprints"] == original["footprints"], "Circuit/placement changed during fixture preparation"
    assert changed["nets"] == original["nets"]
    if changed["unsupported"]:
        raise ValueError(str(changed["unsupported"]))
    links = []
    footprints = {fp["reference"]:fp["footprint"] for fp in changed["footprints"]}
    for schematic in target.rglob("*.kicad_sch"):
        tree = sexpdata.loads(schematic.read_text(encoding="utf-8-sig"),nil=None,true=None,false=None)
        for symbol in children(tree,"symbol"):
            props = {p[1]:p for p in children(symbol,"property")}
            ref = props.get("Reference",[None,None,None])[2]
            prop = props.get("Footprint")
            if ref not in footprints or not prop or prop[2] == footprints[ref]:
                continue
            if str(prop[2]).split(":")[-1] != footprints[ref].split(":")[-1]:
                raise ValueError(f"Non-alias footprint mismatch requires review: {ref}: {prop[2]} / {footprints[ref]}")
            links.append({"reference":ref,"before":prop[2],"after":footprints[ref]})
            prop[2] = footprints[ref]
        schematic.write_bytes(_serialize(tree))
    project_path = board.with_suffix(".kicad_pro")
    project = read_json(project_path)
    strengthened = []
    for kind,section in (("ERC",project.setdefault("erc",{})),("DRC",project.setdefault("board",{}).setdefault("design_settings",{}))):
        for key in sorted(CRITICAL_CHECKS[kind]):
            severities = section.setdefault("rule_severities",{})
            if severities.get(key) != "error":
                strengthened.append({"kind":kind,"check":key,"before":severities.get(key),"after":"error"})
                severities[key] = "error"
    write_json(project_path,project)
    fixed = [f["reference"] for f in changed["footprints"] if any(s in f["footprint"].lower() for s in ("connector","header","dsub","terminalblock"))]
    allowed_bodies = set(fixed) if args.allow_connector_overhang else set()
    if args.allow_mounting_overhang:
        allowed_bodies.update(f["reference"] for f in changed["footprints"] if f["pads"]
            and all(p["type"] == "np_thru_hole" and not p["number"] and not p["net"] for p in f["pads"]))
    overhang = [f["reference"] for f in changed["footprints"] if f["reference"] in allowed_bodies
        and (f["bounds"][0] < bounds[0]+.5 or f["bounds"][1] < bounds[1]+.5 or f["bounds"][2] > bounds[2]-.5 or f["bounds"][3] > bounds[3]-.5)]
    intent = Constraints.model_validate({"board":{"layers":len(changed["copper_layers"]),"edge_clearance_mm":.5,"component_gap_mm":.25},
        "fabrication":{"min_track_mm":.25,"min_clearance_mm":.2,"min_via_drill_mm":.4},
        "fixed_references":fixed,"edge_overhang_references":overhang,
        "placement":{"algorithm":"legalize","passes":4,"max_iterations":80}})
    write_json(target / "constraints.json",intent.model_dump())
    license_source = source.parents[2] / "LICENSE.README"
    shutil.copy2(license_source,target / "LICENSE.KiCad.README")
    write_json(target / "PROVENANCE.json",{"upstream_repository":"https://github.com/KiCad/kicad-source-mirror",
        "upstream_commit":"286b0611feca00727bf70bfa184ec2c28a745dc3","upstream_directory":source.parent.name,
        "license":"CC-BY-SA-4.0","source_board_sha256":digest(source),"derived_board_sha256":digest(board),
        "derivative_notice":"Unrouted rectangular-envelope PCB Weaver benchmark derivative; not the original mechanical design or manufacturing approved",
        "changes":removed,"schematic_library_aliases":links,"strengthened_checks":strengthened,
        "rectangular_test_outline":bounds,"preserved":"All physical footprints, pads, nets, values, rotations, sides, schematic wires and local libraries; only same-basename schematic library nicknames synchronized",
        "components":len(changed["footprints"]),"nets":len(changed["nets"]),"fixed_references":fixed,
        "explicit_body_overhang":overhang,"body_overhang_scope":"Connector body and, only with explicit flag, mounting-head courtyard; physical pads/holes remain contained. No mechanical signoff."})
    print(board)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--allow-connector-overhang",action="store_true")
    parser.add_argument("--allow-mounting-overhang",action="store_true")
    prepare(parser.parse_args())
