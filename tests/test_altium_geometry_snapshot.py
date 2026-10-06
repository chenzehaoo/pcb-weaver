"""Offline captured-evidence and synthetic mutation tests; no native execution."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('geometry_snapshot', SCRIPTS / 'altium_geometry_snapshot.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def evidence():
    return (json.loads(module.RESULT.read_bytes()), json.loads(module.INVENTORY.read_bytes()))


def test_captured_geometry_offline(evidence):
    data, inv = evidence
    layers, pads, polygons = module.validate_geometry(data['native'], inv['native'])
    assert layers == [1, 2, 3, 32]
    assert len(pads) == 200 and len(polygons) == 4
    assert all(len(pad['padstack']) == 4 for pad in pads)
    assert sum(len(p['segments']) for p in polygons) == 60
    arcs = [s for p in polygons for s in p['segments'] if s['kind'] == '1']
    assert len(arcs) == 12
    assert polygons[0]['segments'][0] == data['native']['polygon.0.segment.0']
    assert pads[2]['padstack'][1]['x_size'] == 0


@pytest.mark.parametrize('field', module.PAD_FIELDS)
def test_pad_identity_mismatch(evidence, field):
    data, inv = evidence
    data['native']['pad.2'][field] = 'changed'
    with pytest.raises(ValueError, match='identity'):
        module.validate_geometry(data['native'], inv['native'])


@pytest.mark.parametrize('section,field,value', [
    ('layer.1', 'id', '1'), ('layer.1', 'id', '0'),
    ('pad.2', 'x_size_2', '-1'), ('pad.2', 'shape_3', 'NaN'),
    ('pad.2', 'hole_rotation', 'Infinity'), ('pad.2', 'plated', 'yes'),
    ('polygon.0', 'net', 'unknown'), ('polygon.0', 'layer', '31'),
    ('polygon.0', 'point_count', '16'), ('polygon.0', 'is_keepout', 'yes'),
    ('polygon.0.segment.0', 'kind', '2'), ('polygon.0.segment.0', 'radius', '0'),
    ('polygon.0.segment.0', 'angle1', 'NaN'), ('polygon.0.segment.0', 'vx', '1.5'),
])
def test_malformed_geometry_rejected(evidence, section, field, value):
    data, inv = evidence
    data['native'][section][field] = value
    with pytest.raises(ValueError):
        module.validate_geometry(data['native'], inv['native'])


@pytest.mark.parametrize('section,field', [
    ('pad.2', 'y_size_32'), ('polygon.0.segment.0', 'cx'),
    ('layer.1', None), ('pad.199', None), ('polygon.0.segment.4', None),
])
def test_missing_capture_rejected(evidence, section, field):
    data, inv = evidence
    if field:
        del data['native'][section][field]
    else:
        del data['native'][section]
    with pytest.raises(ValueError):
        module.validate_geometry(data['native'], inv['native'])


@pytest.mark.parametrize('key', ['polygon.0.segment.15', 'polygon.9.segment.0', 'keepout.0'])
def test_extra_sections_rejected(evidence, key):
    data, inv = evidence
    data['native'][key] = {'kind': '0', 'vx': '0', 'vy': '0'}
    with pytest.raises(ValueError):
        module.validate_geometry(data['native'], inv['native'])


@pytest.fixture
def sandbox_snapshot(tmp_path, monkeypatch, evidence):
    """Synthetic file bindings around captured values, not a new native run."""
    data, inv = copy.deepcopy(evidence)
    source = tmp_path / 'source.PcbDoc'
    source.write_bytes(b'synthetic board bytes, not a native PCB')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    paths = []
    for name, item in [('geometry', data), ('inventory', inv)]:
        directory = tmp_path / name
        directory.mkdir()
        board = directory / 'WiFi.PcbDoc'
        board.write_bytes(source.read_bytes())
        item.update(source=str(source), board=str(board), source_sha256=digest,
                    source_snapshot_sha256=digest)
        item['native']['job']['board'] = str(board)
        response = directory / 'response.ini'
        text = ''.join('[' + key + ']\n' + ''.join(f'{k}={v}\n' for k, v in row.items())
                       for key, row in item['native'].items())
        response.write_text(text, encoding='utf-8')
        item['response_sha256'] = hashlib.sha256(response.read_bytes()).hexdigest()
        result = directory / 'result.json'
        result.write_text(json.dumps(item), encoding='utf-8')
        paths.append(result)
    monkeypatch.setattr(module, 'SOURCE_SHA', digest)
    for key, path in zip(('RESULT', 'INVENTORY'), paths):
        monkeypatch.setattr(module, key, path)
        monkeypatch.setattr(module, key + '_SHA', hashlib.sha256(path.read_bytes()).hexdigest())
    return source, paths[0]


def test_synthetic_file_snapshot_stays_partial(sandbox_snapshot):
    result = module.snapshot()
    assert result['status'] == 'partial'
    assert result['scope'] == 'pinned_native_snapshot_not_live'
    assert result['integrity']['pad_identity_matches_inventory'] is True
    for key in ('automatic_routing_authorized', 'manufacturing_authorized', 'all_geometry_verified',
                'holes_fully_captured', 'custom_pad_shapes_fully_captured', 'keepouts_fully_captured'):
        assert result[key] is False


@pytest.mark.parametrize('target', ['source', 'board', 'result', 'response', 'inventory'])
def test_synthetic_bytes_tampering_rejected(sandbox_snapshot, target):
    source, result = sandbox_snapshot
    paths = {'source': source, 'board': result.parent / 'WiFi.PcbDoc', 'result': result,
             'response': result.parent / 'response.ini', 'inventory': module.INVENTORY}
    paths[target].write_bytes(paths[target].read_bytes() + b'changed')
    with pytest.raises(ValueError):
        module.snapshot()


@pytest.mark.parametrize('change', ['status', 'mode', 'request', 'source_unchanged', 'response'])
def test_synthetic_rehashed_invalid_bindings_rejected(sandbox_snapshot, monkeypatch, change):
    _, path = sandbox_snapshot
    data = json.loads(path.read_bytes())
    if change == 'response':
        data['native']['pad.2']['mode'] = '1'
    elif change == 'source_unchanged':
        data[change] = False
    else:
        data[change] = 'invalid'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setattr(module, 'RESULT_SHA', hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError):
        module.snapshot()


def test_synthetic_source_alias_rejected(sandbox_snapshot, monkeypatch):
    source, path = sandbox_snapshot
    data = json.loads(path.read_bytes())
    data['board'] = str(source)
    data['native']['job']['board'] = str(source)
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setattr(module, 'RESULT_SHA', hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match='separate copy'):
        module.snapshot()


def test_synthetic_evidence_changed_during_read_rejected(sandbox_snapshot, monkeypatch):
    source, _ = sandbox_snapshot
    original = module.validate_geometry

    def change_after_validation(native, inventory):
        result = original(native, inventory)
        source.write_bytes(b'changed during validation')
        return result

    monkeypatch.setattr(module, 'validate_geometry', change_after_validation)
    with pytest.raises(ValueError, match='changed during read'):
        module.snapshot()
