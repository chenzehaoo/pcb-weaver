"""Read-only acceptance of one pinned, partial Altium antenna repair.

This does not relax altium_drc_gate or authorize routing/manufacturing. Native
False returns are preserved as evidence of remaining findings/incomplete SI.
"""
from collections import Counter
import configparser
import json
from pathlib import Path
import re

from altium_drc_gate import _file, _json, _pairs, _pin, _require, _sha, parse_drc_html


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docs/validation/altium-native"
BASELINE = BASE / "8fbedf606b454e1698f9e891b96135f6"
COPPER = BASE / "39c807955272477ba12222e9f77ec561"
CANDIDATE = BASE / "962efdb9522e425a8b2b9012b2fd973f"
RESULT_PINS = {
    BASELINE.name: "c71690808dbffbe0439a985ecfb879c00aa18f813d5dca4d7667891494b708ef",
    COPPER.name: "eb76b67a1832ac7035918edad61eb47b834870f86eb399f525ce33b7780ebc13",
    CANDIDATE.name: "264eafb5d0fe57d6031e3ef03425c515667789f0defa954d737d16c7cbf0d5f0",
}
SCRIPT_PINS = {
    BASELINE.name: "91cfdb41f42c2abd84f0b96582dfd603fd2aeaaaf459451872c305920dd48eee",
    COPPER.name: "c64de49ead66b073eecf5e97b069fb196f9d150a6801ee76fc65e1ac10ebd291",
    CANDIDATE.name: "e1bfe053e98ecdc9d39b380b1f2c7947f172dec4ba633f7d641255e530422f3b",
}
OPTION_PINS = {
    "batch-effective.json": "d64a79ceebee3c941d788cc729428981c911831cfb6ffe28ecaad69d4841b3f4",
    "report-options-effective.json": "24772eecec2a00d0b7d6b50ac882eff4153b8c7c6f8388e3bf7448d58971de8b",
}
OLD_TRACK = {"x1": "44514567", "y1": "41879922", "x2": "44514567", "y2": "41966711",
             "width": "100000", "layer": "32", "is_keepout": "False", "net": "GND"}
NEW_TRACK = {**OLD_TRACK, "y1": "41850394", "width": "98425"}
ANTENNA = "Net Antennae (Tolerance=0mm) (All)"


def _track_key(row):
    fields = {"x1", "y1", "x2", "y2", "width", "layer", "is_keepout"}
    _require(isinstance(row, dict) and fields <= row.keys() <= fields | {"net"}, "Unsupported track fields")
    for field in ("x1", "y1", "x2", "y2", "width", "layer"):
        _require(isinstance(row[field], str) and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", row[field]),
                 "Invalid native integer: " + field)
    _require(int(row["width"]) > 0 and int(row["layer"]) > 0, "Invalid track width/layer")
    _require(row["is_keepout"] in {"True", "False"}, "Invalid keepout flag")
    _require("net" not in row or isinstance(row["net"], str) and row["net"].strip(), "Invalid track net")
    return tuple(sorted(row.items()))


def _tracks(native):
    keys = {key for key in native if key.startswith("track.")}
    _require(keys == {"track." + str(i) for i in range(579)}, "Requires 579 contiguous track records")
    return Counter(_track_key(native[key]) for key in keys)


def compare_tracks(before, after):
    """Compare enumerated native fields as a multiset, never track indices."""
    old, new = _tracks(before), _tracks(after)
    old_key, new_key = _track_key(OLD_TRACK), _track_key(NEW_TRACK)
    _require(old[old_key] == 1 and old[new_key] == 0 and new[new_key] == 1 and new[old_key] == 0,
             "Target track must match exactly once before and after")
    _require(old - new == Counter({old_key: 1}) and new - old == Counter({new_key: 1}),
             "Unexpected track multiset difference")
    return {"before_count": 579, "after_count": 579, "unchanged_count": 578,
            "removed": OLD_TRACK.copy(), "added": NEW_TRACK.copy(), "units": "altium_internal_coordinates"}


def compare_reports(before, after):
    """Descriptions identify comparable rows here, not complete native rule coverage."""
    rows = []
    for report, total in ((before, 158), (after, 157)):
        expected = {"warnings": 0, "violations": total, "rule_rows": 25, "waived": None, "health_issues": 0}
        _require(report["counts"] == expected, "Unexpected report counts; total alone cannot establish improvement")
        _require(report["rule_detail_counts_verified"] and report["health_summary_counts_verified"],
                 "Unverified report detail/health totals")
        mapping = {}
        for row in report["rule_rows"]:
            title, count = row["description"], row["count"]
            _require(title not in mapping and type(count) is int and count >= 0, "Duplicate or invalid rule row")
            mapping[title] = count
        _require(len(mapping) == 25 and sum(mapping.values()) == total, "Invalid rule row totals")
        rows.append(mapping)
    old, new = rows
    _require(old.keys() == new.keys(), "Rule descriptions/coverage changed")
    _require(old.get(ANTENNA) == 1 and new.get(ANTENNA) == 0, "Expected antenna removal missing")
    _require(all(new[key] == count for key, count in old.items() if key != ANTENNA),
             "Other rule findings changed; one removed finding cannot offset another")
    for prefix in ("Short-Circuit Constraint", "Un-Routed Net Constraint", "Clearance Constraint"):
        matched = [count for key, count in new.items() if key.startswith(prefix)]
        _require(matched and all(count == 0 for count in matched), "Nonzero or missing " + prefix)
    return {"before": before["counts"], "after": after["counts"], "rule_deltas": {ANTENNA: -1}}


