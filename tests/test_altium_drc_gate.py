"""Offline parser/integrity faults; synthetic reports are not native DRC evidence."""
import configparser
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("altium_drc_gate", ROOT / "scripts/altium_drc_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
DRC = ROOT / "docs/validation/altium-native/5c525fb85c964c329747849c46897dd3"
NEGATIVE = ROOT / "docs/validation/altium-native/2c5259b0dcbb4d0eb0f983445d9dea79"
SETUP = ROOT / "docs/validation/altium-native/8fbedf606b454e1698f9e891b96135f6"


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def evidence(tmp_path):
    data = json.loads((DRC / "result.json").read_text())
    inventory = json.loads(gate.PINNED_INVENTORY.read_text())
    old_board = data["board"]
    source = tmp_path / "original.PcbDoc"
    source.write_bytes(b"offline board fixture, not a native PCB")
    folder = tmp_path / "drc"
    folder.mkdir()
    board = folder / "snapshot.PcbDoc"
    board.write_bytes(source.read_bytes())
    inv_folder = tmp_path / "inventory"
    inv_folder.mkdir()
    inv_board = inv_folder / "snapshot.PcbDoc"
    inv_board.write_bytes(source.read_bytes())
    sha = gate._sha(source.read_bytes())
    data.update(source=str(source), board=str(board), source_sha256=sha, source_snapshot_sha256=sha)
    data["native"]["job"]["board"] = str(board)
    inventory.update(source=str(source), board=str(inv_board), source_sha256=sha)
    inventory["native"]["job"]["board"] = str(inv_board)
    inv_path = inv_folder / "result.json"
    _write_json(inv_path, inventory)
    html = (DRC / "native-drc.html").read_text().replace(old_board, str(board))
    (folder / "native-drc.html").write_text(html, encoding="utf-8")
    script = (DRC / "Job.pas").read_text().replace(old_board, str(board))
    (folder / "Job.pas").write_bytes(script.replace("\n", "\r\n").encode("ascii"))
    state = {"path": folder / "result.json", "data": data, "inventory": inv_path,
             "inventory_sha256": gate._sha(inv_path.read_bytes()), "html": html, "board": board}
    persist(state)
    return state


def persist(state):
    folder, data = state["path"].parent, state["data"]
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict(data["native"])
    with (folder / "response.ini").open("w", encoding="utf-8") as stream:
        parser.write(stream)
    (folder / "native-drc.html").write_text(state["html"], encoding="utf-8")
    data["response_sha256"] = gate._sha((folder / "response.ini").read_bytes())
    data["template_sha256"] = gate._sha((folder / "Job.pas").read_text().encode("ascii"))
    data["artifacts"] = {"native-drc.html": gate._sha((folder / "native-drc.html").read_bytes())}
    _write_json(state["path"], data)


def validate(state):
    return gate.validate_drc(state["path"], inventory_path=state["inventory"], inventory_sha256=state["inventory_sha256"])


def rejected(state):
    result = validate(state)
    assert not result["report_valid"] and result["report_status"] == "invalid", result
    assert result["status"] == "blocked" and result["errors"]
    assert not result["native_drc_pass"] and not result["full_autoroute_authorized"]
    return result


def test_observed_zero_report_has_only_six_rows_and_no_full_coverage(evidence):
    files = [file for file in evidence["path"].parent.parent.rglob("*") if file.is_file()]
    before = {file: file.read_bytes() for file in files}
    result = validate(evidence)
    assert result["report_valid"], result
    assert result["counts"] == {"warnings": 0, "violations": 0, "rule_rows": 6, "waived": None, "health_issues": None}
    assert result["health_reporting"] == "not_reported" and not result["health_summary_counts_verified"]
    assert result["coverage"]["enabled_rules"] == result["coverage"]["inventory_rules"] == 40
    assert result["coverage"]["row_count_shortfall"] == 34
    assert len(result["coverage"]["unproven_enabled_rules"]) == 40
    assert not result["coverage"]["complete"] and not result["coverage"]["exact_rule_mapping_verified"]
    assert result["status"] == "blocked" and not result["native_drc_pass"]
    assert result["waived_reporting"] == "not_reported"
    assert result["integrity"]["snapshot_byte_identical_to_original"]
    assert result["integrity"]["script_byte_sha256"] != result["integrity"]["script_normalized_sha256"]
    assert before == {file: file.read_bytes() for file in files}


@pytest.mark.parametrize("file", ["native-drc.html", "response.ini", "Job.pas", "snapshot.PcbDoc"])
@pytest.mark.parametrize("change", ["missing", "empty", "tamper"])
def test_required_files_hashes(evidence, file, change):
    path = evidence["path"].parent / file
    if change == "missing":
        path.unlink()
    elif change == "empty":
        path.write_bytes(b"")
    else:
        path.write_bytes(path.read_bytes() + b"changed")
    rejected(evidence)


@pytest.mark.parametrize("fault", ["status", "mode", "request", "native_return", "completion", "job",
    "source_changed", "source_flag", "source_pin", "snapshot_pin", "native_board", "inventory_pin", "artifacts"])
def test_binding_and_failed_native_status(evidence, fault):
    data = evidence["data"]
    if fault == "status":
        data["status"] = "failed"
    elif fault == "mode":
        data["mode"] = "Inventory"
    elif fault == "request":
        data["native"]["job"]["request"] = "other"
    elif fault == "native_return":
        data["native"]["job"]["native_return"] = "False"
    elif fault == "completion":
        data["native"]["completion"]["status"] = "incomplete"
    elif fault == "job":
        del data["native"]["job"]
    elif fault == "source_changed":
        Path(data["source"]).write_bytes(b"changed")
    elif fault == "source_flag":
        data["source_unchanged"] = "true"
    elif fault == "source_pin":
        data["source_sha256"] = "0" * 64
    elif fault == "snapshot_pin":
        data["source_snapshot_sha256"] = "0" * 64
    elif fault == "native_board":
        data["native"]["job"]["board"] = data["source"]
    elif fault == "inventory_pin":
        evidence["inventory"].write_bytes(evidence["inventory"].read_bytes() + b" ")
    persist(evidence)
    if fault == "artifacts":
        data["artifacts"] = {}
        _write_json(evidence["path"], data)
    rejected(evidence)


@pytest.mark.parametrize("fault", ["visible", "href", "title"])
def test_html_identity_checked_even_with_matching_report_hash(evidence, fault):
    board = str(evidence["board"])
    other = evidence["path"].parent / "other.PcbDoc"
    other.write_bytes(evidence["board"].read_bytes())
    html = evidence["html"]
    if fault == "visible":
        html = html.replace(">"+board+"</acronym>", ">"+str(other)+"</acronym>")
    elif fault == "href":
        html = html.replace('href="file:///'+board+'"', 'href="file:///'+str(other)+'"')
    else:
        html = html.replace('title="'+board+'"', 'title="'+str(other)+'"')
    assert html != evidence["html"]
    evidence["html"] = html
    persist(evidence)
    rejected(evidence)


@pytest.mark.parametrize("fault", ["truncated", "title", "missing_summary", "negative", "nonfinite", "header_mismatch",
    "row_sum", "missing_total", "duplicate_table", "unrecognized_waived", "missing_rules"])
def test_incomplete_or_ambiguous_html(evidence, fault):
    html = evidence["html"]
    if fault == "truncated":
        html = html[:html.rfind("</body>")]
    elif fault == "title":
        html = html.replace("Design Rule Verification Report", "Export Report")
    elif fault == "missing_summary":
        html = html.replace("<h2>Summary</h2>", "<h2>Other</h2>")
    elif fault in {"negative", "nonfinite", "header_mismatch"}:
        value = {"negative": "-1", "nonfinite": "NaN", "header_mismatch": "1"}[fault]
        html = html.replace('DRC_summary_header_col3">0', 'DRC_summary_header_col3">'+value, 1)
    elif fault == "row_sum":
        html = html.replace('<td class="column2">0</td>', '<td class="column2">1</td>', 1)
    elif fault == "missing_total":
        html = html.replace('class="column1">Total</td>', 'class="column1">Missing</td>', 1)
    elif fault == "duplicate_table":
        html = html.replace("</body>", '<table><tr><th>Warnings</th><th>Count</th></tr><tr><td>Total</td><td>0</td></tr></table></body>')
    elif fault == "unrecognized_waived":
        html = html.replace("</body>", "<p>Waived violations omitted</p></body>")
    else:
        start = html.index('<tr class="onmouseout_odd"')
        end = html.index('<tr>\n<td style="font-weight', start)
        html = html[:start] + html[end:]
    evidence["html"] = html
    persist(evidence)
    rejected(evidence)


def _synthetic_html(board, *, warnings=0, violations=0, waived=None, rules=1):
    warning_row = f"<tr><td>Warning detail</td><td>{warnings}</td></tr>" if warnings else ""
    rule_rows = "".join(f'<tr><td><a href="#rule{i}">Rule {i}</a></td><td>{violations if i == 0 else 0}</td></tr>' for i in range(rules))
    details = ""
    if violations:
        items = "".join(f'<tr><td><a href="dxpprocess://PCB:Zoom?document={board};viewname=PCBEditor">Violation {i}</a></td></tr>'
                        for i in range(violations))
        details = '<a name="rule0"><table><tr><th class="rule">Rule 0</th></tr>' + items + '</table></a>'
    waiver_table = ("" if waived is None else f"<table><tr><th>Waived Violations</th><th>Count</th></tr>"
                   f"<tr><td>Waived detail</td><td>{waived}</td></tr><tr><td>Total</td><td>{waived}</td></tr></table>")
    return f'''<html><body><h1>Design Rule Verification Report</h1>
<table class="front_matter"><tr><td>Filename:</td><td></td><td><a href="{board.as_uri()}">{board}</a></td></tr></table>
<table class="DRC_summary_header"><tr><td>Warnings:</td><td></td><td>{warnings}</td></tr>
<tr><td>Rule Violations:</td><td></td><td>{violations}</td></tr></table><h2>Summary</h2>
<table><tr><th>Warnings</th><th>Count</th></tr>{warning_row}<tr><td>Total</td><td>{warnings}</td></tr></table>
<table><tr><th>Rule Violations</th><th>Count</th></tr>{rule_rows}<tr><td>Total</td><td>{violations}</td></tr></table>
{waiver_table}{details}</body></html>'''


@pytest.mark.parametrize("warnings,violations,waived", [(2, 0, None), (0, 3, None), (0, 0, 2), (0, 0, 0)])
def test_nonzero_findings_and_explicit_waiver_counts_remain_blocked(evidence, warnings, violations, waived):
    evidence["html"] = _synthetic_html(evidence["board"], warnings=warnings, violations=violations, waived=waived)
    persist(evidence)
    result = validate(evidence)
    assert result["report_valid"], result
    assert result["counts"] == {"warnings": warnings, "violations": violations, "rule_rows": 1,
                                "waived": waived, "health_issues": None}
    assert result["status"] == "blocked" and not result["native_drc_pass"]
    assert not result["coverage"]["waiver_coverage_verified"]


def test_forty_zero_rows_are_not_proof_of_forty_enabled_rules(evidence):
    evidence["html"] = _synthetic_html(evidence["board"], rules=40, waived=0)
    persist(evidence)
    result = validate(evidence)
    assert result["report_valid"] and result["coverage"]["row_count_shortfall"] == 0
    assert len(result["coverage"]["unproven_enabled_rules"]) == 40
    assert not result["coverage"]["complete"] and not result["native_drc_pass"]


def test_duplicate_json_and_response_mismatch(evidence):
    path = evidence["path"]
    path.write_text(path.read_text().replace('"status": "completed"', '"status": "failed", "status": "completed"', 1))
    rejected(evidence)
    persist(evidence)
    evidence["data"]["native"]["job"]["extra"] = "unbound"
    _write_json(path, evidence["data"])
    rejected(evidence)


def test_caller_result_pin(evidence):
    result = gate.validate_drc(evidence["path"], inventory_path=evidence["inventory"],
        inventory_sha256=evidence["inventory_sha256"], expected_result_sha256="0"*64)
    assert not result["report_valid"] and "result" in result["errors"][0]


def test_post_parse_file_change_is_rejected(evidence, monkeypatch):
    real = gate.parse_drc_html
    def changed(raw, board):
        result = real(raw, board)
        board.write_bytes(b"changed during validation")
        return result
    monkeypatch.setattr(gate, "parse_drc_html", changed)
    rejected(evidence)


def _negative_html(evidence):
    data = json.loads((NEGATIVE / "result.json").read_text())
    raw = (NEGATIVE / "native-drc.html").read_bytes()
    assert gate._sha(raw) == data["artifacts"]["native-drc.html"]
    return raw.decode("utf-8").replace(data["board"], str(evidence["board"]))


def test_real_native_negative_details_parse_without_authorizing_board(evidence):
    parsed = gate.parse_drc_html(_negative_html(evidence).encode("utf-8"), evidence["board"])
    assert parsed["counts"] == {"warnings": 0, "violations": 2, "rule_rows": 6, "waived": None, "health_issues": None}
    assert parsed["rule_detail_counts_verified"]
    assert len(parsed["violation_details"]) == 2
    assert sum(len(detail["items"]) for detail in parsed["violation_details"]) == 2
    width = next(detail for detail in parsed["violation_details"] if detail["description"].startswith("Width Constraint"))
    assert "Actual Width = 0.001mm, Target Width = 0.25mm" in width["items"][0]["description"]
    assert all(detail["anchor"] in {row["anchor"] for row in parsed["rule_rows"]} for detail in parsed["violation_details"])
    result = gate.validate_drc(NEGATIVE / "result.json")
    assert not result["report_valid"] and not result["native_drc_pass"]
    assert result["status"] == "blocked" and "completed DRC" in result["errors"][0]


@pytest.mark.parametrize("mode,native_return", [("DRCNegative", "False"), ("DRC", "False")])
def test_native_negative_mode_and_false_return_remain_strictly_rejected(evidence, mode, native_return):
    evidence["html"] = _negative_html(evidence)
    evidence["data"]["mode"] = mode
    evidence["data"]["native"]["job"]["native_return"] = native_return
    persist(evidence)
    rejected(evidence)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "orphan", "title", "count", "zero_summary",
    "board", "callback_title", "bad_header", "empty_item"])
