import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from configure_altium_service import generate


def test_generates_local_config_with_native_launch_disabled(tmp_path):
    source = tmp_path / 'boards'
    source.mkdir()
    target = tmp_path / 'altium-service.mcp.json'
    result = generate([str(source)], '', tmp_path / 'evidence', tmp_path / 'jobs',
                      target, python_exe=sys.executable)
    server = json.loads(target.read_text(encoding='utf-8'))['mcpServers']['altium-local-developer-beta']
    assert result['native_launch_enabled'] is False
    assert Path(server['command']) == Path(sys.executable).resolve()
    assert Path(server['args'][0]).name == 'altium_service_mcp.py'
    assert server['env']['ALTIUM_BETA_PROJECT_ROOTS'] == str(source)
    assert server['env']['ALTIUM_SERVICE_NATIVE_LAUNCH'] == '0'
    assert server['env']['ALTIUM_BETA_EXE'] == ''


def test_existing_configuration_is_not_overwritten(tmp_path):
    source = tmp_path / 'boards'
    source.mkdir()
    target = tmp_path / 'altium-service.mcp.json'
    target.write_text('existing', encoding='utf-8')
    with pytest.raises(FileExistsError):
        generate([str(source)], '', tmp_path / 'evidence', tmp_path / 'jobs', target)
    assert target.read_text(encoding='utf-8') == 'existing'


@pytest.mark.parametrize('service_root', ['boards/jobs', 'evidence/jobs'])
def test_rejects_shared_source_or_evidence_roots(tmp_path, service_root):
    source = tmp_path / 'boards'
    source.mkdir()
    with pytest.raises(ValueError):
        generate([str(source)], '', tmp_path / 'evidence', tmp_path / service_root,
                 tmp_path / 'altium-service.mcp.json')


@pytest.mark.skipif(os.name != 'nt', reason='Altium worker launcher is Windows PowerShell')
def test_generated_config_starts_worker_once(tmp_path):
    source = tmp_path / 'boards'
    source.mkdir()
    config = tmp_path / 'altium-service.mcp.json'
    generate([str(source)], '', tmp_path / 'evidence', tmp_path / 'jobs', config)
    script = ROOT / 'Start-Altium-Service.ps1'
    result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                             '-File', str(script), '-Once',
                             '-Config', str(config)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'jobs' / 'jobs.sqlite3').is_file()


@pytest.mark.skipif(os.name != 'nt', reason='Altium setup is Windows PowerShell')
def test_setup_stops_before_install_when_config_exists(tmp_path):
    source = tmp_path / 'boards'
    source.mkdir()
    config = tmp_path / 'altium-service.mcp.json'
    config.write_text('existing', encoding='utf-8')
    script = ROOT / 'Setup-Altium-Service.ps1'
    result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                             '-File', str(script), '-ProjectRoot', str(source),
                             '-Config', str(config)], capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'Configuration already exists' in result.stderr
    assert config.read_text(encoding='utf-8') == 'existing'
