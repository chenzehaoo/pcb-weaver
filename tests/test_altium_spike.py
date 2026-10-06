"""Contract checks only. Native evidence comes from accept_altium_spike.py."""
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('altium_spike',
    Path(__file__).resolve().parents[1] / 'scripts/altium_bridge_mcp.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.mark.parametrize('value', ['x\ny', 'x\ry', 'x|y', 'x"y'])
def test_reject_command_delimiters(value):
    with pytest.raises(ValueError):
        bridge.literal(value)


def test_pascal_quote_escaping():
    assert bridge.literal("a'b") == "a''b"


def test_write_requires_explicit_confirmation():
    with pytest.raises(ValueError, match='confirmation'):
        bridge.altium_test_roundtrip()


def test_source_change_rejected_before_native(monkeypatch):
    monkeypatch.setattr(bridge, 'sha', lambda _: 'changed')
    with pytest.raises(ValueError, match='Source changed'):
        bridge.altium_test_roundtrip(True)
    assert not bridge.LOCK.locked()


def test_uncertain_request_rejected(monkeypatch):
    monkeypatch.setattr(bridge, 'UNCERTAIN', True)
    with pytest.raises(ValueError, match='uncertain'):
        bridge.altium_test_roundtrip(True)
    assert not bridge.LOCK.locked()


@pytest.mark.parametrize('field,value', [('request', 'stale'), ('action', 'add'),
    ('status', 'incomplete'), ('board', 'other.PcbDoc'), ('after', '-1')])
def test_invalid_native_response_rejected(tmp_path, field, value):
    board = tmp_path / 'test.PcbDoc'
    row = dict(request='test', action='inspect', status='completed', board=str(board), before='0', after='0')
    row[field] = value
    report = tmp_path / 'response.ini'
    report.write_text('[bridge]\n' + '\n'.join(f'{k}={v}' for k, v in row.items()))
    with pytest.raises(ValueError):
        bridge.parse_report(report, 'test', 'inspect', board)


def test_matching_native_response(tmp_path):
    board = tmp_path / 'test.PcbDoc'
    report = tmp_path / 'response.ini'
    report.write_text(f'[bridge]\nrequest=test\naction=inspect\nstatus=completed\nboard={board}\nbefore=0\nafter=0\n')
    assert bridge.parse_report(report, 'test', 'inspect', board)['after'] == '0'


@pytest.mark.parametrize('offset,limit', [(-1, 10), (0, 0), (0, 501), (True, 10)])
def test_wifi_snapshot_rejects_bad_pagination(monkeypatch, offset, limit):
    monkeypatch.syspath_prepend(str(Path(bridge.__file__).parent))
    with pytest.raises(ValueError, match='pagination'):
        bridge.altium_wifi_inventory_snapshot(offset=offset, limit=limit)


def test_wifi_snapshot_rejects_tampered_result(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(bridge.__file__).parent))
    path = tmp_path / 'result.json'
    path.write_text('{}')
    monkeypatch.setattr(bridge, 'WIFI_RESULT', path)
    with pytest.raises(ValueError, match='Pinned native result changed'):
        bridge.altium_wifi_inventory_snapshot()