def test_detail_summary_mismatches_fail_closed(evidence, fault):
    html = _negative_html(evidence)
    parser = gate._HTML()
    parser.feed(html)
    detail = next(table for table in parser.root.find("table")
                  if gate._rows(table) and gate._rows(table)[0][0].attrs.get("class") == "rule")
    anchor = detail.parent.attrs["name"]
    start = html.index('<a name="' + anchor + '">')
    end = html.index('</table></a>', start) + len('</table></a>')
    block = html[start:end]
    if fault == "missing":
        changed = ""
    elif fault == "duplicate":
        changed = block + block
    elif fault == "orphan":
        changed = block.replace('name="'+anchor+'"', 'name="unknown"')
    elif fault == "title":
        changed = block.replace('class="rule">', 'class="rule">Wrong title ', 1)
    elif fault in {"count", "empty_item"}:
        row_start = block.index('<tr class="onmouseout_odd"')
        row_end = block.index('</tr>', row_start) + len('</tr>')
        row = block[row_start:row_end]
        changed = block[:row_start] + (row + row if fault == "count" else '<tr><td></td></tr>') + block[row_end:]
    elif fault == "zero_summary":
        title = gate._text(gate._rows(detail)[0][0])
        changed = block.replace('name="'+anchor+'"', 'name="rule0"').replace(title, "Rule 0")
        html = _synthetic_html(evidence["board"], violations=0).replace('</body>', changed+'</body>')
        with pytest.raises(ValueError, match="count"):
            gate.parse_drc_html(html.encode("utf-8"), evidence["board"])
        return
    elif fault == "board":
        other = evidence["board"].with_name("other.PcbDoc")
        other.write_bytes(evidence["board"].read_bytes())
        changed = block.replace(str(evidence["board"]), str(other))
    elif fault == "callback_title":
        changed = block.replace('title="dxpprocess:', 'title="different:')
    else:
        changed = block.replace('colspan="1"', 'colspan="2"')
    assert changed != block
    evidence["html"] = html[:start] + changed + html[end:]
    persist(evidence)
    rejected(evidence)


