"""Portable engineering data, explicitly separate from manufacturing releases."""
import csv
import hashlib
import io
import json
import zipfile

from .storage import canonical


TABLES = {
    "components": ("reference", "value", "footprint", "category", "category_basis", "manufacturer", "mpn", "datasheet", "layer", "x", "y", "rotation", "locked", "dnp"),
    "nets": ("name", "pad_count", "references", "track_count", "via_count", "length_mm", "length_status", "length_scope", "min_width_mm", "max_width_mm", "layers", "connectivity_status"),
    "tracks": ("id", "kind", "net", "layer", "start", "mid", "end", "width_mm", "length_mm", "length_status", "geometry_supported"),
    "vias": ("id", "net", "x", "y", "diameter_mm", "drill_mm", "layers"),
}


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        value = canonical(value)
    if isinstance(value, str) and value.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_inventory(inventory, format="json"):
    if format == "json":
        raw = json.dumps(inventory, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        content_type = "application/json"
    elif format in {name + ".csv" for name in TABLES}:
        name = format.removesuffix(".csv")
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        columns = ("project", "revision", "revision_digest", *TABLES[name])
        writer.writerow(columns)
        for row in inventory[name]:
            writer.writerow(csv_value(inventory.get(key) if key in columns[:3] else row.get(key)) for key in columns)
        raw = stream.getvalue().encode("utf-8-sig")
        content_type = "text/csv; charset=utf-8"
    else:
        raise ValueError("Choose json, components.csv, nets.csv, tracks.csv or vias.csv")
    if len(raw) > 50_000_000:
        raise ValueError("Engineering export exceeds 50 MB")
    return raw, content_type


def exchange_bundle(inventory):
    files = {"inventory.json": export_inventory(inventory)[0]}
    files.update({name + ".csv": export_inventory(inventory, name + ".csv")[0] for name in TABLES})
    manifest = {
        "schema_version": "1.0", "purpose": "engineering_data_exchange",
        "manufacturing_authorized": False, "project": inventory["project"],
        "revision": inventory["revision"], "revision_digest": inventory["revision_digest"],
        "units": inventory["units"], "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "scope": "Data integrity only; no corporate connector certification or manufacturing signoff",
    }
    if sum(map(len, files.values())) > 50_000_000:
        raise ValueError("Engineering exchange exceeds 50 MB")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in {**files, "manifest.json": canonical(manifest).encode("utf-8")}.items():
            entry = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, raw)
    return output.getvalue()
