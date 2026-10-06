import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import altium_route_native as route


def test_prepare_preserves_original_and_rejects_changed_copy(tmp_path, monkeypatch):
    source = tmp_path / 'blank.PcbDoc'
    source.write_bytes(b'known Altium sample bytes')
    monkeypatch.setattr(route, 'SOURCE', source)
    monkeypatch.setattr(route, 'SOURCE_SHA256', hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(route, 'RUN_ROOT', tmp_path / 'runs')
    board = route.prepare()
    assert board.read_bytes() == source.read_bytes()
    board.write_bytes(b'changed candidate')
    with pytest.raises(ValueError, match='modified'):
        route.prepare()
    assert source.read_bytes() == b'known Altium sample bytes'


def test_route_probe_rejects_external_board(tmp_path, monkeypatch):
    monkeypatch.setattr(route, 'RUN_ROOT', tmp_path / 'runs')
    outside = tmp_path / 'outside.PcbDoc'
    outside.write_bytes(b'board')
    with pytest.raises(ValueError, match='only edit a copied'):
        route.run('Inspect', outside)


def test_fixture_requires_unmodified_blank_board(tmp_path, monkeypatch):
    root = tmp_path / 'runs'
    root.mkdir()
    board = root / 'board.PcbDoc'
    board.write_bytes(b'not blank')
    monkeypatch.setattr(route, 'RUN_ROOT', root)
    monkeypatch.setattr(route, 'SOURCE_SHA256', hashlib.sha256(b'blank').hexdigest())
    with pytest.raises(ValueError, match='untouched blank'):
        route.run('Fixture', board)


def test_native_completion_requires_real_effect_and_inventory(tmp_path):
    with pytest.raises(ValueError, match='without changing'):
        route.validate_completion('Fixture', 'same', 'same', 'completed', {}, {})
    with pytest.raises(ValueError, match='changed the copied'):
        route.validate_completion('Inspect', 'old', 'new', 'completed', {}, {})
    with pytest.raises(ValueError, match='missing tracks'):
        route.validate_completion('Inspect', 'same', 'same', 'completed', {},
                                  {'job': {'components': '2', 'pads': '4', 'nets': '2'}})
    with pytest.raises(ValueError, match='did not produce'):
        route.validate_completion('Export', 'same', 'same', 'completed',
                                  {'OUTPUT': str(tmp_path / 'missing.dsn')}, {})
    with pytest.raises(ValueError, match='not implemented'):
        route.validate_completion('Import', 'old', 'new', 'completed', {}, {})


def test_native_completion_accepts_matching_evidence(tmp_path):
    native = {'job': dict.fromkeys(('components', 'pads', 'nets', 'tracks', 'vias', 'polygons'), '0')}
    route.validate_completion('Inspect', 'same', 'same', 'completed', {}, native)
    output = tmp_path / 'board.dsn'
    output.write_text('(pcb example)')
    route.validate_completion('Export', 'same', 'same', 'completed', {'OUTPUT': str(output)}, {})


def test_export_rejects_stale_or_non_dsn_output(tmp_path, monkeypatch):
    root = tmp_path / 'runs'
    root.mkdir()
    board = root / 'board.PcbDoc'
    board.write_bytes(b'copy')
    monkeypatch.setattr(route, 'RUN_ROOT', root)
    with pytest.raises(ValueError, match='DSN output'):
        route.run('Export', board, {'OUTPUT': str(root / 'board.ses')})
    stale = root / 'board.dsn'
    stale.write_text('(pcb old)')
    with pytest.raises(ValueError, match='must be new'):
        route.run('Export', board, {'OUTPUT': str(stale)})


def test_native_probe_requires_all_altium_instances_closed(monkeypatch):
    if route.os.name != 'nt':
        pytest.skip('Altium process check is Windows-only')
    monkeypatch.setattr(route.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=2))
    with pytest.raises(RuntimeError, match='Close all Altium'):
        route.require_closed_altium()
    monkeypatch.setattr(route.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match='Could not verify'):
        route.require_closed_altium()
    monkeypatch.setattr(route.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    route.require_closed_altium()


def test_current_instance_dispatch_requires_open_altium(monkeypatch):
    if route.os.name != 'nt':
        pytest.skip('Altium process check is Windows-only')
    monkeypatch.setattr(route.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match='requires an open'):
        route.require_open_altium()
    monkeypatch.setattr(route.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=2))
    route.require_open_altium()


def test_native_probe_rejects_unbounded_timeout(tmp_path, monkeypatch):
    root = tmp_path / 'runs'
    root.mkdir()
    board = root / 'board.PcbDoc'
    board.write_bytes(b'copy')
    monkeypatch.setattr(route, 'RUN_ROOT', root)
    with pytest.raises(ValueError, match='1-900'):
        route.run('Inspect', board, timeout=901, dispatch='current')


def test_reconcile_late_native_inspection(tmp_path, monkeypatch):
    root = tmp_path / 'runs'
    board = root / 'fixture' / 'board.PcbDoc'
    board.parent.mkdir(parents=True)
    board.write_bytes(b'blank board')
    job = root / 'native' / 'job'
    job.mkdir(parents=True)
    monkeypatch.setattr(route, 'RUN_ROOT', root)
    result = {
        'job': str(job), 'mode': 'Inspect', 'dispatch': 'current', 'status': 'failed',
        'error': 'No native response; Altium requires manual inspection',
        'board': str(board), 'before_sha256': route.sha(board),
        'response': str(job / 'response.ini'), 'request': 'known-request',
    }
    (job / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    (job / 'response.ini').write_text(
        '[job]\nrequest=known-request\nboard=' + str(board) + '\n'
        'components=0\npads=0\nnets=0\ntracks=0\nvias=0\npolygons=0\n'
        '[completion]\nstatus=completed\n', encoding='utf-8')

    reconciled = route.reconcile_inspect(job)
    assert reconciled['status'] == 'completed'
    assert reconciled['orchestration_status'] == 'timed_out'
    assert reconciled['late_response'] is True
    assert reconciled['native']['job']['tracks'] == '0'
    assert reconciled['before_sha256'] == reconciled['after_sha256']
    assert json.loads((job / 'result.json').read_text(encoding='utf-8')) == reconciled

    result_path = job / 'result.json'
    result_path.write_text(json.dumps(result), encoding='utf-8')
    response_path = job / 'response.ini'
    response_path.write_text(response_path.read_text(encoding='utf-8').replace(
        'known-request', 'different-request'), encoding='utf-8')
    with pytest.raises(ValueError, match='identity mismatch'):
        route.reconcile_inspect(job)
    assert json.loads(result_path.read_text(encoding='utf-8')) == result
