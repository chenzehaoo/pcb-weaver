import configparser
import hashlib
import importlib.util
from pathlib import Path
import re
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('project_compile_test', SCRIPTS / 'altium_project_compile.py')
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    (tmp_path / 'docs/validation/altium-native').mkdir(parents=True)
    scripts = tmp_path / 'scripts/altium'
    scripts.mkdir(parents=True)
    (scripts / 'ProjectCompile.pas.template').write_text((SCRIPTS / 'altium/ProjectCompile.pas.template').read_text())
    folder = tmp_path / 'project'
    folder.mkdir()
    files = []
    for name in ('WiFi_miniPCIe.PrjPcb', 'WiFi.PcbDoc', 'Connector_WiFi.SchDoc'):
        source = tmp_path / name
        source.write_bytes(b'original')
        target = folder / name
        target.write_bytes(source.read_bytes())
        files.append({'role': 'project' if name.endswith('PrjPcb') else 'document',
                      'source': str(source), 'copy': str(target), 'relative_path': name,
                      'source_sha256': hashlib.sha256(b'original').hexdigest()})
    (folder / 'manifest.json').write_text('{}')
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(job, 'snapshot_project', lambda: {'status': 'completed', 'files': files})
    return folder


def emulate(folder, compiled=True):
    script = (folder / 'Compile.pas').read_text()
    request = re.search(r"request=([a-f0-9]+)", script).group(1)
    ini = configparser.ConfigParser()
    ini['job'] = {'request': request, 'compile_return': str(compiled),
                  'pcb_project': str(folder / 'WiFi_miniPCIe.PrjPcb')}
    ini['completion'] = {'status': 'completed' if compiled else 'failed'}
    with (folder / 'compile-response.ini').open('w') as handle:
        ini.write(handle)


def test_compile_only_never_claims_si(environment, monkeypatch):
    monkeypatch.setattr(job.subprocess, 'Popen', lambda *a, **k: emulate(environment))
    result = job.run()
    assert result['status'] == 'completed'
    assert result['signal_integrity_verified'] is False
    assert 'PCB:DesignRuleCheck' not in (environment / 'Compile.pas').read_text()
    assert not (job.ROOT / 'docs/validation/altium-native/pending.json').exists()


def test_failed_compile_keeps_pending(environment, monkeypatch):
    monkeypatch.setattr(job.subprocess, 'Popen', lambda *a, **k: emulate(environment, False))
    with pytest.raises(ValueError, match='compilation'):
        job.run()
    assert (job.ROOT / 'docs/validation/altium-native/pending.json').exists()


def test_source_change_rejected(environment, monkeypatch):
    def launch(*args, **kwargs):
        emulate(environment)
        (job.ROOT / 'WiFi.PcbDoc').write_bytes(b'changed')
    monkeypatch.setattr(job.subprocess, 'Popen', launch)
    with pytest.raises(ValueError, match='document changed'):
        job.run()


def test_existing_pending_prevents_dispatch(environment, monkeypatch):
    marker = job.ROOT / 'docs/validation/altium-native/pending.json'
    marker.write_text('pending')
    monkeypatch.setattr(job.subprocess, 'Popen', lambda *a, **k: pytest.fail('Must not dispatch'))
    with pytest.raises(FileExistsError):
        job.run()
