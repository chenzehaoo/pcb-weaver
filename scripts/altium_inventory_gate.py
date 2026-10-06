"""Read-only Inventory evidence checks; never authorizes routing or claims native DRC."""
import argparse
import configparser
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re


MISSING_SEMANTICS = (
    "layerstack", "padstack", "keepouts", "polygons", "rule_numeric_values",
)
RULE_FIELDS = ("name", "kind", "scope1", "scope2", "drc_enabled", "priority")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def _sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _text(row, key, *, empty=False):
    value = row.get(key)
    _require(isinstance(value, str) and not any(ord(c) < 32 for c in value)
             and (empty or bool(value.strip())), "Missing or invalid text: " + key)
    return value


def _numeric(row, key, *, integer=False, minimum=None):
    value = row.get(key)
    _require(isinstance(value, str) and len(value) <= 128
             and re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value),
             "Invalid numeric field: " + key)
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid numeric field: " + key) from error
    _require(number.is_finite(), "Non-finite field: " + key)
    _require(not integer or number == number.to_integral_value(), "Non-integral field: " + key)
    _require(minimum is None or number >= minimum, "Out-of-range field: " + key)
    return number


def _sections(native, kind):
    prefix = kind + "."
    keys = [key for key in native if key.startswith(prefix)]
    _require(keys and set(keys) == {prefix + str(i) for i in range(len(keys))},
             "Missing or non-contiguous sections: " + kind)
    rows = [native[prefix + str(i)] for i in range(len(keys))]
    _require(all(isinstance(row, dict) for row in rows), "Invalid section: " + kind)
    return rows


def _names(rows, key):
    values = [_text(row, key) for row in rows]
    _require(len({value.casefold() for value in values}) == len(values),
             "Duplicate or case-ambiguous " + key)
    return set(values)


def _file(value):
    _require(isinstance(value, str) and bool(value), "Missing evidence file path")
    path = Path(value)
    _require(path.is_absolute() and path.is_file() and path.stat().st_size > 0,
             "Requires a nonempty absolute file: " + str(path))
    for item in (path, *path.parents):
        _require(not item.is_symlink() and not getattr(item, "is_junction", lambda: False)(),
                 "Evidence path contains a link or junction")
    return path


