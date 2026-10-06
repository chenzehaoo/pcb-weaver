import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('altium_native_job',
    Path(__file__).resolve().parents[1] / 'scripts/altium_native_job.py')
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


def test_unknown_action_never_creates_job(tmp_path, monkeypatch):
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='Unknown'):
        job.run('../other')
    assert not list(tmp_path.iterdir())


def test_completed_action_releases_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(job, '_run', lambda mode: tmp_path / mode)
    assert job.run('Inventory') == tmp_path / 'Inventory'
    assert not (tmp_path / 'docs/validation/altium-native/pending.json').exists()


def test_negative_control_is_explicit_separate_action(tmp_path, monkeypatch):
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(job, '_run', lambda mode: tmp_path / mode)
    assert job.run('DRCNegative') == tmp_path / 'DRCNegative'


def test_negative_control_template_only_targets_new_bound_copy():
    template = (Path(__file__).resolve().parents[1] /
                'scripts/altium/DRCNegative.pas.template').read_text()
    assert "UpperCase(Board.FileName) <> UpperCase('@@BOARD@@')" in template
    assert 'negative_width_control_not_routing_output' in template
    assert 'Track.Width := MMsToCoord(0.001)' in template
    assert 'RunBatchDesignRuleCheck' in template
    assert 'DRCEnabled :=' not in template


def test_failure_preserves_marker_and_prevents_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    calls = []

    def fail(mode):
        calls.append(mode)
        raise TimeoutError('native outcome unknown')

    monkeypatch.setattr(job, '_run', fail)
    with pytest.raises(TimeoutError):
        job.run('Export')
    marker = tmp_path / 'docs/validation/altium-native/pending.json'
    original = marker.read_bytes()
    with pytest.raises(FileExistsError):
        job.run('Inventory')
    assert marker.read_bytes() == original
    assert calls == ['Export']
