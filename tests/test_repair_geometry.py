import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
import sexpdata

from pcb_weaver.repair_geometry import merge_repair, prepare_repair
from pcb_weaver import repair_geometry


EXAMPLE = Path(__file__).resolve().parents[1] / "examples/two-layer/two-layer.kicad_pcb"
REGION = [30, 25, 50, 45]


def parse(text):
    return sexpdata.loads(text, nil=None, true=None, false=None)


def tag(node):
    return str(node[0]) if isinstance(node, list) and node else ""


def children(node, name):
    return [n for n in node[1:] if tag(n) == name]


def field(node, name):
    return children(node, name)[0]


def segment(identity="remove", start=(32, 28), end=(36, 28), width=0.4, net=2, extra=""):
    return parse(f'(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) '
                 f'(width {width}) (layer "F.Cu") (net {net}) (uuid "{identity}") {extra})')


def via(identity="via", at=(38, 32), size=0.8, net=2, extra=""):
    return parse(f'(via (at {at[0]} {at[1]}) (size {size}) (drill 0.3) '
                 f'(layers "F.Cu" "B.Cu") (net {net}) (uuid "{identity}") {extra})')


def write(tmp_path, ast, name="source.kicad_pcb"):
    path = tmp_path / name
    path.write_text(sexpdata.dumps(ast), encoding="utf-8")
    return path


def load(path):
    return parse(path.read_text(encoding="utf-8-sig"))


def fixture_ast():
    ast = load(EXAMPLE)
    ast.extend([segment(), via(),
                segment("retained", start=(40, 36), end=(44, 36)),
                segment("outside", start=(55, 30), end=(60, 30)),
                segment("other-net", start=(33, 40), end=(37, 40), net=1),
                via("locked", at=(46, 40), extra="(locked yes)")])
    return ast


def prepare(tmp_path, ast=None, **kwargs):
    source = write(tmp_path, fixture_ast() if ast is None else ast)
    target, empty = tmp_path / "target.kicad_pcb", tmp_path / "empty.kicad_pcb"
    manifest = prepare_repair(source, target, empty, kwargs.get("nets", ["VIN"]),
                              kwargs.get("region", REGION), kwargs.get("remove_ids", ["remove", "via"]))
    return source, target, empty, manifest