def _validate(result_path, expected_source_sha256):
    path = _file(str(Path(result_path).absolute()))
    raw = path.read_bytes()
    result = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    _require(isinstance(result, dict), "Expected Inventory result object")
    _require(result.get("status") == "completed" and result.get("mode") == "Inventory",
             "Requires completed Inventory, not export or DRC evidence")
    request = _text(result, "request")
    _require(result.get("source_unchanged") is True, "Original unchanged assertion missing")
    expected = result.get("source_sha256")
    _require(isinstance(expected, str) and re.fullmatch(r"[0-9a-fA-F]{64}", expected),
             "Invalid source SHA256")
    expected = expected.lower()
    if expected_source_sha256 is not None:
        _require(isinstance(expected_source_sha256, str)
                 and re.fullmatch(r"[0-9a-fA-F]{64}", expected_source_sha256)
                 and expected_source_sha256.lower() == expected, "Source differs from caller SHA256 pin")
    source, board = _file(result.get("source")), _file(result.get("board"))
    _require(board.parent.resolve() == path.parent.resolve(), "Snapshot must be beside result.json")
    _require(not source.samefile(board), "Snapshot must not alias original")
    _require(_sha(source) == expected, "Original source bytes changed")
    _require(_sha(board) == expected, "Inventory snapshot bytes differ from original; possible save")

    native = result.get("native")
    _require(isinstance(native, dict), "Missing native sections")
    _require(isinstance(native.get("job"), dict) and isinstance(native.get("completion"), dict),
             "Missing native job/completion sections")
    _require(native["job"].get("request") == request, "Native request mismatch")
    _require(native["job"].get("units") == "altium_internal_coordinates", "Unsupported inventory units")
    _require(_file(native["job"].get("board")).resolve() == board.resolve(), "Native board mismatch")
    _require(native["completion"].get("status") == "completed", "Native inventory incomplete")
    _require(all(key in {"job", "completion"} or re.fullmatch(r"(?:component|net|pad|rule)\.[0-9]+", key)
                 for key in native), "Unexpected inventory section; coverage must be reviewed")

    response = _file(str(path.parent / "response.ini"))
    response_raw = response.read_bytes()
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(response_raw.decode("utf-8-sig"))
    _require(not parser.defaults(), "Native response must not inherit DEFAULT values")
    _require({name: dict(parser[name]) for name in parser.sections()} == native,
             "Native response.ini differs from result.json")

    components, nets, pads, rules = [_sections(native, kind) for kind in ("component", "net", "pad", "rule")]
    component_names, net_names = _names(components, "designator"), _names(nets, "name")
    for row in components:
        for key in ("comment", "pattern"):
            _text(row, key, empty=True)
    for row in components + pads:
        for key in ("x", "y"):
            _numeric(row, key, integer=True)
        _numeric(row, "rotation")
        _numeric(row, "layer", integer=True, minimum=0)
    attached = assigned = 0
    for pad in pads:
        _text(pad, "name")
        for key, declared in (("component", component_names), ("net", net_names)):
            if key in pad:
                _require(_text(pad, key) in declared, "Unknown pad " + key + " reference")
        attached += "component" in pad
        assigned += "net" in pad
        for key in ("top_x_size", "top_y_size", "hole_size"):
            _numeric(pad, key, integer=True, minimum=0)
    for rule in rules:
        for key in ("name", "scope1", "scope2"):
            _text(rule, key)
        _numeric(rule, "kind", integer=True, minimum=0)
        _numeric(rule, "priority", integer=True, minimum=1)
        _require(rule.get("drc_enabled") in ("True", "False"), "Invalid native drc_enabled")

    # Recheck all four inputs after parsing; assertions in JSON alone are not proof.
    _require(_sha(source) == expected and _sha(board) == expected, "Board bytes changed during validation")
    _require(path.read_bytes() == raw and response.read_bytes() == response_raw,
             "Inventory evidence changed during validation")
    return {
        "inventory_status": "valid", "inventory_valid": True, "request": request,
        "counts": {"components": len(components), "pads": len(pads), "nets": len(nets), "rules": len(rules)},
        "coverage": {
            "component_identity_and_placement": "captured",
            "net_names": "captured", "pad_basic_geometry_and_references": "captured",
            "pads_with_component": attached, "standalone_pads": len(pads) - attached,
            "pads_with_net": assigned, "unassigned_pads": len(pads) - assigned,
            "rule_metadata_fields": list(RULE_FIELDS),
            "rules_drc_enabled": sum(rule["drc_enabled"] == "True" for rule in rules),
            "rules_drc_disabled": sum(rule["drc_enabled"] == "False" for rule in rules),
            "rule_scope_evaluation": "not_performed",
            **{key: "not_captured" for key in MISSING_SEMANTICS},
        },
        "rule_metadata": [{key: rule[key] for key in RULE_FIELDS} for rule in rules],
        "integrity": {"source": str(source), "snapshot": str(board), "source_sha256": expected,
                      "snapshot_sha256": expected, "source_unchanged": True,
                      "snapshot_byte_identical_to_original": True,
                      "result_sha256": hashlib.sha256(raw).hexdigest(),
                      "response_sha256": hashlib.sha256(response_raw).hexdigest(),
                      "native_response_matches_result": True,
                      "caller_sha256_pin_checked": expected_source_sha256 is not None},
    }


def validate_inventory(result_path, *, expected_source_sha256=None):
    """Validate existing files only; valid inventory still returns blocked readiness.

    The JSON/INI cross-check establishes consistency, not cryptographic native
    provenance or proof of an unsaved GUI session. No native tools are invoked.
    """
    gate = {"schema": 1, "status": "blocked", "inventory_status": "invalid", "inventory_valid": False,
            "full_autoroute": {"status": "blocked", "authorized": False,
                "missing_semantics": list(MISSING_SEMANTICS),
                "reason": "Inventory metadata is not complete routing constraints; native DRC is not established"},
            "native_drc": "not_verified_by_inventory", "manufacturing_authorized": False}
    try:
        gate.update(_validate(result_path, expected_source_sha256))
    except (ValueError, OSError, TypeError, KeyError, configparser.Error) as error:
        gate["errors"] = [str(error)]
    return gate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--expected-source-sha256")
    args = parser.parse_args(argv)
    result = validate_inventory(args.result, expected_source_sha256=args.expected_source_sha256)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 2 if result["inventory_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
