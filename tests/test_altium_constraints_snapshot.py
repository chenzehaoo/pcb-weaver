import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('constraints_snapshot', SCRIPTS / 'altium_constraints_snapshot.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def native():
    return json.loads(module.RESULT.read_text())['native']


def reference(native):
    return {key: {field: row[field] for field in module.FIELDS}
            for key, row in native.items() if key.startswith('rule.')}


def test_real_captured_rule_fields(native):
    captured, missing = module.validate_rules(native, reference(native))
    assert len(captured) == 6
    assert len(missing) == 34


@pytest.mark.parametrize('field,value', [('min_width_1', '-1'), ('max_width_32', 'NaN'),
                                      ('favored_width_2', '999999999'), ('min_width_1', '1.0')])
def test_invalid_width_rejected(native, field, value):
    expected = reference(native)
    native['rule.20'][field] = value
    with pytest.raises(ValueError):
        module.validate_rules(native, expected)


@pytest.mark.parametrize('field', module.FIELDS)
def test_rule_identity_mismatch_rejected(native, field):
    expected = reference(native)
    native['rule.20'][field] = 'changed'
    with pytest.raises(ValueError, match='metadata'):
        module.validate_rules(native, expected)


def test_missing_layer_rejected(native):
    expected = reference(native)
    del native['rule.20']['min_width_32']
    with pytest.raises(ValueError):
        module.validate_rules(native, expected)


def test_missing_rule_rejected(native):
    expected = reference(native)
    del native['rule.20']
    with pytest.raises(ValueError, match='coverage'):
        module.validate_rules(native, expected)


def test_bad_snapshot_hash_fails_before_native_access(tmp_path, monkeypatch):
    path = tmp_path / 'result.json'
    path.write_text('{}')
    monkeypatch.setattr(module, 'RESULT', path)
    with pytest.raises(ValueError, match='hash'):
        module.snapshot()