def _setup_html(evidence):
    data = json.loads((SETUP / "result.json").read_text())
    raw = (SETUP / "native-drc.html").read_bytes()
    assert gate._sha(raw) == data["artifacts"]["native-drc.html"] == (
        "22f23338861170dd1bd9c71c84658691255957fbbcf533e25219cca40d5268ef")
    return raw.decode("utf-8").replace(data["board"], str(evidence["board"]))


def test_real_setup_health_summary_and_all_violation_details(evidence):
    parsed = gate.parse_drc_html(_setup_html(evidence).encode("utf-8"), evidence["board"])
    assert parsed["counts"] == {"warnings": 0, "violations": 158, "rule_rows": 25,
                                "waived": None, "health_issues": 0}
    assert parsed["health_rows"] == [] and parsed["health_reporting"] == "explicit_table"
    assert parsed["health_summary_counts_verified"] and parsed["rule_detail_counts_verified"]
    assert len(parsed["violation_details"]) == 10
    assert sum(len(detail["items"]) for detail in parsed["violation_details"]) == 158
    result = gate.validate_drc(SETUP / "result.json")
    assert result["status"] == "blocked" and not result["report_valid"]
    assert not result["native_drc_pass"] and not result["full_autoroute_authorized"]
    assert "completed DRC" in result["errors"][0]


