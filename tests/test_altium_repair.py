"""Read-only real repair regressions and synthetic rejection checks; no native calls."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    spec = importlib.util.spec_from_file_location("accept_altium_repair", ROOT / "scripts/accept_altium_repair.py")
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)
finally:
    sys.path.pop(0)

DELETED = repair.BASE / "cbdb34a0d550467e9eac33738c4c55c7"
SHIFTED = repair.BASE / "cc96a572ef4e4d83a591117d554f6310"


def _data(folder):
    return json.loads((folder / "result.json").read_bytes())


def _report(folder):
    data = _data(folder)
    raw = (folder / "native-drc.html").read_bytes()
    repair._pin(raw, data["artifacts"]["native-drc.html"], "Test report")
    return repair.parse_drc_html(raw, Path(data["board"]))


def test_fixed_native_repair_acceptance_is_partial_and_read_only():
    paths = [path for folder in (repair.BASELINE, repair.COPPER, repair.CANDIDATE)
             for path in folder.iterdir() if path.is_file()]
    paths.append(Path(_data(repair.BASELINE)["source"]))
    before = {path: repair._sha(path.read_bytes()) for path in paths}
    outcome = repair.accept()
    assert outcome["repair_acceptance"] == "passed", outcome
    assert outcome["track_diff"]["unchanged_count"] == 578
    assert outcome["track_diff"]["before_count"] == outcome["track_diff"]["after_count"] == 579
    assert outcome["report_diff"]["before"]["violations"] == 158
    assert outcome["report_diff"]["after"]["violations"] == 157
    assert outcome["report_diff"]["rule_deltas"] == {repair.ANTENNA: -1}
    assert outcome["batch_and_options_equal"] and outcome["source_unchanged"]
    assert outcome["native_return"] is False
    for key in ("native_drc_pass", "signal_integrity_verified", "manufacturing_authorized",
                "automatic_routing_authorized", "full_rule_coverage_verified", "other_objects_preserved_verified"):
        assert outcome[key] is False
    assert before == {path: repair._sha(path.read_bytes()) for path in paths}


@pytest.mark.parametrize("folder,total", [(DELETED, 159), (SHIFTED, 158)])
def test_real_failed_candidates_rejected_by_pin_tracks_and_reports(folder, total):
    outcome = repair.accept(folder)
    assert outcome["repair_acceptance"] == "rejected" and "fixed" in outcome["errors"][0]
    report = _report(folder)
    assert report["counts"]["violations"] == total
    with pytest.raises(ValueError):
        repair.compare_reports(_report(repair.BASELINE), report)
    with pytest.raises(ValueError):
        repair.compare_tracks(_data(repair.COPPER)["native"], _data(folder)["native"])
    rows = {row["description"]: row["count"] for row in report["rule_rows"]}
    if folder == SHIFTED:
        assert rows[repair.ANTENNA] == 0
        assert rows["Clearance Constraint (Gap=0.2mm) (All),(All)"] == 1
    else:
        assert rows[repair.ANTENNA] == 1
        assert rows["Un-Routed Net Constraint ( (All) )"] == 1


@pytest.mark.parametrize("fault", ["x1", "y1", "width", "layer", "net", "is_keepout", "extra_field",
    "missing_field", "nonfinite", "delete", "extra", "duplicate", "other_track", "gap", "no_change"])
def test_unapproved_track_changes_rejected(fault):
    before = _data(repair.COPPER)["native"]
    after = _data(repair.CANDIDATE)["native"]
    row = after["track.535"]
    if fault in {"x1", "y1", "width", "layer"}:
        row[fault] = str(int(row[fault]) + 1)
    elif fault in {"net", "is_keepout"}:
        row[fault] = "3V3" if fault == "net" else "True"
    elif fault == "extra_field":
        row["extra"] = "hidden"
    elif fault == "missing_field":
        del row["net"]
    elif fault == "nonfinite":
        row["width"] = "NaN"
    elif fault == "delete":
        del after["track.535"]
    elif fault == "extra":
        after["track.579"] = row.copy()
    elif fault == "duplicate":
        after["track.100"] = row.copy()
    elif fault == "other_track":
        after["track.100"]["width"] = "1"
    elif fault == "gap":
        after["track.579"] = after.pop("track.100")
    else:
        after["track.535"] = before["track.535"].copy()
    with pytest.raises(ValueError):
        repair.compare_tracks(before, after)


def test_track_comparison_ignores_enumeration_order_but_preserves_multiplicity():
    before = _data(repair.COPPER)["native"]
    after = _data(repair.CANDIDATE)["native"]
    after["track.535"], after["track.100"] = after["track.100"], after["track.535"]
    assert repair.compare_tracks(before, after)["unchanged_count"] == 578
    before["track.100"] = before["track.535"].copy()
    with pytest.raises(ValueError, match="exactly once"):
        repair.compare_tracks(before, after)


@pytest.fixture(scope="module")
def reports():
    return _report(repair.BASELINE), _report(repair.CANDIDATE)


@pytest.mark.parametrize("fault", ["short", "unrouted", "clearance", "other_increase", "other_decrease",
    "duplicate_row", "missing_row", "renamed_rule", "health", "warnings", "waived", "details", "antenna"])
def test_report_changes_cannot_be_offset_by_unrelated_improvements(reports, fault):
    before, after = copy.deepcopy(reports)
    rows = {row["description"]: row for row in after["rule_rows"]}
    if fault in {"short", "unrouted", "clearance", "other_increase"}:
        prefix = {"short": "Short-Circuit", "unrouted": "Un-Routed", "clearance": "Clearance Constraint",
                  "other_increase": "Width Constraint"}[fault]
        next(row for row in rows.values() if row["description"].startswith(prefix))["count"] += 1
        # Keep total at 157 to prove a headline count cannot mask a regression.
        next(row for row in rows.values() if row["description"].startswith("Minimum Solder"))["count"] -= 1
    elif fault == "other_decrease":
        next(row for row in rows.values() if row["count"] > 0)["count"] -= 1
    elif fault == "duplicate_row":
        after["rule_rows"][1] = after["rule_rows"][0].copy()
    elif fault == "missing_row":
        after["rule_rows"].pop()
    elif fault == "renamed_rule":
        after["rule_rows"][0]["description"] += " changed"
    elif fault in {"health", "warnings", "waived"}:
        after["counts"]["health_issues" if fault == "health" else fault] = 1
    elif fault == "details":
        after["rule_detail_counts_verified"] = False
    else:
        rows[repair.ANTENNA]["count"] = 1
    with pytest.raises(ValueError):
        repair.compare_reports(before, after)


@pytest.mark.parametrize("folder,name", [(repair.BASELINE, "result.json"), (repair.COPPER, "result.json"),
    (repair.CANDIDATE, "result.json"), (repair.CANDIDATE, "WiFi.PcbDoc"), (repair.COPPER, "WiFi.PcbDoc"),
    (repair.BASELINE, "WiFi.PcbDoc"), (repair.CANDIDATE, "response.ini"), (repair.CANDIDATE, "Job.pas"),
    (repair.CANDIDATE, "native-drc.html"), (repair.BASELINE, "batch-effective.json"),
    (repair.CANDIDATE, "batch-effective.json"), (repair.CANDIDATE, "report-options-effective.json")])
def test_any_pinned_evidence_tampering_rejected_without_writing(folder, name, monkeypatch):
    target = folder / name
    original = Path.read_bytes
    def tampered(path):
        raw = original(path)
        return raw + b" " if path == target else raw
    monkeypatch.setattr(Path, "read_bytes", tampered)
    outcome = repair.accept()
    assert outcome["repair_acceptance"] == "rejected" and "SHA256" in outcome["errors"][0]


def test_original_source_tamper_rejected(monkeypatch):
    target = Path(_data(repair.BASELINE)["source"])
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: b"changed" if path == target else original(path))
    assert repair.accept()["repair_acceptance"] == "rejected"


def test_missing_report_rejected(monkeypatch):
    original = Path.read_bytes
    def missing(path):
        if path == repair.CANDIDATE / "native-drc.html":
            raise FileNotFoundError("injected missing report")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", missing)
    assert repair.accept()["repair_acceptance"] == "rejected"


def test_evidence_change_after_comparison_rejected(monkeypatch):
    original = Path.read_bytes
    real_compare = repair.compare_reports
    state = {"compared": False}
    def compare(*args):
        outcome = real_compare(*args)
        state["compared"] = True
        return outcome
    def changed(path):
        raw = original(path)
        return raw + b" " if state["compared"] and path == repair.BASELINE / "result.json" else raw
    monkeypatch.setattr(repair, "compare_reports", compare)
    monkeypatch.setattr(Path, "read_bytes", changed)
    outcome = repair.accept()
    assert outcome["repair_acceptance"] == "rejected" and "changed during acceptance" in outcome["errors"][0]


@pytest.mark.parametrize("raw", [b'[]', b'{}', b'[{"name":"a","value":"x"},{"name":"a","value":"y"}]',
    b'[{"name":"a","name":"b","value":"x"}]', b'[{"name":"a","value":NaN}]',
    b'[{"name":"a"}]'])
def test_invalid_option_observations_rejected(raw):
    with pytest.raises(ValueError):
        repair._options(raw)
