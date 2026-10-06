"""AST, malformed-input and exclusive-write tests; no router invocation implied."""

from copy import deepcopy
import hashlib
from pathlib import Path
import re

import pytest
import sexpdata

from pcb_weaver.repair_dsn import protect_dsn


DSN = '''(pcb "board.dsn"
  (parser (string_quote ") (space_in_quoted_tokens on)
    (host_cad "KiCad's Pcbnew") (host_version "9.0.0"))
  (resolution um 10) (unit um)
  (structure
    (layer F.Cu (type signal) (property (index 0)))
    (layer B.Cu (type signal) (property (index 1)))
    (boundary (path pcb 0 0 0 10000 0 10000 10000 0 10000 0 0))
    (via Via1)
    (rule (width 200) (clearance 200) (clearance 50 (type smd_smd)))
    (keepout "region (A)" (rect F.Cu 7000 7000 8000 8000)))
  (placement (component R0402 (place R1 1000 1000 front 0 (PN "10k"))))
  (library
    (image R0402 (pin Pad1 1 -500 0) (pin Pad1 2 500 0))
    (padstack Pad1 (shape (rect F.Cu -300 -250 300 250)) (attach off))
    (padstack Via1 (shape (circle F.Cu 600)) (shape (circle B.Cu 600)) (attach off)))
  (network (net GND (pins R1-1)) (net "Net-(R1-Pad2)" (pins R1-2))
    (class Default GND (circuit (use_via Via1)) (rule (width 600) (clearance 300)))
    (class "fine (class)" "Net-(R1-Pad2)" (rule (width 180) (clearance 125))))
  (wiring
    (wire (path F.Cu 600 1000 1000 2000 1000) (net GND) (type route))
    (wire (path B.Cu 180 2000 1000 3000 1000) (net "Net-(R1-Pad2)"))
    (via Via1 2000 1000 (net GND) (type fix))))
'''


def parse(text):
    # Independent AST oracle for this fixture's ordinary native atoms.
    normalized = text.replace('(string_quote ")', '(string_quote "double_quote")', 1)
    return sexpdata.loads(normalized, nil=None, true=None)


def child(tree, name):
    return next(n for n in tree if isinstance(n, list) and n and n[0] == sexpdata.Symbol(name))


def run(tmp_path, text=DSN):
    source, output = tmp_path / "source.dsn", tmp_path / "protected.dsn"
    source.write_text(text, encoding="utf-8")
    before = source.read_bytes()
    report = protect_dsn(source, output)
    assert source.read_bytes() == before
    return source, output, report


