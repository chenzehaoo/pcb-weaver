import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from altium_silk_repair import verify_objects, verify_drc
from altium_silk_status import FOLDER, snapshot
from altium_redesign_status import FOLDER as BASELINE


@pytest.fixture
def record():
    return json.loads((FOLDER / 'silk-result.json').read_text())


def test_accepted_native_evidence():
    result = snapshot(True)
    assert result['after_violations'] == 154
    assert result['local_repair_accepted']
    assert not result['native_drc_pass']
    assert not result['manufacturing_release']
    assert sum(len(r['items']) for r in result['findings']) == 154


@pytest.mark.parametrize('kind,field', [('track', 'width'), ('pad', 'x'), ('component', 'x'), ('component', 'text_height')])
def test_rejects_unintended_object_change(record, kind, field):
    native = record['native']
    native[f'after.{kind}.0'][field] = '999'
    with pytest.raises(ValueError):
        verify_objects(native)


def test_rejects_hidden_label(record):
    for phase in ('before', 'after'):
        for key, row in record['native'].items():
            if key.startswith(phase + '.component.') and row['name'] == 'R11':
                row['text_visible'] = 'False'
    with pytest.raises(ValueError, match='visibly'):
        verify_objects(record['native'])


def test_rejects_missing_track(record):
    del record['native']['after.track.0']
    with pytest.raises(ValueError, match='copper'):
        verify_objects(record['native'])


def test_rejects_new_drc_violation(record):
    before = json.loads((BASELINE / 'native-drc.json').read_text())['drc_report']
    after = record['drc_report']
    after['rule_rows'][0]['count'] += 1
    with pytest.raises(ValueError):
        verify_drc(before, after)


def test_rejects_first_failed_candidate():
    folder = FOLDER.parent / '39a4227a840048fdb41828cfcb0bd00d'
    candidate = json.loads((folder / 'silk-result.json').read_text())
    before = json.loads((BASELINE / 'native-drc.json').read_text())['drc_report']
    with pytest.raises(ValueError):
        verify_drc(before, candidate['drc_report'])


def test_rejects_changed_pin(monkeypatch):
    import altium_silk_status as module
    monkeypatch.setattr(module, 'PIN', 'changed')
    with pytest.raises(ValueError, match='Pinned'):
        module.snapshot()