@pytest.mark.parametrize("mode,native_return", [("DRCSetup", "False"), ("DRCSetup", "True"), ("DRC", "False")])
def test_setup_health_support_does_not_relax_native_completion(evidence, mode, native_return):
    evidence["html"] = _setup_html(evidence)
    evidence["data"]["mode"] = mode
    evidence["data"]["native"]["job"]["native_return"] = native_return
    persist(evidence)
    rejected(evidence)


def _health_blocks(count=0):
    header = f"<tr><td>PCB Health Issues:</td><td></td><td>{count}</td></tr>"
    row = f"<tr><td>Synthetic health issue</td><td>{count}</td></tr>" if count else ""
    table = ("<table><tr><th>PCB Health Issues</th><th>Count</th></tr>" + row
             + f"<tr><td>Total</td><td>{count}</td></tr></table>")
    return header, table


def _with_health(board, header, table):
    html = _synthetic_html(board, waived=0, rules=40)
    return html.replace("</table><h2>Summary", header + "</table><h2>Summary").replace("</body>", table + "</body>")


@pytest.mark.parametrize("count", [0, 2])
def test_health_counts_separate_from_rules_never_prove_full_coverage(evidence, count):
    evidence["html"] = _with_health(evidence["board"], *_health_blocks(count))
    persist(evidence)
    result = validate(evidence)
    assert result["report_valid"], result
    assert result["counts"]["health_issues"] == count
    assert sum(row["count"] for row in result["health_rows"]) == count
    assert result["counts"]["violations"] == 0 and result["counts"]["rule_rows"] == 40
    assert result["health_summary_counts_verified"]
    assert not result["coverage"]["health_check_coverage_verified"]
    assert not result["coverage"]["complete"] and not result["coverage"]["check_options_verified"]
    assert result["status"] == "blocked" and not result["native_drc_pass"]
    if count:
        assert "Report contains health_issues" in result["blocking_reasons"]


