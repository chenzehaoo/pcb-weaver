import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('coverage_snapshot', SCRIPTS / 'altium_drc_coverage_snapshot.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_missing_evidence_cannot_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'FOLDER', tmp_path)
    with pytest.raises(FileNotFoundError):
        module.snapshot()


def test_changed_evidence_cannot_pass(tmp_path, monkeypatch):
    for name in module.PINS:
        (tmp_path / name).write_text('{}')
    monkeypatch.setattr(module, 'FOLDER', tmp_path)
    with pytest.raises(ValueError, match='evidence changed'):
        module.snapshot()
