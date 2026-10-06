import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('drc_controls', SCRIPTS / 'accept_altium_drc_controls.py')
controls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controls)


def test_invalid_baseline_cannot_pass(monkeypatch):
    monkeypatch.setattr(controls, 'validate_drc', lambda _: {'report_valid': False})
    with pytest.raises(ValueError, match='Invalid baseline'):
        controls.accept()


def test_baseline_with_violations_cannot_pass(monkeypatch):
    monkeypatch.setattr(controls, 'validate_drc',
                        lambda _: {'report_valid': True, 'counts': {'violations': 1}})
    with pytest.raises(ValueError, match='Baseline has violations'):
        controls.accept()