def test_prepare_preserves_all_source_ast_except_explicit_copper(tmp_path):
    ast = fixture_ast()
    ast.insert(3, parse('(future_metadata (text "unchanged") (flag false) (value nil))'))
    source, target, empty, manifest = prepare(tmp_path, ast)
    original = source.read_bytes()
    removed = {"remove", "via"}
    assert load(target) == [n for n in ast if not (tag(n) in {"segment", "via"} and str(field(n, "uuid")[1]) in removed)]
    assert load(empty) == [n for n in ast if tag(n) not in {"segment", "via", "arc"}]
    assert source.read_bytes() == original
    assert manifest["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest == json.loads(json.dumps(manifest))
    assert manifest["nets"] == ["VIN"]
    assert manifest["region"] == REGION
    assert manifest["remove_ids"] == ["remove", "via"]
    assert [r["uuid"] for r in manifest["removed_geometry"]] == ["remove", "via"]
    assert parse(manifest["removed_geometry"][0]["ast"]) == segment()
    assert manifest["removed_geometry"][0]["bounds"] == pytest.approx([31.8, 27.8, 36.2, 28.2])
    assert {p.name for p in tmp_path.iterdir()} == {"source.kicad_pcb", "target.kicad_pcb", "empty.kicad_pcb"}


def test_merge_ignores_native_pad_electrical_and_retained_copper_edits(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    original = source.read_bytes()
    native = load(target)
    fp = children(native, "footprint")[0]
    field(fp, "at")[1:] = [999, 999, 180]
    pad = children(fp, "pad")[0]
    field(pad, "net")[1:] = [1, "GND"]
    field(pad, "size")[1:] = [8, 9]
    fp.append(parse('(property "native-only" "discard")'))
    field(native, "setup").append(parse('(pad_to_paste_clearance 12)'))
    native = [n for n in native if tag(n) not in {"segment", "via"}]
    native.extend([segment("new", start=(34, 30), end=(38, 30)), via("new-via", at=(38, 30))])
    routed = write(tmp_path, native, "routed.kicad_pcb")
    routed_bytes = routed.read_bytes()
    output = tmp_path / "merged.kicad_pcb"
    report = merge_repair(source, routed, output, json.loads(json.dumps(manifest)))
    merged = load(output)
    assert merged[:-2] == load(target)
    assert children(merged, "footprint") == children(load(source), "footprint")
    assert field(merged, "setup") == field(load(source), "setup")
    assert report["added"] == report["removed"] == 2
    assert report["preservation"] == {
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "noncopper_ast_preserved": True, "retained_copper_ast_preserved": True,
        "retained_copper_count": 4, "changed_copper_within_region": True,
    }
    assert source.read_bytes() == original
    assert routed.read_bytes() == routed_bytes


def test_merge_drops_outside_and_unselected_and_deduplicates_fixed_wires(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    native = load(target)
    native.extend([
        segment("fixed-copy", start=(44, 36), end=(40, 36)),
        via("locked-copy", at=(46, 40)),
        segment("accepted", start=(33, 31), end=(38, 31)),
        segment("accepted-copy", start=(38, 31), end=(33, 31)),
        segment("out", start=(52, 29), end=(55, 29)),
        segment("radius-out", start=(30.1, 29), end=(33, 29)),
        via("via-radius-out", at=(49.8, 30)),
        segment("unselected", start=(32, 33), end=(36, 33), net=1),
    ])
    routed = write(tmp_path, native, "routed.kicad_pcb")
    output = tmp_path / "merged.kicad_pcb"
    report = merge_repair(source, routed, output, manifest)
    assert report["added"] == 1
    assert report["removed"] == 2
    assert report["deduplicated"] == 7
    assert report["dropped_outside_region"] == 3
    assert report["dropped_unselected_nets"] == 1
    assert load(output)[:-1] == load(target)


def test_native_net_codes_remapped_by_name(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    native = load(target)
    for node in children(native, "net"):
        if node[1]:
            node[1] += 10
    for node in children(native, "segment") + children(native, "via"):
        field(node, "net")[1] += 10
    native.extend([segment("new", start=(32, 32), end=(36, 32), net=12),
                   via("named", at=(36, 32), net='"VIN"')])
    output = tmp_path / "merged.kicad_pcb"
    report = merge_repair(source, write(tmp_path, native, "routed.kicad_pcb"), output, manifest)
    assert report["added"] == 2
    assert [field(n, "net")[1] for n in load(output)[-2:]] == [2, 2]
    assert children(load(output), "net") == children(load(source), "net")


def test_uuid_collisions_missing_and_duplicate_native_ids_are_replaced(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    native = load(target)
    fp_id = str(field(children(native, "footprint")[0], "tstamp")[1])
    native.extend([segment("retained", start=(33, 31), end=(38, 31)),
                   segment(fp_id, start=(33, 32), end=(38, 32)),
                   segment("same-new", start=(33, 33), end=(38, 33)),
                   segment("same-new", start=(33, 34), end=(38, 34))])
    no_id = via(at=(38, 34))
    no_id.remove(field(no_id, "uuid"))
    native.append(no_id)
    output = tmp_path / "merged.kicad_pcb"
    report = merge_repair(source, write(tmp_path, native, "routed.kicad_pcb"), output, manifest)
    assert report["added"] == 5
    assert report["regenerated_uuids"] == 4
    ids = [str(field(n, "uuid")[1]) for n in load(output)[-5:]]
    assert len(set(ids)) == 5
    for index in [0, 1, 3, 4]:
        assert str(UUID(ids[index])) == ids[index]
    assert load(output)[:-5] == load(target)


@pytest.mark.parametrize("kwargs,match", [
    ({"nets": []}, "Nets"), ({"nets": ["VIN", "VIN"]}, "Nets"),
    ({"nets": ["MISSING"]}, "Unknown"), ({"nets": [""]}, "Nets"),
    ({"nets": [str(n) for n in range(9)]}, "Nets"),
    ({"nets": "VIN"}, "Nets"), ({"nets": [True]}, "Nets"),
    ({"region": [30, 25, 30, 45]}, "positive"),
    ({"region": [50, 25, 30, 45]}, "positive"),
    ({"region": [19, 25, 50, 45]}, "outline"),
    ({"region": [30, 25, 80, 45]}, "outline"),
    ({"region": [30, 25, 50]}, "Region"),
    ({"region": [30, 25, float("nan"), 45]}, "Non-finite"),
    ({"region": [30, 25, float("inf"), 45]}, "Non-finite"),
    ({"region": [True, 25, 50, 45]}, "numeric"),
    ({"region": ["30", 25, 50, 45]}, "numeric"),
    ({"remove_ids": ["remove", "remove"]}, "Duplicate"),
    ({"remove_ids": ["missing"]}, "Unknown"),
    ({"remove_ids": ["locked"]}, "Locked"),
    ({"remove_ids": ["outside"]}, "boundary"),
    ({"remove_ids": ["other-net"]}, "unselected"),
    ({"remove_ids": [str(n) for n in range(1001)]}, "1000"),
    ({"remove_ids": [None]}, "UUID"),
])
def test_invalid_requests_fail_without_outputs(tmp_path, kwargs, match):
    with pytest.raises(ValueError, match=match):
        prepare(tmp_path, **kwargs)
    assert not (tmp_path / "target.kicad_pcb").exists()
    assert not (tmp_path / "empty.kicad_pcb").exists()


@pytest.mark.parametrize("node", [
    segment(start=(30.1, 30), end=(40, 30)),
    segment(start=(32, 25.1), end=(40, 30)),
    segment(start=(32, 30), end=(49.9, 30)),
    segment(start=(32, 30), end=(40, 44.9)),
    via(identity="remove", at=(30.3, 30)),
    via(identity="remove", at=(49.7, 30)),
    via(identity="remove", at=(35, 25.3)),
    via(identity="remove", at=(35, 44.7)),
])
def test_whole_copper_envelope_required_for_removal(tmp_path, node):
    ast = load(EXAMPLE) + [node]
    with pytest.raises(ValueError, match="boundary"):
        prepare(tmp_path, ast, remove_ids=["remove"])


def test_envelope_exactly_touching_boundary_is_allowed(tmp_path):
    ast = load(EXAMPLE) + [segment(start=(30.5, 25.5), end=(49.5, 44.5), width=1),
                           via(at=(49.5, 25.5), size=1)]
    source, _, empty, manifest = prepare(tmp_path, ast)
    native = load(empty) + [segment("new", start=(30.5, 25.5), end=(49.5, 44.5), width=1)]
    report = merge_repair(source, write(tmp_path, native, "routed.kicad_pcb"), tmp_path / "merged.kicad_pcb", manifest)
    assert report["added"] == 1


@pytest.mark.parametrize("lock", ["locked", "(locked)", "(locked yes)", "(locked true)", "(locked 1)"])
def test_all_locked_encodings_rejected(tmp_path, lock):
    with pytest.raises(ValueError, match="Locked"):
        prepare(tmp_path, load(EXAMPLE) + [segment(extra=lock)], remove_ids=["remove"])


@pytest.mark.parametrize("lock", ["(locked no)", "(locked false)", "(locked 0)"])
def test_unlocked_encodings_supported(tmp_path, lock):
    prepare(tmp_path, load(EXAMPLE) + [segment(extra=lock)], remove_ids=["remove"])


@pytest.mark.parametrize("text,match", [
    ('(arc (start 31 30) (mid 32 31) (end 33 30) (width 0.2) (layer "F.Cu") (net 2))', "Unsupported"),
    ('(zone (net 2) (layer "F.Cu"))', "Unsupported"),
    ('(rule_area (layer "F.Cu"))', "Unsupported"),
    ('(gr_line (start 31 30) (end 35 30) (width 0.2) (layer "F.Cu"))', "Unsupported"),
    ('(mystery_copper (layer "B.Cu") (net 2))', "Unsupported"),
    ('(mystery_copper (net 2))', "Unsupported"),
    ('(group "fixed" (members "remove"))', "Unsupported"),
    ('(generated (members "remove"))', "Unsupported"),
    ('(gr_circle (center 45 35) (end 46 35) (layer "Edge.Cuts"))', "outline"),
])
def test_unsupported_board_geometry_rejected_in_prepare_and_merge(tmp_path, text, match):
    source, target, _, manifest = prepare(tmp_path)
    bad = load(target) + [parse(text)]
    routed = write(tmp_path, bad, "routed.kicad_pcb")
    with pytest.raises(ValueError, match=match):
        merge_repair(source, routed, tmp_path / "merged.kicad_pcb", manifest)
    with pytest.raises(ValueError, match=match):
        prepare_repair(routed, tmp_path / "bad-target.kicad_pcb", tmp_path / "bad-empty.kicad_pcb", ["VIN"], REGION, [])
    assert not (tmp_path / "merged.kicad_pcb").exists()


@pytest.mark.parametrize("node", [
    segment(width=0), segment(width=-1), segment(width="nan"),
    segment(start=("inf", 30)), segment(start=("oops", 30)),
    segment(start=(32, 28), end=(32, 28)), segment(net=0), segment(net=99),
    segment(extra="(width 0.8)"), segment(extra="(net 1)"),
    segment(extra="(locked maybe)"), segment(extra="(future_geometry 2)"),
    segment(extra="(tstamp duplicate)"), via(size=0.1), via(size="inf"),
    via(extra="blind"), via(extra="micro"), via(extra="(remove_unused_layers yes)"),
    via(extra="(padstack (layer \"F.Cu\" (size 2 2)))"),
])
def test_invalid_copper_rejected_even_outside_selection(tmp_path, node):
    with pytest.raises(ValueError):
        prepare(tmp_path, load(EXAMPLE) + [node], remove_ids=[])
    assert not (tmp_path / "target.kicad_pcb").exists()


@pytest.mark.parametrize("edit", [
    lambda a: a.append(segment()),
    lambda a: a.append(parse('(net 2 "VIN")')),
    lambda a: a.append(parse('(net 7 "VIN")')),
    lambda a: a.append(parse('(net 2 "OTHER")')),
    lambda a: field(children(a, "segment")[0], "uuid").pop(),
    lambda a: children(a, "segment")[0].remove(field(children(a, "segment")[0], "uuid")),
    lambda a: children(a, "footprint")[0].append(parse('(fp_line (start 0 0) (end 1 1) (layer "F.Cu"))')),
    lambda a: children(a, "footprint")[0].append(parse('(fp_rect (start 0 0) (end 1 1) (layer "Edge.Cuts"))')),
    lambda a: children(children(a, "footprint")[0], "pad")[0].__setitem__(3, sexpdata.Symbol("custom")),
    lambda a: field(children(children(a, "footprint")[0], "pad")[0], "size").__setitem__(1, float("nan")),
])
def test_ambiguous_or_unsupported_source_ast_rejected(tmp_path, edit):
    ast = fixture_ast()
    edit(ast)
    with pytest.raises(ValueError):
        prepare(tmp_path, ast)


@pytest.mark.parametrize("key,value", [
    ("version", 2), ("version", True), ("source_sha256", "0" * 64),
    ("nets", ["VIN", "GND"]), ("region", [29, 24, 51, 46]),
    ("remove_ids", ["retained"]), ("removed_geometry", []),
    ("manifest_sha256", "0" * 64), ("unexpected", "field"),
])
def test_manifest_tampering_rejected(tmp_path, key, value):
    source, target, _, manifest = prepare(tmp_path)
    manifest[key] = value
    with pytest.raises(ValueError, match="[Mm]anifest"):
        merge_repair(source, target, tmp_path / "merged.kicad_pcb", manifest)
    assert not (tmp_path / "merged.kicad_pcb").exists()


def test_removed_geometry_revalidated_even_with_recomputed_checksum(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    manifest["removed_geometry"][0]["width"] = 1.8
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(ValueError, match="Manifest"):
        merge_repair(source, target, tmp_path / "merged.kicad_pcb", manifest)


def test_source_hash_is_over_exact_bytes(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="source hash"):
        merge_repair(source, target, tmp_path / "merged.kicad_pcb", manifest)


@pytest.mark.parametrize("which", ["source", "target", "empty", "same-output"])
def test_prepare_rejects_existing_or_alias_destinations_without_partial_write(tmp_path, which):
    source = write(tmp_path, fixture_ast())
    before = source.read_bytes()
    target, empty = tmp_path / "target.kicad_pcb", tmp_path / "empty.kicad_pcb"
    if which == "source":
        target = source
    elif which == "target":
        target.write_text("keep")
    elif which == "empty":
        empty.write_text("keep")
    else:
        empty = target
    with pytest.raises(ValueError):
        prepare_repair(source, target, empty, ["VIN"], REGION, [])
    assert source.read_bytes() == before
    for p in (target, empty):
        if p.exists() and p != source:
            assert p.read_text() == "keep"


def test_hardlinks_rejected_for_output_and_routed_input(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    alias = tmp_path / "alias.kicad_pcb"
    alias.hardlink_to(source)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="alias"):
        merge_repair(source, alias, tmp_path / "merged.kicad_pcb", manifest)
    with pytest.raises(ValueError, match="alias"):
        merge_repair(source, target, alias, manifest)
    assert source.read_bytes() == before


@pytest.mark.parametrize("destination", ["source", "routed", "existing"])
def test_merge_never_overwrites(tmp_path, destination):
    source, target, _, manifest = prepare(tmp_path)
    existing = tmp_path / "existing.kicad_pcb"
    existing.write_text("keep")
    output = {"source": source, "routed": target, "existing": existing}[destination]
    before = output.read_bytes()
    with pytest.raises(ValueError):
        merge_repair(source, target, output, manifest)
    assert output.read_bytes() == before


def test_empty_removal_list_and_no_additions_preserve_exact_ast(tmp_path):
    source, target, _, manifest = prepare(tmp_path, remove_ids=[])
    output = tmp_path / "merged.kicad_pcb"
    report = merge_repair(source, target, output, manifest)
    assert load(output) == load(source)
    assert report["added"] == report["removed"] == 0
    assert report["deduplicated"] == 6


def test_legacy_tstamp_and_named_copper_net_supported(tmp_path):
    ast = load(EXAMPLE) + [segment(net='"VIN"')]
    field(ast[-1], "uuid")[0] = sexpdata.Symbol("tstamp")
    source, _, _, manifest = prepare(tmp_path, ast, remove_ids=["remove"])
    assert manifest["removed_geometry"][0]["net"] == "VIN"
    assert load(source) == ast


def test_closed_four_edge_outline_supported_but_cutouts_rejected(tmp_path):
    ast = fixture_ast()
    ast = [n for n in ast if tag(n) != "gr_rect"]
    for a, b in [((20, 20), (70, 20)), ((70, 20), (70, 55)), ((70, 55), (20, 55)), ((20, 55), (20, 20))]:
        ast.append(parse(f'(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "Edge.Cuts"))'))
    source, _, _, _ = prepare(tmp_path, ast)
    ast.append(parse('(gr_rect (start 38 31) (end 42 35) (layer "Edge.Cuts"))'))
    cutout = write(tmp_path, ast, "cutout.kicad_pcb")
    with pytest.raises(ValueError, match="outline"):
        prepare_repair(cutout, tmp_path / "cut-target.kicad_pcb", tmp_path / "cut-empty.kicad_pcb", ["VIN"], REGION, [])
    assert load(source) != ast


def test_native_invalid_unselected_copper_fails_closed(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    native = load(target) + [segment("bad", start=(55, 30), end=(60, 30), net=0)]
    with pytest.raises(ValueError, match="no-net"):
        merge_repair(source, write(tmp_path, native, "routed.kicad_pcb"), tmp_path / "merged.kicad_pcb", manifest)


def test_source_existing_duplicates_preserved_but_not_reimported(tmp_path):
    ast = fixture_ast() + [segment("original-duplicate", start=(44, 36), end=(40, 36))]
    source, target, _, manifest = prepare(tmp_path, ast)
    output = tmp_path / "merged.kicad_pcb"
    report = merge_repair(source, target, output, manifest)
    assert load(output) == load(target)
    assert report["added"] == 0
    assert report["preservation"]["retained_copper_count"] == 5


def test_sub_float_resolution_protrusion_is_rejected(tmp_path):
    tiny = segment(start=(30, 30), end=(40, 30), width=1e-20)
    with pytest.raises(ValueError, match="boundary"):
        prepare(tmp_path, load(EXAMPLE) + [tiny], remove_ids=["remove"])


def test_sub_float_resolution_native_protrusion_is_dropped(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    native = load(target) + [segment("tiny", start=(40, 30), end=(50, 30), width=1e-20)]
    output = tmp_path / "merged.kicad_pcb"
    report = merge_repair(source, write(tmp_path, native, "routed.kicad_pcb"), output, manifest)
    assert report["added"] == 0
    assert report["dropped_outside_region"] == 1
    assert load(output) == load(target)


def test_destination_race_cleans_only_our_reserved_file(tmp_path, monkeypatch):
    source = write(tmp_path, fixture_ast())
    before = source.read_bytes()
    target, empty = tmp_path / "target.kicad_pcb", tmp_path / "empty.kicad_pcb"
    original_open = Path.open

    def racing_open(path, mode="r", *args, **kwargs):
        if path == empty and mode == "xb":
            with original_open(path, "wb") as stream:
                stream.write(b"other writer")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    with pytest.raises(FileExistsError):
        prepare_repair(source, target, empty, ["VIN"], REGION, ["remove"])
    assert not target.exists()
    assert empty.read_bytes() == b"other writer"
    assert source.read_bytes() == before


@pytest.mark.parametrize("operation", ["prepare", "merge"])
def test_source_change_during_validation_prevents_write(tmp_path, monkeypatch, operation):
    source, target, _, manifest = prepare(tmp_path)
    original_serialize = repair_geometry._serialize

    def changed_source(ast):
        source.write_bytes(source.read_bytes() + b"\n")
        return original_serialize(ast)

    monkeypatch.setattr(repair_geometry, "_serialize", changed_source)
    with pytest.raises(ValueError, match="Source hash changed"):
        if operation == "prepare":
            prepare_repair(source, tmp_path / "new-target.kicad_pcb", tmp_path / "new-empty.kicad_pcb", ["VIN"], REGION, [])
        else:
            merge_repair(source, target, tmp_path / "merged.kicad_pcb", manifest)
    assert not (tmp_path / "new-target.kicad_pcb").exists()
    assert not (tmp_path / "new-empty.kicad_pcb").exists()
    assert not (tmp_path / "merged.kicad_pcb").exists()


def test_new_locked_native_copper_is_rejected(tmp_path):
    source, target, _, manifest = prepare(tmp_path)
    native = load(target) + [segment("new-locked", start=(33, 31), end=(38, 31), extra="locked")]
    with pytest.raises(ValueError, match="locked"):
        merge_repair(source, write(tmp_path, native, "routed.kicad_pcb"), tmp_path / "merged.kicad_pcb", manifest)


@pytest.mark.parametrize("change", ["nets", "layers"])
def test_native_incompatible_tables_fail_closed(tmp_path, change):
    source, target, _, manifest = prepare(tmp_path)
    native = load(target)
    if change == "nets":
        native.append(parse('(net 7 "EXTRA")'))
    else:
        field(native, "layers").extend([parse('(4 "In1.Cu" signal)'), parse('(6 "In2.Cu" signal)')])
    with pytest.raises(ValueError, match="differ"):
        merge_repair(source, write(tmp_path, native, "routed.kicad_pcb"), tmp_path / "merged.kicad_pcb", manifest)


def test_malformed_board_does_not_create_outputs(tmp_path):
    source = tmp_path / "malformed.kicad_pcb"
    source.write_text('(kicad_pcb (net 1 "VIN")')
    with pytest.raises(ValueError, match="S-expression"):
        prepare_repair(source, tmp_path / "target.kicad_pcb", tmp_path / "empty.kicad_pcb", ["VIN"], REGION, [])
    assert not (tmp_path / "target.kicad_pcb").exists()


def test_serializer_prepares_native_size_160_footprint_board(tmp_path):
    from copy import deepcopy

    ast = load(EXAMPLE)
    template = children(ast, "footprint")[0]
    ast = [n for n in ast if tag(n) != "footprint"]
    for index in range(160):
        footprint = deepcopy(template)
        field(footprint, "tstamp")[1] = f"fixture-{index}"
        for label in children(footprint, "fp_text"):
            if str(label[1]) == "reference":
                label[2] = f"J{index}"
        footprint.append([sexpdata.Symbol("property"), "large-fixture-note", "x" * 7200])
        ast.append(footprint)
    assert len(sexpdata.dumps(ast).encode("utf-8")) > 1_172_664
    source, target, empty, _ = prepare(tmp_path, ast, remove_ids=[])
    for output in (target, empty):
        payload = output.read_bytes()
        assert len(payload.splitlines()) > 160
        assert max(map(len, payload.split(b"\n"))) < 64 * 1024
        assert load(output) == ast
    assert load(source) == ast


def test_serializer_wraps_large_nested_and_flat_lists_without_splitting_atoms():
    flat = [sexpdata.Symbol("members"), *[f"member-{i:05d}" for i in range(12000)]]
    nested = [sexpdata.Symbol("pts"), *[[sexpdata.Symbol("xy"), i, i + 1] for i in range(12000)]]
    ast = [sexpdata.Symbol("kicad_pcb"), [sexpdata.Symbol("metadata"), flat, nested]]
    payload = repair_geometry._serialize(ast)
    assert max(map(len, payload.split(b"\n"))) < 64 * 1024
    assert parse(payload.decode("utf-8")) == ast


def test_serializer_preserves_escaped_string_whitespace_and_symbols():
    value = '  spaces  \t tab\nnewline\rreturn \\n literal "quote" (paren)  '
    ast = [sexpdata.Symbol("kicad_pcb"),
           [sexpdata.Symbol("property"), "escaped", value],
           [sexpdata.Symbol("metadata"), sexpdata.Symbol("nil"), sexpdata.Symbol("false"),
            sexpdata.Symbol("true"), "nil", "false", "1e-20", []]]
    payload = repair_geometry._serialize(ast)
    assert sexpdata.dumps(value).encode("utf-8") in payload
    assert parse(payload.decode("utf-8")) == ast


@pytest.mark.parametrize("value", [1e-20, -1e-20, 5e-324, 1e20, 1.7976931348623157e308, -0.0, 0.0])
def test_serializer_uses_plain_decimal_and_preserves_float_identity(value):
    from decimal import Decimal

    ast = [sexpdata.Symbol("kicad_pcb"), [sexpdata.Symbol("value"), value]]
    payload = repair_geometry._serialize(ast)
    expected = format(Decimal(repr(value)), "f")
    expected += ".0" if "." not in expected else ""
    assert f"(value {expected})".encode() in payload
    restored = parse(payload.decode("utf-8"))[1][1]
    assert type(restored) is float
    assert restored.hex() == value.hex()


def test_serializer_enforces_byte_limit_for_utf8_and_rejects_oversized_string():
    # Construct Unicode explicitly so this source remains ASCII.
    text = chr(0x754C) * 12000
    ast = [sexpdata.Symbol("kicad_pcb"), [sexpdata.Symbol("metadata"), text, text]]
    payload = repair_geometry._serialize(ast)
    assert max(map(len, payload.split(b"\n"))) < 64 * 1024
    assert parse(payload.decode("utf-8")) == ast
    with pytest.raises(ValueError, match="atom.*line limit"):
        repair_geometry._serialize([sexpdata.Symbol("kicad_pcb"), text * 2])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_serializer_rejects_nonfinite_floats(value):
    with pytest.raises(ValueError, match="Non-finite"):
        repair_geometry._serialize([sexpdata.Symbol("kicad_pcb"), [sexpdata.Symbol("value"), value]])


def test_serializer_roundtrip_proof_rejects_type_changing_atoms():
    with pytest.raises(ValueError, match="without changes"):
        repair_geometry._serialize([sexpdata.Symbol("kicad_pcb"), True])
