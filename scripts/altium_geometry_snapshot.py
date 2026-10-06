"""Validate a pinned native geometry capture, not live geometry or a routing permit."""
import configparser
import hashlib
import json
from pathlib import Path
import re

from altium_inventory_gate import _file, _numeric, _sections, _unique_object, validate_inventory

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / 'docs/validation/altium-native/233b217e8c3a4170b748045f9f586dae/result.json'
RESULT_SHA = 'b0026876cd1f185e464236d9afaa6a858df7650c3195657f8d9bd9630cb9e89f'
INVENTORY = ROOT / 'docs/validation/altium-native/a201905ecc7b4c3eb819e531d52c5f1e/result.json'
INVENTORY_SHA = '2d306cfd23c1c5ec5fbdee41991bb494d9a23103012999477c7dc45380f5f4ad'
SOURCE_SHA = 'ec13cc84307c6393263624a1a2f4c10ddb767640b2860c3e71e655098a87e031'
PAD_FIELDS = ('name', 'component', 'net', 'x', 'y', 'layer', 'rotation', 'hole_size')
UNITS = 'altium_internal_coordinates'
NATIVE_SCOPE = 'electrical_layers_padstack_polygon_definitions_not_all_geometry'


def require(value, message):
    if not value:
        raise ValueError(message)


def integer(row, key, minimum=0):
    return int(_numeric(row, key, integer=True, minimum=minimum))


def validate_geometry(native, inventory):
    """Check captured structure against inventory; retain raw coordinates and arcs."""
    layers = _sections(native, 'layer')
    layer_ids = [integer(row, 'id', 1) for row in layers]
    require(all(set(row) == {'id'} for row in layers) and
            len(set(layer_ids)) == len(layer_ids), 'Invalid or duplicate electrical layers')
    pads, expected_pads = _sections(native, 'pad'), _sections(inventory, 'pad')
    require(len(pads) == len(expected_pads) == 200, 'Pinned pad coverage mismatch')
    pad_output = []
    for index, (row, expected) in enumerate(zip(pads, expected_pads)):
        require({k: row[k] for k in PAD_FIELDS if k in row} ==
                {k: expected[k] for k in PAD_FIELDS if k in expected},
                'Pad identity/placement mismatch: pad.' + str(index))
        for key in ('x', 'y'):
            integer(row, key, None)
        _numeric(row, 'rotation')
        _numeric(row, 'hole_rotation')
        for key in ('layer', 'mode', 'hole_type', 'hole_size', 'hole_width'):
            integer(row, key)
        require(row.get('plated') in ('True', 'False'), 'Invalid plated flag')
        stack = []
        fields = set(PAD_FIELDS) | {'mode', 'hole_type', 'hole_width', 'hole_rotation', 'plated'}
        for layer in layer_ids:
            stack.append({'layer_id': layer, **{
                field: integer(row, f'{field}_{layer}') for field in ('x_size', 'y_size', 'shape')}})
            fields.update(f'{field}_{layer}' for field in ('x_size', 'y_size', 'shape'))
        require(set(row) == ((fields - {'component', 'net'}) | (set(row) & {'component', 'net'})),
                'Unexpected or missing padstack fields')
        pad_output.append({'section': f'pad.{index}', 'raw': dict(row), 'padstack': stack})

    roots = {key: row for key, row in native.items() if re.fullmatch(r'polygon\.[0-9]+', key)}
    polygons = _sections(roots, 'polygon')
    known = {'job', 'completion'} | {f'layer.{i}' for i in range(len(layers))} | {
        f'pad.{i}' for i in range(len(pads))} | set(roots)
    net_names = {row['name'] for row in _sections(inventory, 'net')}
    polygon_output = []
    for index, row in enumerate(polygons):
        key = f'polygon.{index}'
        require(set(row) in ({'layer', 'is_keepout', 'point_count'},
                             {'layer', 'is_keepout', 'point_count', 'net'}), 'Invalid polygon fields')
        require(integer(row, 'layer', 1) in layer_ids, 'Polygon outside captured electrical layers')
        require(row.get('is_keepout') in ('True', 'False'), 'Invalid polygon keepout flag')
        require('net' not in row or row['net'] in net_names, 'Unknown polygon net')
        count = integer(row, 'point_count', 1)
        require(count <= len(native), 'Polygon segment count exceeds capture')
        segments = []
        for i in range(count):
            segment_key = f'{key}.segment.{i}'
            segment = native.get(segment_key)
            require(isinstance(segment, dict), 'Missing polygon segment: ' + segment_key)
            kind = integer(segment, 'kind')
            require(kind in (0, 1), 'Unsupported polygon segment kind')
            fields = {'kind', 'vx', 'vy'}
            for field in ('vx', 'vy'):
                integer(segment, field, None)
            if kind == 1:
                fields.update(('cx', 'cy', 'radius', 'angle1', 'angle2'))
                for field in ('cx', 'cy'):
                    integer(segment, field, None)
                integer(segment, 'radius', 1)
                for field in ('angle1', 'angle2'):
                    _numeric(segment, field)
            require(set(segment) == fields, 'Unexpected or missing segment fields')
            segments.append(dict(segment))
            known.add(segment_key)
        polygon_output.append({'section': key, **row, 'segments': segments})
    require(set(native) == known, 'Unexpected geometry sections or orphan segments')
    return layer_ids, pad_output, polygon_output