def test_full_ast_identity_except_wiring_type_and_report(tmp_path):
    source, output, report = run(tmp_path)
    expected = deepcopy(parse(DSN))
    for item in child(expected, "wiring")[1:]:
        states = [n for n in item if isinstance(n, list) and n and n[0] == sexpdata.Symbol("type")]
        if states:
            states[0][1] = sexpdata.Symbol("protect")
        else:
            item.append([sexpdata.Symbol("type"), sexpdata.Symbol("protect")])
    assert parse(output.read_text(encoding="utf-8")) == expected
    assert report["input_dsn_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["output_dsn_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert report["wire_count"] == 2 and report["via_count"] == 1
    assert report["protected_count"] == report["type_changed_count"] == 3
    assert report["ast_preserved_except_wiring_type"]
    assert report["source_bytes_unchanged_verified"]
    assert report["physical_rule_overrides"] is False
    evidence = report["fixed_state_evidence"]
    assert evidence["fixed_state"] == "USER_FIXED"
    assert evidence["version"] == "1.9.0"
    assert re.fullmatch("[0-9a-f]{40}", evidence["commit"])
    assert len(evidence["sources"]) == 7
    assert all(evidence["commit"] in s["url"] and s["reading_scope"] for s in evidence["sources"])
    assert "no routing/native-import execution" in evidence["verification"]


@pytest.mark.parametrize("state", ["route", "normal", "fix", "protect", "shove_fixed"])
def test_replace_existing_type_and_idempotent(tmp_path, state):
    _, output, report = run(tmp_path, DSN.replace("(type route)", f"(type {state})"))
    assert report["already_protected_count"] == (state == "protect")
    items = child(parse(output.read_text()), "wiring")[1:]
    assert all(sum(isinstance(n, list) and n and n[0] == sexpdata.Symbol("type") for n in item) == 1 for item in items)
    second = tmp_path / "second.dsn"
    again = protect_dsn(output, second)
    assert second.read_bytes() == output.read_bytes()
    assert again["already_protected_count"] == 3 and again["type_changed_count"] == 0


@pytest.mark.parametrize("name", ['Net-(U1-Pad(2))', 'quote " and \\ slash', 'literal (string_quote ")', 'double_quote'])
def test_quoted_names_escaping_and_parentheses(tmp_path, name):
    text = DSN.replace('"Net-(R1-Pad2)"', sexpdata.dumps(name))
    _, output, _ = run(tmp_path, text)
    before, after = parse(text), parse(output.read_text(encoding="utf-8"))
    assert child(before, "network") == child(after, "network")
    assert child(child(after, "wiring")[2], "net")[1] == name
    assert '(string_quote ")' in output.read_text()


def test_native_unquoted_punctuation_and_lisp_literals(tmp_path):
    text = DSN.replace("Pad1", "Rect[T]Pad_600x500").replace("Default", "class,A")
    text = text.replace("GND", "nil").replace("R0402", "t")
    _, output, _ = run(tmp_path, text)
    result = output.read_text()
    assert "Rect[T]Pad_600x500" in result and r"Rect\[T\]" not in result
    assert "class,A" in result and r"class\,A" not in result
    assert "(net nil" in result and "(component t" in result
    # Quote the punctuation-bearing atoms for an independent sexpdata oracle.
    def oracle(value):
        return parse(value.replace("Rect[T]Pad_600x500", '"Rect[T]Pad_600x500"').replace("class,A", '"class,A"'))
    before, after = oracle(text), oracle(result)
    for name in ("parser", "structure", "library", "placement", "network"):
        assert child(before, name) == child(after, name)


@pytest.mark.parametrize(("old", "new"), [
    ("(type route)", "(type route) (type fix)"),
    ("(type fix)", "(type protect) (type protect)"),
    ("(type route)", "(type)"),
    ("(type route)", "(type alien)"),
    ("(type route)", '(type "protect")'),
    ("(type route)", "(type fix protect)"),
    ("(type route)", "(locked yes)"),
    ("(net GND) (type route)", "(net GND) (net GND)"),
    ("(net GND) (type route)", ""),
    ("(net GND) (type route)", "(net missing)"),
    ("(net GND) (type route)", "(net GND 2)"),
    ("(path F.Cu 600 1000 1000 2000 1000)", "(qarc F.Cu 600 1000 1000 2000 1000)"),
    ("(path F.Cu 600 1000 1000 2000 1000)", "(rect F.Cu 0 0 10 10)"),
    ("(path F.Cu 600 1000 1000 2000 1000)", "(path F.Cu 600 1000 1000 2000)"),
    ("(path F.Cu 600", "(path Missing 600"),
    ("(path F.Cu 600", "(path F.Cu 0"),
    ("(path F.Cu 600", "(path F.Cu -1"),
    ("(path F.Cu 600", "(path F.Cu nan"),
    ("(via Via1 2000 1000", "(via Unknown 2000 1000"),
    ("(via Via1 2000 1000", "(via Via1 2000"),
    ("(via Via1 2000 1000", "(via Via1 2000 inf"),
    ("(via Via1 2000 1000", '(via Via1 "2000" 1000'),
    ("(wiring", "(wiring junk"),
    ("(wiring", "(wiring (polygon F.Cu 0 0 0 1 1)"),
    ("(wiring", "(wiring ()"),
    ("(wiring", "(wiring) (wiring"),
    ("(type signal)", "(type power)"),
    ("(layer B.Cu", "(layer F.Cu"),
    ("(layer B.Cu (type signal) (property (index 1)))", ""),
    ("(host_cad \"KiCad's Pcbnew\")", '(host_cad "Other")'),
    ("(host_version", "(generated_by_freerouting) (host_version"),
    ("(string_quote \")", "(string_quote ')") ,
    ("(string_quote \")", '(string_quote ") (string_quote ")'),
    ("(resolution um 10)", "(resolution mil 10)"),
    ("(width 200)", "(width -1)"),
    ("(width 600)", "(width bogus)"),
    ("(clearance 125)", "(clearance)"),
    ("(clearance 125)", "(clearance -1)"),
    ("(clearance 125)", "(clearance 125 junk)"),
])
def test_refuse_malformed_or_unsupported_without_writing(tmp_path, old, new):
    assert old in DSN
    source, output = tmp_path / "input.dsn", tmp_path / "output.dsn"
    source.write_text(DSN.replace(old, new), encoding="utf-8")
    original = source.read_bytes()
    with pytest.raises(ValueError):
        protect_dsn(source, output)
    assert source.read_bytes() == original
    assert not output.exists()


@pytest.mark.parametrize("text", ["", "(rules PCB input (rule (width 1)))", DSN[:-3], DSN + "(extra)", DSN + '"unterminated'])
def test_invalid_source(tmp_path, text):
    source = tmp_path / "source.dsn"
    source.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        protect_dsn(source, tmp_path / "out.dsn")
    assert not (tmp_path / "out.dsn").exists()


def test_empty_wiring(tmp_path):
    text = DSN[:DSN.index("  (wiring")] + "(wiring))"
    _, output, report = run(tmp_path, text)
    assert parse(output.read_text()) == parse(text)
    assert report["protected_count"] == report["wire_count"] == report["via_count"] == 0


@pytest.mark.parametrize("kind", ["same", "relative", "hardlink", "symlink", "existing", "dangling"])
def test_aliases_and_existing_destinations_never_overwritten(tmp_path, kind):
    source = tmp_path / "source.dsn"
    source.write_text(DSN, encoding="utf-8")
    original = source.read_bytes()
    output = tmp_path / "protected.dsn"
    if kind == "same":
        output = source
    elif kind == "relative":
        output = tmp_path / "sub" / ".." / "source.dsn"
        (tmp_path / "sub").mkdir()
    elif kind == "hardlink":
        output.hardlink_to(source)
    elif kind in ("symlink", "dangling"):
        try:
            output.symlink_to(source if kind == "symlink" else tmp_path / "missing.dsn")
        except OSError as exc:
            pytest.skip(f"Symlink creation unavailable: {exc}")
    else:
        output.write_bytes(b"existing output")
    with pytest.raises((ValueError, FileExistsError)):
        protect_dsn(source, output)
    assert source.read_bytes() == original
    if kind == "existing":
        assert output.read_bytes() == b"existing output"
    if kind == "dangling":
        assert not (tmp_path / "missing.dsn").exists()


def test_exclusive_open_handles_destination_race(tmp_path, monkeypatch):
    source, output = tmp_path / "source.dsn", tmp_path / "output.dsn"
    source.write_text(DSN, encoding="utf-8")
    real_open = Path.open

    def raced_open(path, mode="r", *args, **kwargs):
        if path == output and mode == "xb":
            with real_open(output, "wb") as stream:
                stream.write(b"concurrent output")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", raced_open)
    with pytest.raises(FileExistsError):
        protect_dsn(source, output)
    assert output.read_bytes() == b"concurrent output"


@pytest.mark.parametrize("coordinates,edges,entire", [
    ("137474 -102109 137474 -102109", [0], True),
    ("130000 -94483.4 130000 -95792.6 130000 -95792.6", [1], False),
    ("1000 1000 1000 1000 2000 1000", [0], False),
    ("1000 1000 1000 1000 1000 1000", [0, 1], True),
    ("1000 1000 1000 1000 2000 1000 2000 1000", [0, 2], False),
])
def test_native_repeated_points_preserved_and_counted(tmp_path, coordinates, edges, entire):
    text = DSN.replace("1000 1000 2000 1000)", coordinates + ")", 1)
    source, output, report = run(tmp_path, text)
    before, after = parse(text), parse(output.read_text())
    source_wire = child(before, "wiring")[1]
    protected_wire = child(after, "wiring")[1]
    assert child(protected_wire, "path") == child(source_wire, "path")
    assert child(protected_wire, "type")[1] == sexpdata.Symbol("protect")
    for name in ("parser", "resolution", "unit", "structure", "placement", "library", "network"):
        assert child(before, name) == child(after, name)
    assert report["repeated_point_path_count"] == 1
    assert report["zero_length_path_count"] == int(entire)
    assert report["zero_length_edge_count"] == len(edges)
    evidence = report["representation_zero_paths"][0]
    assert evidence["wire_index"] == 0
    assert evidence["net"] == "GND" and evidence["layer"] == "F.Cu" and evidence["width"] == 600
    assert evidence["zero_length_edge_indices"] == edges
    assert evidence["entire_path_zero_length"] is entire
    path = child(source_wire, "path")
    assert evidence["points"] == [list(p) for p in zip(path[3::2], path[4::2])]
    assert report["protected_count"] == 3
    assert report["source_bytes_unchanged_verified"] and report["ast_preserved_except_wiring_type"]
    second = tmp_path / "second.dsn"
    again = protect_dsn(output, second)
    assert output.read_bytes() == second.read_bytes()
    assert again["representation_zero_paths"] == report["representation_zero_paths"]
    assert source.read_text() == text


def test_nonzero_paths_report_no_representation_zeros(tmp_path):
    _, _, report = run(tmp_path)
    assert report["repeated_point_path_count"] == report["zero_length_path_count"] == report["zero_length_edge_count"] == 0
    assert report["representation_zero_paths"] == []


@pytest.mark.parametrize("old,new", [
    ('(host_cad "KiCad\'s Pcbnew")', '(host_cad "Other")'),
    ('(host_version "9.0.0")', '(host_version "8.0.9")'),
    ('(host_version "9.0.0")', '(host_version "10.0.0")'),
    ('(host_version "9.0.0")', '(host_version "9.0.9garbage")'),
    ('(host_version "9.0.0")', '(host_version 9.0)'),
    ('(host_version "9.0.0")', ''),
    ('(host_version "9.0.0")', '(host_version "9.0.0") (host_version "9.0.9")'),
    ('(host_version', '(generated_by_freerouting) (host_version'),
    ('(host_version', '(generated_by_freeroute) (host_version'),
    ('(resolution um 10)', '(resolution um 1000)'),
    ('(resolution um 10)', '(resolution mil 10)'),
    ('(unit um)', '(unit mil)'),
    ('(unit um)', ''),
    ('(unit um)', '(unit um) (unit um)'),
    ('(space_in_quoted_tokens on)', '(space_in_quoted_tokens off)'),
    ('(space_in_quoted_tokens on)', ''),
])
def test_repeated_points_require_verified_native_profile(tmp_path, old, new):
    text = DSN.replace("1000 1000 2000 1000)", "1000 1000 1000 1000)", 1).replace(old, new)
    source, output = tmp_path / "source.dsn", tmp_path / "protected.dsn"
    source.write_text(text)
    before = source.read_bytes()
    with pytest.raises(ValueError):
        protect_dsn(source, output)
    assert source.read_bytes() == before
    assert not output.exists()


@pytest.mark.parametrize("path", [
    "(path F.Cu 0 1000 1000 1000 1000)",
    "(path F.Cu -1 1000 1000 1000 1000)",
    "(path F.Cu nan 1000 1000 1000 1000)",
    "(path F.Cu inf 1000 1000 1000 1000)",
    "(path F.Cu 600 nan 1000 nan 1000)",
    "(path F.Cu 600 inf 1000 inf 1000)",
    '(path F.Cu 600 "1000" 1000 "1000" 1000)',
    "(path F.Cu 600 1000 1000)",
    "(path F.Cu 600 1000 1000 1000)",
    "(path Missing 600 1000 1000 1000 1000)",
])
def test_representation_zero_exception_does_not_relax_physical_fields(tmp_path, path):
    text = DSN.replace("(path F.Cu 600 1000 1000 2000 1000)", path)
    source, output = tmp_path / "source.dsn", tmp_path / "protected.dsn"
    source.write_text(text)
    with pytest.raises(ValueError):
        protect_dsn(source, output)
    assert not output.exists()


def test_actual_native_segment_rounds_to_degenerate_dsn_and_is_preserved(tmp_path):
    # Native working board UUID be5dcc3a-1c36-4900-b998-ce6e86dc42b9, /AN4,
    # In1.Cu, width 0.2 mm. POINT::Format emits six significant digits in um.
    start, end = (137.4739, 102.1091), (137.4738, 102.1091)
    assert start != end
    rounded = [tuple(float(format(v, ".6g")) for v in (p[0] * 1000, -p[1] * 1000)) for p in (start, end)]
    assert rounded == [(137474, -102109), (137474, -102109)]
    actual_path = "(path In1.Cu 200 137474 -102109 137474 -102109)"
    text = DSN.replace("F.Cu", "In1.Cu").replace("GND", '"/AN4"')
    text = text.replace("(path In1.Cu 600 1000 1000 2000 1000)", actual_path)
    text = text.replace('"9.0.0"', '"9.0.9-9.0.9~ubuntu24.04.1"')
    _, output, report = run(tmp_path, text)
    assert actual_path in output.read_text()
    assert report["representation_zero_paths"][0]["net"] == "/AN4"
    assert report["zero_length_path_count"] == report["zero_length_edge_count"] == 1
    assert report["physical_rule_overrides"] is False
