import configparser
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from altium_redesign_revision import create_revision, working_project
from altium_project_snapshot import _sha


def fixture_project(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    names = ['Connector_WiFi.SchDoc', 'WiFi.PcbDoc', 'WiFi.PCBDwf',
             'WiFi_miniPCIe.BomDoc', 'WiFi_panel.PcbDoc', 'WiFi_panel.PCBDwf']
    raw = '[Design]\nVersion=1.0\n'
    for i, name in enumerate(names, 1):
        raw += f'[Document{i}]\nDocumentPath={name}\n'
        (source / name).write_bytes(name.encode())
    project = source / 'WiFi_miniPCIe.PrjPcb'
    project.write_text(raw)
    return project


def test_revision_preserves_baseline_and_excludes_stale_production(tmp_path):
    source = fixture_project(tmp_path)
    before = {p.name: p.read_bytes() for p in source.parent.iterdir()}
    result = create_revision(source, tmp_path / 'output', _sha(b'WiFi.PcbDoc'))
    parser = configparser.ConfigParser()
    parser.read(result['project'])
    assert [parser[s]['documentpath'] for s in parser.sections() if s.startswith('Document')] == ['Connector_WiFi.SchDoc', 'WiFi.PcbDoc']
    assert not any(result['acceptance'].values())
    assert not result['mini_pcie_mechanical_compatibility_claimed']
    assert {p.name: p.read_bytes() for p in source.parent.iterdir()} == before
    folder = Path(result['project']).parent
    for name, raw in before.items():
        assert (folder / name).read_bytes() == raw


def test_hash_mismatch_fails_before_creating_revision(tmp_path):
    source = fixture_project(tmp_path)
    with pytest.raises(ValueError, match='hash mismatch'):
        create_revision(source, tmp_path / 'output', 'wrong')
    assert not (tmp_path / 'output').exists()


def test_missing_active_document_rejected():
    with pytest.raises(ValueError, match='one schematic'):
        working_project(b'[Design]\nVersion=1.0\n')


def test_native_validator_rejects_external_directory(tmp_path):
    from altium_redesign_compile import validate_revision
    with pytest.raises(ValueError, match='direct child'):
        validate_revision(tmp_path)


def test_native_validator_checks_dependencies(tmp_path, monkeypatch):
    import altium_redesign_compile as native
    source = fixture_project(tmp_path)
    monkeypatch.setattr(native, 'ROOT', tmp_path)
    result = create_revision(source, tmp_path / 'docs/validation/altium-redesign', _sha(b'WiFi.PcbDoc'))
    folder = Path(result['project']).parent
    assert native.validate_revision(folder)[1]['route'] == 'B'
    (folder / 'Connector_WiFi.SchDoc').write_bytes(b'changed')
    with pytest.raises(ValueError, match='Dependency hash'):
        native.validate_revision(folder)


def test_pinned_redesign_reports_failure_honestly():
    from altium_redesign_status import snapshot
    result = snapshot(True)
    assert result['native_compile'] is True
    assert result['counts']['violations'] == 159
    assert result['native_drc_pass'] is False
    assert result['manufacturing_release'] is False
    assert result['placement_modified'] is False
    assert result['rule_details']


def test_pinned_redesign_rejects_tampering(monkeypatch):
    import altium_redesign_status as status
    monkeypatch.setattr(status, 'PINS', {'native-drc.json': 'wrong'})
    with pytest.raises(ValueError, match='evidence changed'):
        status.snapshot()
