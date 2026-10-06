"""Offline Altium DRC evidence validation, not full-rule/native acceptance.

Only the observed English HTML table profile is supported. A hash-consistent
report is not a signed native attestation. Rule execution/waiver coverage is
never inferred from zero findings, rule descriptions, or enabled metadata.
"""
import argparse
import configparser
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
PINNED_INVENTORY = ROOT / "docs/validation/altium-native/a201905ecc7b4c3eb819e531d52c5f1e/result.json"
PINNED_INVENTORY_SHA256 = "2d306cfd23c1c5ec5fbdee41991bb494d9a23103012999477c7dc45380f5f4ad"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pin(raw, expected, label):
    _require(isinstance(expected, str) and re.fullmatch(r"[0-9a-fA-F]{64}", expected)
             and _sha(raw) == expected.lower(), label + " SHA256 mismatch or missing pin")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def _json(raw):
    def invalid(value):
        raise ValueError("Non-finite JSON constant: " + value)
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=invalid)
    _require(isinstance(value, dict), "Expected JSON evidence object")
    return value


def _file(value):
    _require(isinstance(value, (str, Path)) and bool(str(value)), "Missing evidence path")
    path = Path(value)
    _require(path.is_absolute() and path.is_file(), "Missing or non-absolute evidence file: " + str(path))
    for item in (path, *path.parents):
        _require(not item.is_symlink() and not getattr(item, "is_junction", lambda: False)(),
                 "Evidence path contains a link or junction")
    _require(path.stat().st_size > 0, "Empty evidence file: " + str(path))
    return path


