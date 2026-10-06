import configparser
import hashlib
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import altium_beta as module


@pytest.fixture
def beta(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    output = tmp_path / 'evidence'
    exe = tmp_path / 'X2.EXE'
    exe.write_bytes(b'fixture')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    (tmp_path / 'docs/validation/altium-native').mkdir(parents=True)
    (tmp_path / 'scripts/altium').mkdir(parents=True)
    template = Path(__file__).resolve().parents[1] / 'scripts/altium/ProjectCompile.pas.template'
    (tmp_path / 'scripts/altium/ProjectCompile.pas.template').write_bytes(template.read_bytes())
    names = ['Top.SchDoc', 'Board.PcbDoc', r'Libraries\Parts.SchLib']
    project = source / 'Sample.PrjPcb'
    project.write_text('[Design]\nVersion=1.0\n' + ''.join(
        f'[Document{i}]\nDocumentPath={name}\n' for i, name in enumerate(names, 1)))
    for name in names:
        file = source.joinpath(*module._parts(name))
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(name.encode())
    return module.Beta([source], output, exe), project


def test_inspect_nested_and_snapshot_preserve_files(beta):
    service, project = beta
    inspection = service.inspect(str(project))
    folder, manifest = service._snapshot(inspection)
    assert len(manifest['files']) == 3
    assert (folder / 'Libraries/Parts.SchLib').read_bytes() == b'Libraries\\Parts.SchLib'
    assert service.ready()['native_busy_or_uncertain'] is False
    assert inspection['board'] == 'Board.PcbDoc'


def test_reject_unlisted_path(beta, tmp_path):
    service, project = beta
    other = tmp_path / 'other.PrjPcb'
    other.write_bytes(project.read_bytes())
    with pytest.raises(ValueError, match='outside allowed'):
        service.inspect(str(other))


@pytest.mark.parametrize('name', [r'..\escape.PcbDoc', 'C:escape.PcbDoc', '/escape.PcbDoc',
                                      r'Libraries\..\Board.PcbDoc', 'NUL.PcbDoc'])
def test_reject_unsafe_document_path(beta, name):
    service, project = beta
    project.write_text(project.read_text().replace('Board.PcbDoc', name))
    with pytest.raises(ValueError):
        service.inspect(str(project))


def test_multiple_boards_require_explicit_selection(beta):
    service, project = beta
    with project.open('a') as stream:
        stream.write('[Document4]\nDocumentPath=Panel.PcbDoc\n')
    (project.parent / 'Panel.PcbDoc').write_bytes(b'panel')
    with pytest.raises(ValueError, match='Multiple PCBs'):
        service.inspect(str(project))
    assert len(service.inspect(str(project), 'Board.PcbDoc')['available_boards']) == 2


def _emulate_success(folder):
    script = (folder / 'Check.pas').read_text()
    def literal(name):
        return re.search(rf"Report.Add\('{name}=([^']+)'\)", script).group(1)
    parser = configparser.ConfigParser()
    parser['job'] = {'request': literal('request'), 'project': literal('project'),
                     'board': literal('board'), 'pcb_project': literal('project'),
                     'schematic': str(folder / 'Top.SchDoc'),
                     'compile_return': 'True', 'flattened_available': 'True'}
    parser['completion'] = {'status': 'completed'}
    with (folder / 'response.ini').open('w') as stream:
        parser.write(stream)


def test_check_success_uses_copy_and_releases_lock(beta, monkeypatch):
    service, project = beta
    before = project.read_bytes()
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: _emulate_success(next(service.run_root.iterdir())))
    result = service.check(str(project))
    assert result['native_compile'] is True and result['files_unchanged'] is True
    assert result['full_drc_pass'] is False
    assert project.read_bytes() == before
    assert not service.lock.exists()
    assert service.report(result['run_id'])['findings_total'] == 0


def test_stale_native_lock_blocks_dispatch(beta, monkeypatch):
    service, project = beta
    service.lock.write_text('uncertain')
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('native launched'))
    with pytest.raises(FileExistsError):
        service.check(str(project))
    assert service.lock.read_text() == 'uncertain'


def test_failed_native_identity_retains_lock(beta, monkeypatch):
    service, project = beta
    def wrong(*args, **kwargs):
        folder = next(service.run_root.iterdir())
        (folder / 'response.ini').write_text('[job]\nrequest=wrong\n[completion]\nstatus=completed\n')
    monkeypatch.setattr(module.subprocess, 'Popen', wrong)
    with pytest.raises((ValueError, KeyError)):
        service.check(str(project))
    assert service.lock.exists()
    folder = next(service.run_root.iterdir())
    assert 'failed' in (folder / 'result.json').read_text()


def test_pin_real_two_project_acceptance():
    root = Path(__file__).resolve().parents[1]
    acceptance = root / 'docs/validation/altium-beta-mcp-acceptance.json'
    if not acceptance.exists():
        pytest.skip('Machine-specific native acceptance not present')
    import json
    result = json.loads(acceptance.read_text())
    assert result['status'] == 'passed' and result['full_drc_pass'] is False
    for key in ('wifi_run_id', 'spirit_run_id'):
        run_id = result[key]
        folder = root / 'docs/validation/altium-beta' / run_id
        record = json.loads((folder / 'result.json').read_text())
        manifest = folder / 'manifest.json'
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() == record['manifest_sha256']
        assert record['native_compile'] and record['files_unchanged']


def test_native_response_supports_windows_chinese_encoding():
    assert module._decode_native('中文工程'.encode('mbcs')) == '中文工程'


def test_chinese_project_native_acceptance():
    root = Path(__file__).resolve().parents[1]
    path = root / 'docs/validation/altium-beta-chinese-acceptance.json'
    if not path.exists():
        pytest.skip('Machine-specific Chinese project acceptance not present')
    import json
    result = json.loads(path.read_text(encoding='utf-8'))
    native = json.loads((root / 'docs/validation/altium-beta' / result['run_id'] / 'result.json').read_text())
    assert result['status'] == 'passed' and native['native_compile']
    assert native['files_unchanged'] and native['full_drc_pass'] is False


def test_core_config_regeneration_preserves_beta(tmp_path, monkeypatch):
    import json
    import configure_client
    (tmp_path / 'toolchain.unified.json').write_text('{}')
    config = tmp_path / '.mcp.json'
    config.write_text(json.dumps({'mcpServers': {'altium-developer-beta': {'command': 'fixture'}}}))
    monkeypatch.setattr(sys, 'argv', ['configure_client.py', '--root', str(tmp_path)])
    configure_client.main()
    servers = json.loads(config.read_text())['mcpServers']
    assert servers['altium-developer-beta'] == {'command': 'fixture'}
    assert 'pcb-weaver' in servers
