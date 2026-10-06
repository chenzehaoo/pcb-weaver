"""Validate pinned, read-only native numeric constraints. Not a routing permit."""
import configparser
import hashlib
import json
from pathlib import Path

from altium_inventory_gate import validate_inventory

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / 'docs/validation/altium-native/ccaa1e480e1446918ad46ee9904dc6be/result.json'
RESULT_SHA = '1a8809a2a01deb30475a8ba151d2111161e61bdd94072a0643e3147d3aaa421e'
INVENTORY = ROOT / 'docs/validation/altium-native/a201905ecc7b4c3eb819e531d52c5f1e/result.json'
INVENTORY_SHA = '2d306cfd23c1c5ec5fbdee41991bb494d9a23103012999477c7dc45380f5f4ad'
SOURCE_SHA = 'ec13cc84307c6393263624a1a2f4c10ddb767640b2860c3e71e655098a87e031'
FIELDS = ('name', 'kind', 'scope1', 'scope2', 'priority', 'drc_enabled')


def require(value, message):
    if not value:
        raise ValueError(message)


def numeric(row, key):
    value = row.get(key)
    require(isinstance(value, str) and value.isascii() and value.isdigit(), 'Invalid ' + key)
    return int(value)


def validate_rules(native, inventory):
    keys = [key for key in native if key.startswith('rule.')]
    expected = [key for key in inventory if key.startswith('rule.')]
    require(set(keys) == set(expected) and bool(keys), 'Rule coverage mismatch')
    captured, missing = [], []
    for key in sorted(keys, key=lambda key: int(key.split('.')[1])):
        row = native[key]
        require(all(row.get(field) == inventory[key].get(field) for field in FIELDS),
                'Rule metadata mismatch: ' + key)
        kind = numeric(row, 'kind')
        if kind == 0:
            numeric(row, 'gap')
        elif kind in (2, 51):
            for layer in range(1, 33):
                fields = ('min_width', 'favored_width', 'max_width') if kind == 2 else (
                    'min_gap', 'preferred_gap', 'max_gap')
                low, preferred, high = [numeric(row, f'{field}_{layer}') for field in fields]
                require(low <= preferred <= high, 'Invalid layer constraint range')
            if kind == 2:
                require(row.get('impedance_driven') in ('True', 'False'), 'Missing impedance mode')
            else:
                numeric(row, 'max_uncoupled_length')
        elif kind == 9:
            require(all(row.get(f'routing_allowed_{layer}') in ('True', 'False')
                        for layer in range(1, 33)), 'Incomplete routing layer flags')
        elif kind == 11:
            for suffix in ('width', 'hole_width'):
                low, preferred, high = [numeric(row, f'{prefix}_{suffix}')
                                         for prefix in ('min', 'preferred', 'max')]
                require(low <= preferred <= high, 'Invalid via constraint range')
            numeric(row, 'via_style')
        else:
            require(row.get('numeric_profile') == 'not_captured', 'Unknown numeric profile')
            missing.append({field: row[field] for field in FIELDS})
            continue
        captured.append({'section': key, **row})
    return captured, missing


def snapshot():
    inputs = {}

    def read(path, digest):
        path = Path(path)
        raw = path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == digest, 'Evidence hash mismatch: ' + str(path))
        inputs[path] = raw
        return raw

    data = json.loads(read(RESULT, RESULT_SHA))
    inv = json.loads(read(INVENTORY, INVENTORY_SHA))
    gate = validate_inventory(INVENTORY, expected_source_sha256=SOURCE_SHA)
    require(gate['inventory_valid'], 'Invalid original inventory')
    require(data['status'] == 'completed' and data['mode'] == 'Constraints', 'Incomplete native capture')
    require(data['source_sha256'] == data['source_snapshot_sha256'] == SOURCE_SHA, 'Source binding mismatch')
    read(data['source'], SOURCE_SHA)
    read(data['board'], SOURCE_SHA)
    native = data['native']
    require(native['job']['request'] == data['request'] and
            Path(native['job']['board']).resolve() == Path(data['board']).resolve(), 'Native identity mismatch')
    require(native['completion']['status'] == 'completed', 'Missing native completion')
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(read(RESULT.parent / 'response.ini', data['response_sha256']).decode('utf-8-sig'))
    require(not parser.defaults() and {s: dict(parser[s]) for s in parser.sections()} == native,
            'Native response mismatch')
    captured, missing = validate_rules(native, inv['native'])
    require(all(path.read_bytes() == raw for path, raw in inputs.items()), 'Evidence changed during read')
    return {'scope': 'pinned_native_snapshot_not_live', 'status': 'partial',
            'units': 'altium_internal_coordinates', 'rule_count': len(captured) + len(missing),
            'captured_numeric_rules': captured, 'uncaptured_numeric_rules': missing,
            'physical_stack_verified': False, 'clearance_matrix_verified': False,
            'rule_scope_evaluation_verified': False, 'full_rule_coverage_verified': False,
            'automatic_routing_authorized': False, 'result_sha256': RESULT_SHA}


if __name__ == '__main__':
    print(json.dumps(snapshot(), indent=2))