def snapshot():
    inputs = {}

    def read(path, digest):
        path = _file(str(Path(path).absolute()))
        raw = path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == digest, 'Evidence hash mismatch: ' + str(path))
        inputs[path] = raw
        return raw

    data = json.loads(read(RESULT, RESULT_SHA), object_pairs_hook=_unique_object)
    inv = json.loads(read(INVENTORY, INVENTORY_SHA), object_pairs_hook=_unique_object)
    gate = validate_inventory(INVENTORY, expected_source_sha256=SOURCE_SHA)
    require(gate['inventory_valid'], 'Invalid original inventory')
    require(gate['integrity']['result_sha256'] == INVENTORY_SHA, 'Inventory changed during read')
    read(INVENTORY.parent / 'response.ini', gate['integrity']['response_sha256'])
    read(inv['board'], SOURCE_SHA)
    require(data['status'] == 'completed' and data['mode'] == 'Geometry', 'Incomplete native capture')
    require(data.get('source_unchanged') is True and
            data['source_sha256'] == data['source_snapshot_sha256'] == SOURCE_SHA, 'Source binding mismatch')
    source, board = _file(data['source']), _file(data['board'])
    require(source.resolve() == _file(inv['source']).resolve(), 'Inventory source mismatch')
    require(board.parent.resolve() == RESULT.parent.resolve() and not source.samefile(board),
            'Snapshot must be a separate copy beside result.json')
    read(source, SOURCE_SHA)
    read(board, SOURCE_SHA)
    native = data['native']
    require(native['job']['request'] == data['request'] and
            _file(native['job']['board']).resolve() == board.resolve(), 'Native identity mismatch')
    require(native['job']['units'] == UNITS and native['job']['scope'] == NATIVE_SCOPE,
            'Unsupported geometry units or scope')
    require(native['completion']['status'] == 'completed', 'Missing native completion')
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(read(RESULT.parent / 'response.ini', data['response_sha256']).decode('utf-8-sig'))
    require(not parser.defaults() and {s: dict(parser[s]) for s in parser.sections()} == native,
            'Native response mismatch')
    layers, pads, polygons = validate_geometry(native, inv['native'])
    require(all(path.read_bytes() == raw for path, raw in inputs.items()), 'Evidence changed during read')
    return {'scope': 'pinned_native_snapshot_not_live', 'status': 'partial', 'units': UNITS,
            'electrical_layer_ids': layers, 'pad_count': len(pads), 'pads': pads,
            'polygon_count': len(polygons), 'polygon_definitions': polygons,
            'holes_fully_captured': False, 'custom_pad_shapes_fully_captured': False,
            'keepouts_fully_captured': False, 'all_geometry_verified': False,
            'physical_stack_verified': False, 'poured_copper_verified': False,
            'automatic_routing_authorized': False, 'manufacturing_authorized': False,
            'native_drc': 'not_evaluated',
            'limitations': ['No complete board outline, cutouts or region holes',
                            'Pad shape codes and sizes do not capture custom shape contours',
                            'Polygon keepout flags are not a complete keepout inventory',
                            'No complete tracks, vias, arcs or physical stackup inventory',
                            'File consistency only; no independent native execution verification'],
            'integrity': {'source_sha256': SOURCE_SHA, 'snapshot_sha256': SOURCE_SHA,
                          'source_unchanged': True, 'native_response_matches_result': True,
                          'pad_identity_matches_inventory': True},
            'request': data['request'], 'result_sha256': RESULT_SHA,
            'response_sha256': data['response_sha256'], 'inventory_sha256': INVENTORY_SHA}


if __name__ == '__main__':
    print(json.dumps(snapshot(), indent=2, allow_nan=False))
