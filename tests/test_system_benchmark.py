"""Frozen real-design evidence tests, distinct from a live route/production pass."""
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
import sexpdata

from pcb_weaver.board import read_board
from pcb_weaver.compiler import project_rule_issues
from pcb_weaver.models import Constraints
from pcb_weaver.netlist import inspect_netlist
from pcb_weaver.service import design_files


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "examples/system-controller"
NAME = "kit-dev-coldfire-xilinx_5213"
EVIDENCE = ROOT / "benchmarks/results/system-controller-frozen"
ORIGINAL = ROOT / "benchmarks/results/upstream-9.0.0"
pytestmark = pytest.mark.skipif(
    not EVIDENCE.is_dir() or not ORIGINAL.is_dir(),
    reason="Historical native benchmark evidence is distributed separately from public source",
)


def nodes(tree, tag):
    return [n for n in tree if isinstance(n, list) and n and str(n[0]) == tag]


def load(path):
    return sexpdata.loads(path.read_text(encoding="utf-8-sig"), nil=None, true=None, false=None)


@pytest.fixture(scope="module")
def design():
    return read_board(PROJECT / (NAME + ".kicad_pcb"))


@pytest.fixture(scope="module")
def provenance():
    return json.loads((PROJECT / "PROVENANCE.json").read_text())


def test_scale_is_real_components_pads_and_connected_nets(design):
    assert len(design["footprints"]) == 160
    assert sum(len(fp["pads"]) for fp in design["footprints"]) == 825
    identities = {(fp["reference"], p["number"]) for fp in design["footprints"] for p in fp["pads"] if p["number"]}
    assert len(identities) == 807
    assert len(design["nets"]) == 278
    assert sum(len(net["pads"]) > 1 for net in design["nets"]) >= 40


