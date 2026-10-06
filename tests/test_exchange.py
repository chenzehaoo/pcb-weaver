import csv
import hashlib
import io
import json
import zipfile

import pytest

from pcb_weaver.exchange import csv_value, exchange_bundle, export_inventory


def fixture():
    return {"schema_version": "1.0", "project": "test", "revision": "r-1", "revision_digest": "abc", "units": "mm",
            "components": [{"reference": "R1", "value": '=HYPERLINK("unsafe")', "x": -1.2, "mpn": None}],
            "nets": [], "tracks": [], "vias": []}


@pytest.mark.parametrize("text", ["=1+1", "+1+1", "-formula", "@SUM(A1)", " \t=1", "\ufeff@X", "\r\n-1"])
def test_csv_formula_strings_are_literal(text):
    assert csv_value(text) == "'" + text
    assert csv_value(-1.2) == -1.2


def test_export_preserves_json_and_binds_csv_revision():
    inventory = fixture()
    raw, media = export_inventory(inventory)
    assert media == "application/json" and json.loads(raw) == inventory
    raw, media = export_inventory(inventory, "components.csv")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0]["value"].startswith("'=")
    assert rows[0]["x"] == "-1.2" and rows[0]["mpn"] == ""
    assert rows[0]["revision_digest"] == "abc"


def test_exchange_is_deterministic_hash_bound_and_not_a_release():
    data = fixture()
    raw = exchange_bundle(data)
    assert raw == exchange_bundle(data)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["manufacturing_authorized"] is False
        assert manifest["purpose"] == "engineering_data_exchange"
        assert set(archive.namelist()) == {"manifest.json", *manifest["files"]}
        for name, sha in manifest["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == sha


def test_invalid_export_format_and_nonfinite_json_rejected():
    with pytest.raises(ValueError):
        export_inventory(fixture(), "../../secret.csv")
    data = fixture()
    data["components"][0]["x"] = float("nan")
    with pytest.raises(ValueError):
        export_inventory(data)