class _Node:
    def __init__(self, tag, attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []
        self.parent = None

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    def find(self, tag):
        result = []
        for child in self.children:
            if isinstance(child, _Node):
                if child.tag == tag:
                    result.append(child)
                result.extend(child.find(tag))
        return result


class _HTML(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        _require(self.nodes <= 100000 and len(self.stack) <= 64, "HTML complexity exceeds supported profile")
        _require(len(dict(attrs)) == len(attrs), "Duplicate HTML attribute")
        node = _Node(tag, attrs)
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        _require(len(self.stack) > 1 and self.stack[-1].tag == tag,
                 "Unbalanced or unsupported HTML closure: " + tag)
        self.stack.pop()

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _text(node):
    return " ".join(node.text().split())


def _one(items, label):
    _require(len(items) == 1, "Requires exactly one " + label)
    return items[0]


def _count(text):
    _require(bool(re.fullmatch(r"[0-9]{1,12}", text)), "Invalid DRC count: " + text)
    return int(text)


def _rows(table):
    # Nested layout tables must not contribute duplicate summary cells.
    result = []
    for child in table.children:
        if isinstance(child, _Node):
            if child.tag == "tr":
                result.append([cell for cell in child.children if isinstance(cell, _Node) and cell.tag in {"td", "th"}])
            elif child.tag in {"thead", "tbody", "tfoot"}:
                result.extend(_rows(child))
    return result


def _file_url(value):
    parsed = urlsplit(value)
    _require(parsed.scheme == "file" and not parsed.netloc and not parsed.query and not parsed.fragment,
             "Filename link is not a local file URL")
    path = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:[\\/]", path):
        path = path[1:]
    return path


def _summary_table(rows, label):
    _require(len(rows) >= 2 and len(rows[0]) == 2 and all(cell.tag == "th" for cell in rows[0]),
             "Incomplete " + label + " table")
    findings, totals, anchors = [], [], set()
    for index, cells in enumerate(rows[1:]):
        _require(len(cells) == 2 and all(cell.tag == "td" for cell in cells), "Malformed " + label + " row")
        description, count = _text(cells[0]), _count(_text(cells[1]))
        _require(bool(description), "Empty " + label + " description")
        if description == "Total":
            _require(index == len(rows) - 2, "Total must close " + label + " table")
            totals.append(count)
            continue
        links = cells[0].find("a")
        _require(len(links) <= 1, "Ambiguous " + label + " row link")
        anchor = links[0].attrs.get("href") if links else None
        if anchor is not None:
            _require(isinstance(anchor, str) and anchor.startswith("#") and len(anchor) > 1
                     and anchor not in anchors, "Invalid or repeated summary anchor")
            anchors.add(anchor)
        findings.append({"description": description, "count": count, "anchor": anchor})
    total = _one(totals, label + " total")
    _require(sum(row["count"] for row in findings) == total, label + " row sum disagrees with total")
    return findings, total


def _rule_detail(table, rows, board):
    _require(len(rows) >= 2 and len(rows[0]) == 1 and rows[0][0].tag == "th"
             and rows[0][0].attrs.get("colspan", "1") == "1", "Malformed rule detail header")
    parent = table.parent
    _require(parent is not None and parent.tag == "a" and isinstance(parent.attrs.get("name"), str)
             and parent.attrs["name"].strip(), "Rule detail lacks native named anchor")
    description = _text(rows[0][0])
    _require(bool(description), "Empty rule detail title")
    items = []
    for cells in rows[1:]:
        _require(len(cells) == 1 and cells[0].tag == "td" and _text(cells[0]), "Malformed rule detail row")
        link = _one(cells[0].find("a"), "rule detail callback")
        callback = link.attrs.get("href", "")
        prefix = "dxpprocess://PCB:Zoom?document="
        _require(isinstance(callback, str) and callback.startswith(prefix), "Unsupported rule detail callback")
        document, separator, _ = callback[len(prefix):].partition(";")
        _require(separator and _file(unquote(document)).resolve() == board.resolve(), "Rule detail callback board mismatch")
        for acronym in link.find("acronym"):
            _require(acronym.attrs.get("title") == callback, "Rule detail callback title mismatch")
        items.append({"description": _text(cells[0]), "callback": callback})
    return {"anchor": "#" + parent.attrs["name"], "description": description, "items": items}


def _match_rule_details(rule_rows, details):
    by_anchor = {row["anchor"]: row for row in rule_rows if row["anchor"] is not None}
    seen = set()
    for detail in details:
        anchor = detail["anchor"]
        _require(anchor in by_anchor and anchor not in seen, "Orphan or duplicate rule detail anchor")
        row = by_anchor[anchor]
        _require(row["description"] == detail["description"], "Rule detail title does not match summary")
        _require(row["count"] > 0 and len(detail["items"]) == row["count"], "Rule detail count does not match summary")
        seen.add(anchor)
    _require(all(row["count"] == 0 or row["anchor"] in seen for row in rule_rows),
             "Nonzero rule summary lacks matching details")


def parse_drc_html(raw, board):
    """Parse bounded HTML; absent waiver/health reporting is unknown, not zero."""
    _require(isinstance(raw, bytes) and 0 < len(raw) <= 8 * 1024 * 1024, "Missing or oversized HTML report")
    parser = _HTML()
    parser.feed(raw.decode("utf-8-sig"))
    parser.close()
    _require(len(parser.stack) == 1, "Truncated HTML report")
    html = _one(parser.root.find("html"), "html document")
    _require(all(child is html or isinstance(child, str) and not child.strip() for child in parser.root.children),
             "Unexpected content outside HTML document")
    body = _one(html.find("body"), "report body")
    _require([_text(node) for node in body.find("h1")] == ["Design Rule Verification Report"], "Wrong DRC report title")
    _require("Summary" in [_text(node) for node in body.find("h2")], "Missing report summary")
    tables = body.find("table")
    front = _one([table for table in tables if table.attrs.get("class") == "front_matter"], "front matter")
    filenames = [row for row in _rows(front) if row and _text(row[0]) == "Filename:"]
    filename = _one(filenames, "report filename")
    _require(len(filename) == 3, "Malformed report filename")
    visible_path = filename[2].text().strip()
    _require(_file(visible_path).resolve() == board.resolve(), "HTML board identity mismatch")
    link = _one(filename[2].find("a"), "filename link")
    _require(_file(_file_url(link.attrs.get("href", ""))).resolve() == board.resolve(), "HTML filename link mismatch")
    for acronym in filename[2].find("acronym"):
        _require(_file(acronym.attrs.get("title")).resolve() == board.resolve(), "HTML filename title mismatch")

    header = _one([table for table in tables if table.attrs.get("class") == "DRC_summary_header"], "DRC header")
    header_counts = {}
    allowed = {"Warnings:", "Rule Violations:", "Waived Violations:", "Waived Rule Violations:", "PCB Health Issues:"}
    for row in _rows(header):
        _require(len(row) == 3 and not _text(row[1]), "Malformed DRC header count")
        key = _text(row[0])
        _require(key in allowed and key not in header_counts, "Unexpected or duplicate DRC header count")
        header_counts[key] = _count(_text(row[2]))
    _require({"Warnings:", "Rule Violations:"} <= header_counts.keys(), "Missing DRC header counts")

    summaries, details = {}, []
    known = {"Warnings", "Rule Violations", "Waived Violations", "Waived Rule Violations", "PCB Health Issues"}
    for table in tables:
        rows = _rows(table)
        if not rows or not rows[0] or rows[0][0].tag != "th":
            continue
        if "rule" in (rows[0][0].attrs.get("class") or "").split():
            details.append(_rule_detail(table, rows, board))
            continue
        title = _text(rows[0][0])
        _require(title in known and [_text(cell) for cell in rows[0]] == [title, "Count"],
                 "Unsupported report table header")
        _require(title not in summaries, "Duplicate " + title + " table")
        summaries[title] = _summary_table(rows, title)
    _require({"Warnings", "Rule Violations"} <= summaries.keys(), "Missing DRC summary tables")
    for title in ("Warnings", "Rule Violations"):
        _require(summaries[title][1] == header_counts[title+":"], "Header/summary count mismatch: " + title)
    _require(bool(summaries["Rule Violations"][0]), "No executed rule rows recorded")
    _match_rule_details(summaries["Rule Violations"][0], details)
    health_title = "PCB Health Issues"
    _require((health_title in summaries) == (health_title + ":" in header_counts),
             "PCB Health Issues requires both header and summary table")
    health_rows, health = summaries.get(health_title, ([], None))
    if health is not None:
        _require(header_counts[health_title + ":"] == health, "PCB Health Issues header/summary count mismatch")
    else:
        _require(not re.search(r"PCB Health Issues\b", _text(body), re.I), "Unsupported PCB Health Issues reporting")
    waived_titles = [title for title in summaries if title.startswith("Waived")]
    waived_headers = [title for title in header_counts if title.startswith("Waived")]
    _require(len(waived_titles) <= 1 and len(waived_headers) <= 1, "Ambiguous waiver counts")
    _require(not waived_headers or waived_titles and waived_headers[0] == waived_titles[0]+":",
             "Waived header lacks matching detail table")
    if waived_titles:
        waived_rows, waived = summaries[waived_titles[0]]
        if waived_headers:
            _require(header_counts[waived_headers[0]] == waived, "Waived count mismatch")
    else:
        _require(not re.search(r"\bwaiv(?:ed|er|ers|ing)\b", _text(body), re.I), "Unsupported waiver reporting")
        waived_rows, waived = [], None
    return {"counts": {"warnings": summaries["Warnings"][1], "violations": summaries["Rule Violations"][1],
                       "rule_rows": len(summaries["Rule Violations"][0]), "waived": waived, "health_issues": health},
            "warning_rows": summaries["Warnings"][0], "rule_rows": summaries["Rule Violations"][0],
            "violation_details": details, "rule_detail_counts_verified": True,
            "waived_rows": waived_rows, "waived_reporting": "explicit_table" if waived is not None else "not_reported",
            "health_rows": health_rows, "health_reporting": "explicit_table" if health is not None else "not_reported",
            "health_summary_counts_verified": health is not None,
            "board": str(board), "profile": "english_html_summary_tables"}


def _validate(result_path, inventory_path, inventory_sha256, expected_result_sha256):
    inputs = {}
    def read(value):
        path = _file(value)
        raw = path.read_bytes()
        inputs[path] = raw
        return path, raw

    path, raw = read(Path(result_path).absolute())
    if expected_result_sha256 is not None:
        _pin(raw, expected_result_sha256, "DRC result")
    data = _json(raw)
    _require(data.get("status") == "completed" and data.get("mode") == "DRC", "Requires completed DRC result")
    _require(isinstance(data.get("request"), str) and data["request"].strip(), "Missing DRC request")
    native = data.get("native")
    _require(isinstance(native, dict) and set(native) == {"job", "completion"}
             and all(isinstance(row, dict) for row in native.values()), "Missing or unsupported native sections")
    _require(native["job"].get("request") == data["request"], "Native DRC request mismatch")
    _require(native["job"].get("native_return") == "True" and native["completion"].get("status") == "completed",
             "Native DRC failed or incomplete")
    source, source_raw = read(data.get("source"))
    board, board_raw = read(data.get("board"))
    _require(board.parent.resolve() == path.parent.resolve() and not source.samefile(board), "DRC requires an independent local snapshot")
    _require(_file(native["job"].get("board")).resolve() == board.resolve(), "Native board identity mismatch")
    _require(data.get("source_unchanged") is True, "Missing original unchanged assertion")
    _pin(source_raw, data.get("source_sha256"), "Original source")
    _pin(board_raw, data.get("source_snapshot_sha256"), "DRC snapshot")
    _require(_sha(board_raw) == _sha(source_raw), "DRC snapshot no longer equals original pinned board")
    response, response_raw = read(path.parent / "response.ini")
    _pin(response_raw, data.get("response_sha256"), "Native response")
    ini = configparser.ConfigParser(interpolation=None, strict=True)
    ini.read_string(response_raw.decode("utf-8-sig"))
    _require(not ini.defaults() and {section: dict(ini[section]) for section in ini.sections()} == native,
             "Native response does not match result sections")
    job, job_raw = read(path.parent / "Job.pas")
    normalized_job = job_raw.decode("ascii").replace("\r\n", "\n").replace("\r", "\n").encode("ascii")
    _pin(normalized_job, data.get("template_sha256"), "Generated script normalized text")
    artifacts = data.get("artifacts")
    _require(isinstance(artifacts, dict) and set(artifacts) == {"native-drc.html"}, "Missing or unsupported DRC artifact manifest")
    report, report_raw = read(path.parent / "native-drc.html")
    _pin(report_raw, artifacts["native-drc.html"], "Native HTML report")

    inv_path, inv_raw = read(Path(inventory_path).absolute())
    _pin(inv_raw, inventory_sha256, "Pinned inventory")
    inventory = _json(inv_raw)
    _require(inventory.get("status") == "completed" and inventory.get("mode") == "Inventory"
             and inventory.get("source_unchanged") is True, "Invalid pinned Inventory status")
    _require(_file(inventory.get("source")).resolve() == source.resolve(), "Inventory refers to a different original board")
    _pin(source_raw, inventory.get("source_sha256"), "Inventory source binding")
    inv_board, inv_board_raw = read(inventory.get("board"))
    _pin(inv_board_raw, inventory.get("source_sha256"), "Inventory snapshot")
    inv_native = inventory.get("native", {})
    _require(isinstance(inv_native, dict) and isinstance(inv_native.get("job"), dict)
             and isinstance(inv_native.get("completion"), dict), "Missing inventory completion sections")
    _require(inv_native["completion"].get("status") == "completed"
             and inv_native["job"].get("request") == inventory.get("request")
             and _file(inv_native["job"].get("board")).resolve() == inv_board.resolve(), "Inventory native identity mismatch")
    rule_keys = [key for key in inv_native if key.startswith("rule.")]
    _require(rule_keys and set(rule_keys) == {"rule."+str(i) for i in range(len(rule_keys))}, "Inventory rule sections incomplete")
    enabled, names = [], set()
    for key in sorted(rule_keys, key=lambda value: int(value.split(".")[1])):
        rule = inv_native[key]
        _require(isinstance(rule, dict) and all(isinstance(rule.get(field), str) and rule[field].strip()
                 for field in ("name", "kind", "scope1", "scope2", "drc_enabled", "priority")), "Incomplete inventory rule metadata")
        _require(rule["name"].casefold() not in names and rule["drc_enabled"] in {"True", "False"}, "Ambiguous inventory rule")
        names.add(rule["name"].casefold())
        _require(re.fullmatch(r"[0-9]+", rule["kind"]) and re.fullmatch(r"[1-9][0-9]*", rule["priority"]), "Invalid inventory rule kind/priority")
        if rule["drc_enabled"] == "True":
            enabled.append({"section": key, **rule})
    parsed = parse_drc_html(report_raw, board)
    _require(all(_file(file).read_bytes() == original for file, original in inputs.items()), "Evidence changed during validation")
    reasons = ["Stable native rule identities and executed check-options coverage are not captured"]
    for key in ("warnings", "violations", "waived", "health_issues"):
        if parsed["counts"][key]:
            reasons.append("Report contains " + key)
    if parsed["counts"]["waived"] is None:
        reasons.append("Waiver reporting/inclusion is not established; absence is not zero")
    if parsed["counts"]["health_issues"] is None:
        reasons.append("PCB health reporting is not established; absence is not zero")
    return {"report_valid": True, "report_status": "valid", "request": data["request"], **parsed,
            "blocking_reasons": reasons,
            "coverage": {"complete": False, "inventory_rules": len(rule_keys), "enabled_rules": len(enabled),
                         "reported_rule_rows": parsed["counts"]["rule_rows"],
                         "row_count_shortfall": max(0, len(enabled)-parsed["counts"]["rule_rows"]),
                         "exact_rule_mapping_verified": False, "check_options_verified": False,
                         "health_check_coverage_verified": False,
                         "waiver_coverage_verified": False, "unproven_enabled_rules": enabled,
                         "note": "Descriptions/counts are not native rule identities or proof of execution"},
            "integrity": {"source_sha256": _sha(source_raw), "snapshot_sha256": _sha(board_raw),
                          "source_unchanged": True, "snapshot_byte_identical_to_original": True,
                          "result_sha256": _sha(raw), "report_sha256": _sha(report_raw),
                          "response_sha256": _sha(response_raw), "script_byte_sha256": _sha(job_raw),
                          "script_normalized_sha256": _sha(normalized_job),
                          "inventory_path": str(inv_path), "inventory_sha256": _sha(inv_raw),
                          "caller_result_pin_checked": expected_result_sha256 is not None}}


def validate_drc(result_path, *, inventory_path=PINNED_INVENTORY,
                 inventory_sha256=PINNED_INVENTORY_SHA256, expected_result_sha256=None):
    """Read existing evidence; valid partial reports remain blocked. Never invokes Altium."""
    result = {"schema": 1, "status": "blocked", "report_status": "invalid", "report_valid": False,
              "native_drc_pass": False, "full_autoroute_authorized": False, "manufacturing_authorized": False}
    try:
        result.update(_validate(result_path, inventory_path, inventory_sha256, expected_result_sha256))
    except (ValueError, OSError, TypeError, KeyError, configparser.Error) as error:
        result["errors"] = [str(error)]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--expected-result-sha256")
    args = parser.parse_args(argv)
    result = validate_drc(args.result, expected_result_sha256=args.expected_result_sha256)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 2 if result["report_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