def test_four_layer_real_stackup_and_rectangle(design):
    assert design["copper_layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert design["outline"]["supported"]
    assert design["outline"]["bounds"] == [64, 31, 234, 153]


def test_diverse_packages_and_real_active_devices(design):
    fps = {fp["reference"]: fp for fp in design["footprints"]}
    assert len({fp["footprint"] for fp in fps.values()}) == 35
    assert "LQFP-100" in fps["U102"]["footprint"]
    assert "TQFP-144" in fps["U301"]["footprint"]
    assert "SOIC-16" in fps["U202"]["footprint"]
    assert "SOIC-8" in fps["U205"]["footprint"]
    assert "SOT23" in fps["Q101"]["footprint"]
    assert "MCF5213" in fps["U102"]["value"]
    assert "XCR3256" in fps["U301"]["value"]
    assert fps["U205"]["value"] == "PCA82C251"


def test_no_inherited_routing_or_copper_remains(design, provenance):
    assert design["tracks"] == design["vias"] == 0
    ast = load(PROJECT / (NAME + ".kicad_pcb"))
    assert not any(nodes(ast, name) for name in ("segment", "via", "arc", "zone"))
    for node in ast[1:]:
        if isinstance(node, list) and str(node[0]).startswith("gr_"):
            layer = nodes(node, "layer")
            assert not layer or not str(layer[0][1]).endswith(".Cu")
    removed = provenance["removed_copper_objects"]
    assert {key: removed[key] for key in ("segment", "via", "zone", "gr_text")} == {
        "segment": 2935, "via": 253, "zone": 3, "gr_text": 5}


def test_footprints_positions_sides_and_pad_assignments_unchanged():
    before = load(ORIGINAL / "design" / (NAME + ".kicad_pcb"))
    after = load(PROJECT / (NAME + ".kicad_pcb"))
    assert nodes(before, "footprint") == nodes(after, "footprint")
    assert nodes(before, "net") == nodes(after, "net")
    assert nodes(before, "layers") == nodes(after, "layers")
    assert nodes(before, "setup") == nodes(after, "setup")


def test_expanded_envelope_does_not_lock_connectors_outside_board(design, provenance):
    assert provenance["outline_change"]["before"] == [71.12, 55.88, 228.6, 147.32]
    bounds = design["outline"]["bounds"]
    for fp in design["footprints"]:
        box = fp["bounds"]
        assert box[0] - bounds[0] >= 0.5 and box[1] - bounds[1] >= 0.5
        assert bounds[2] - box[2] >= 0.5 and bounds[3] - box[3] >= 0.5


def strip_metadata(tree):
    if not isinstance(tree, list):
        return tree
    return [strip_metadata(item) for item in tree if not (
        isinstance(item, list) and len(item) >= 3 and str(item[0]) == "property"
        and str(item[1]) in {"Footprint", "ki_fp_filters"})]


@pytest.mark.parametrize("name", [NAME + ".kicad_sch", "in_out_conn.kicad_sch", "xilinx.kicad_sch", NAME + ".kicad_sym"])
def test_no_schematic_wires_pin_types_or_values_modified(name):
    before = load(ORIGINAL / "design" / name)
    after = load(PROJECT / name)
    assert strip_metadata(before) == strip_metadata(after)


def test_filters_add_only_actual_package_names_not_wildcards(design, provenance):
    packages = {fp["footprint"].split(":", 1)[1] for fp in design["footprints"]}
    changes = provenance["explicit_footprint_filter_updates"]
    assert changes
    for change in changes:
        before, after = change["before"].split(), change["after"].split()
        assert after[:len(before)] == before
        for name in after[len(before):]:
            assert name in packages
            assert "*" not in name and "?" not in name


def test_real_electrical_types_not_all_passive():
    root = ET.parse(EVIDENCE / "netlist.xml").getroot()
    types = Counter(pin.get("type") for pin in root.findall("./libparts/libpart/pins/pin"))
    for kind in ("input", "output", "bidirectional", "tri_state", "power_in", "power_out", "open_collector"):
        assert types[kind] > 0
    assert types["passive"] < sum(types.values())


def xml_nets(path):
    root = ET.parse(path).getroot()
    return {net.get("name"): sorted((n.get("ref"), n.get("pin")) for n in net.findall("node"))
            for net in root.findall("./nets/net")}


def test_full_electrical_topology_matches_original_native_export():
    assert xml_nets(EVIDENCE / "netlist.xml") == xml_nets(ORIGINAL / "netlist.xml")


def test_connected_multi_module_design_has_no_isolated_padding_components():
    root = ET.parse(EVIDENCE / "netlist.xml").getroot()
    modules = {c.get("ref"): c.find("sheetpath").get("names") for c in root.findall("./components/comp")}
    assert Counter(modules.values()) == {"/": 66, "/inout_user/": 73, "/xilinx/": 21}
    graph = defaultdict(set)
    cross = 0
    for net in root.findall("./nets/net"):
        refs = {n.get("ref") for n in net.findall("node")}
        cross += len({modules[ref] for ref in refs}) > 1
        for ref in refs:
            graph[ref].update(refs - {ref})
    assert cross == 56
    assert set(graph) == set(modules) and all(graph.values())
    pending, reached = ["U102"], set()
    while pending:
        ref = pending.pop()
        if ref not in reached:
            reached.add(ref)
            pending.extend(graph[ref] - reached)
    assert reached == set(modules)


def test_no_check_disabled_or_waiver_added():
    before = json.loads((ORIGINAL / "design" / (NAME + ".kicad_pro")).read_text())
    after = json.loads((PROJECT / (NAME + ".kicad_pro")).read_text())
    ranks = {"ignore": 0, "warning": 1, "error": 2}
    for a, b in ((before["board"]["design_settings"], after["board"]["design_settings"]), (before["erc"], after["erc"])):
        assert "ignore" not in b["rule_severities"].values()
        assert not b.get("drc_exclusions") and not b.get("erc_exclusions")
        assert all(ranks[b["rule_severities"][key]] >= ranks[value] for key, value in a["rule_severities"].items())
        aa, bb = deepcopy(a), deepcopy(b)
        aa.pop("rule_severities")
        bb.pop("rule_severities")
        assert aa == bb
    assert project_rule_issues(PROJECT / (NAME + ".kicad_pcb")) == []


def test_project_tables_and_all_native_libraries_in_snapshot():
    files = design_files(PROJECT)
    engineering = {name for name in files if Path(name).suffix in {".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_sym", ".kicad_mod"}
                   or name in {"fp-lib-table", "sym-lib-table"}}
    assert len(engineering) == 44
    assert {"PROVENANCE.json", "LICENSE.KiCad.README"} <= set(files)
    assert {"fp-lib-table", "sym-lib-table", NAME + ".kicad_sym"} <= set(files)
    for file in PROJECT.rglob("*.kicad_mod"):
        assert file.relative_to(PROJECT).as_posix() in files
    for name in ("fp-lib-table", "sym-lib-table"):
        for lib in nodes(load(PROJECT / name), "lib"):
            assert str(nodes(lib, "uri")[0][1]).startswith("${KIPRJMOD}/")


def test_native_evidence_is_hashed_and_bound_to_frozen_design():
    baseline = json.loads((EVIDENCE / "baseline.json").read_text())
    assert baseline["kicad_version"] == "9.0.9"
    assert baseline["source_unchanged"] and baseline["engine_snapshot_unchanged"]
    for name, digest in baseline["source_hashes"].items():
        assert hashlib.sha256((PROJECT / name).read_bytes()).hexdigest() == digest
    for name, digest in baseline["artifacts"].items():
        assert hashlib.sha256((EVIDENCE / name).read_bytes()).hexdigest() == digest
    assert all(command["returncode"] in (0, 5) for command in baseline["commands"])
    drc = next(command for command in baseline["commands"] if command["kind"] == "drc")
    assert "--severity-all" in drc["argv"] and "--schematic-parity" in drc["argv"]


def test_native_baseline_is_unrouted_not_falsely_a_release():
    baseline = json.loads((EVIDENCE / "baseline.json").read_text())
    assert baseline["checks"]["erc"]["by_severity"] == {"warning": 16}
    assert baseline["checks"]["drc"]["unconnected"] == 499
    assert baseline["checks"]["drc"]["parity"] == 0
    assert baseline["checks"]["drc"]["by_severity"] == {"warning": 47, "error": 499}
    assert baseline["checks"]["erc"]["excluded"] == baseline["checks"]["drc"]["excluded"] == 0


def test_adapter_parity_has_no_undocumented_gap(design):
    result = inspect_netlist(EVIDENCE / "netlist.xml", design)
    assert not result["component_differences"] and not result["missing_references"]
    # The original board exercises KiCad's escaped literal slash. Until the core
    # adapter normalizes it, this is a documented blocker, NOT a service pass.
    if result["status"] != "passed":
        assert len(result["differences"]) == 10
        for item in result["differences"]:
            assert "{slash}" in item["board_net"]
            assert item["board_net"].replace("{slash}", "/") == item["schematic_net"]


def test_constraint_schema_and_fixed_references_are_valid(design):
    constraints = Constraints.model_validate_json((PROJECT / "constraints.json").read_text())
    assert constraints.board.layers == 4 and constraints.release.require_erc is True
    assert set(constraints.fixed_references) <= {fp["reference"] for fp in design["footprints"]}
    assert constraints.fabrication.min_track_mm == 0.2


def test_source_license_and_modification_notice_are_explicit(provenance):
    assert provenance["upstream_commit"] == "286b0611feca00727bf70bfa184ec2c28a745dc3"
    assert provenance["license"] == "CC-BY-SA-4.0"
    assert "Not an original reference circuit" in provenance["derivative_notice"]
    assert "All the demo files provided in demos/*" in (PROJECT / "LICENSE.KiCad.README").read_text()