def _options(raw):
    def invalid(value):
        raise ValueError("Non-finite option value: " + value)
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=invalid)
    _require(isinstance(value, list) and value, "Missing observed options")
    result = {}
    for row in value:
        _require(isinstance(row, dict) and set(row) == {"name", "value"}
                 and all(isinstance(item, str) and item.strip() for item in row.values()), "Malformed option observation")
        _require(row["name"] not in result, "Duplicate option name")
        result[row["name"]] = row["value"]
    return result


def _load(folder, mode, read):
    result_raw = read(folder / "result.json")
    _pin(result_raw, RESULT_PINS[folder.name], "Pinned result")
    data = _json(result_raw)
    _require(data["status"] == "completed" and data["mode"] == mode, "Wrong action or incomplete result")
    native = data["native"]
    _require(native["completion"] == {"status": "completed"}
             and native["job"]["request"] == data["request"], "Native completion/request mismatch")
    board, source = _file(data["board"]), _file(data["source"])
    _require(board.parent.resolve() == folder.resolve() and not board.samefile(source)
             and _file(native["job"]["board"]).resolve() == board.resolve(), "Board identity mismatch")
    _require(data["source_unchanged"] is True, "Source unchanged assertion missing")
    _pin(read(source), data["source_sha256"], "Original source")
    _pin(read(board), data["source_snapshot_sha256"], "Saved board snapshot")
    if mode != "StubRepair":
        _require(data["source_snapshot_sha256"] == data["source_sha256"], "Baseline snapshot changed")
    response = read(folder / "response.ini")
    _pin(response, data["response_sha256"], "Native response")
    ini = configparser.ConfigParser(interpolation=None, strict=True)
    ini.read_string(response.decode("utf-8-sig"))
    _require(not ini.defaults() and {s: dict(ini[s]) for s in ini.sections()} == native, "Native response mismatch")
    script = read(folder / "Job.pas")
    _pin(script, SCRIPT_PINS[folder.name], "Script bytes")
    normalized = script.decode("ascii").replace("\r\n", "\n").replace("\r", "\n").encode("ascii")
    _pin(normalized, data["template_sha256"], "Script normalized text")
    if mode == "Copper":
        _require(data["artifacts"] == {} and native["job"]["units"] == "altium_internal_coordinates",
                 "Unexpected copper evidence profile")
        return data, None, {}
    _require(set(data["artifacts"]) == {"native-drc.html"}, "Unexpected report artifact manifest")
    raw = read(folder / "native-drc.html")
    _pin(raw, data["artifacts"]["native-drc.html"], "Native report")
    report = parse_drc_html(raw, board)
    observed = {}
    for name, digest in OPTION_PINS.items():
        raw = read(folder / name)
        _pin(raw, digest, "Observed " + name)
        observed[name] = _options(raw)
    _require(len(observed["batch-effective.json"]) == 55, "Missing batch observations")
    native_return = native["drc"]["native_return"] if mode == "StubRepair" else native["job"]["native_return"]
    _require(native_return == "False", "Unexpected pinned native return")
    return data, report, observed


def accept(candidate=CANDIDATE):
    """Only the fixed candidate can receive local repair acceptance; no native calls."""
    result = {"schema": 1, "repair_acceptance": "rejected", "scope": "pinned_local_track_repair_only",
              "native_drc_pass": False, "signal_integrity_verified": False, "manufacturing_authorized": False,
              "automatic_routing_authorized": False, "full_rule_coverage_verified": False,
              "other_objects_preserved_verified": False,
              "limitations": ["157 native violations remain; native DRC returned False",
                              "Signal Integrity completion and waiver coverage are unproven",
                              "Only enumerated track fields were compared; pads, vias, polygons and other objects are not fully proven unchanged",
                              "Matching UI options are pinned observations, not proof all analyses completed"]}
    inputs = {}
    def read(path):
        path = _file(path)
        raw = path.read_bytes()
        if path in inputs:
            _require(inputs[path] == raw, "Evidence changed during read")
        inputs[path] = raw
        return raw
    try:
        _require(Path(candidate).absolute() == CANDIDATE, "Candidate is not the fixed accepted repair target")
        baseline, before_report, before_options = _load(BASELINE, "DRCSetup", read)
        copper, _, _ = _load(COPPER, "Copper", read)
        repaired, after_report, after_options = _load(CANDIDATE, "StubRepair", read)
        _require(all(data["source"] == baseline["source"] and data["source_sha256"] == baseline["source_sha256"]
                     for data in (copper, repaired)), "Evidence does not share the original source")
        _require(before_options == after_options, "Batch or report options changed")
        job = repaired["native"]["job"]
        _require(job["before_tracks"] == "579" and job["exact_matches"] == "1"
                 and job["repair"] == "extend_exact_gnd_track_into_c12_at_native_minimum_width", "Wrong repair operation")
        track_diff = compare_tracks(copper["native"], repaired["native"])
        report_diff = compare_reports(before_report, after_report)
        _require(all(_file(path).read_bytes() == raw for path, raw in inputs.items()), "Evidence changed during acceptance")
        result.update(repair_acceptance="passed", candidate=str(CANDIDATE), board=repaired["board"],
                      track_diff=track_diff, report_diff=report_diff, batch_and_options_equal=True,
                      native_return=False, source_unchanged=True,
                      evidence_sha256={str(path): _sha(raw) for path, raw in inputs.items()})
    except (ValueError, OSError, KeyError, TypeError, configparser.Error) as error:
        result["errors"] = [str(error)]
    return result


if __name__ == "__main__":
    outcome = accept()
    print(json.dumps(outcome, indent=2))
    raise SystemExit(0 if outcome["repair_acceptance"] == "passed" else 1)