@pytest.mark.parametrize("fault", ["missing_header", "missing_table", "duplicate_header", "duplicate_table",
    "header_mismatch", "row_sum", "negative", "nonfinite", "missing_total", "duplicate_total",
    "missing_row", "bad_columns", "empty_description", "duplicate_anchor", "unknown_title", "stray_marker"])
def test_health_summary_faults_rejected_even_with_matching_hash(evidence, fault):
    header, table = _health_blocks(2)
    if fault == "missing_header":
        header = ""
    elif fault == "missing_table":
        table = ""
    elif fault == "duplicate_header":
        header *= 2
    elif fault == "duplicate_table":
        table *= 2
    elif fault == "header_mismatch":
        header = header.replace("<td>2</td>", "<td>0</td>")
    elif fault in {"row_sum", "negative", "nonfinite"}:
        value = {"row_sum": "1", "negative": "-2", "nonfinite": "NaN"}[fault]
        table = table.replace("<td>2</td>", f"<td>{value}</td>", 1)
    elif fault == "missing_total":
        table = table.replace("<tr><td>Total</td><td>2</td></tr>", "")
    elif fault == "duplicate_total":
        table = table.replace("</table>", "<tr><td>Total</td><td>2</td></tr></table>")
    elif fault == "missing_row":
        table = table.replace("<tr><td>Synthetic health issue</td><td>2</td></tr>", "")
    elif fault == "bad_columns":
        table = table.replace("<th>Count</th>", "<th>Count</th><th>Other</th>")
    elif fault == "empty_description":
        table = table.replace("Synthetic health issue", "")
    elif fault == "duplicate_anchor":
        row = '<tr><td><a href="#health">Issue</a></td><td>1</td></tr>'
        table = table.replace("<tr><td>Synthetic health issue</td><td>2</td></tr>", row * 2)
    elif fault == "unknown_title":
        table = table.replace("PCB Health Issues", "PCB Health Issue Details")
    else:
        header, table = "", "<p>PCB Health Issues omitted</p>"
    evidence["html"] = _with_health(evidence["board"], header, table)
    persist(evidence)
    rejected(evidence)


@pytest.mark.parametrize("replacement", ["-1", "NaN", "1"])
def test_real_setup_health_header_tampering_rejected(evidence, replacement):
    html = _setup_html(evidence)
    start = html.index('>PCB Health Issues:</td>')
    old = 'DRC_summary_header_col3">0'
    assert old in html[start:]
    evidence["html"] = html[:start] + html[start:].replace(old, 'DRC_summary_header_col3">' + replacement, 1)
    persist(evidence)
    rejected(evidence)
