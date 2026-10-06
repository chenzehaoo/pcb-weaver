import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from altium_service import AltiumService


@pytest.fixture
def service(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    project = source / 'Sample.PrjPcb'
    project.write_bytes(b'project')
    board = source / 'Board.PcbDoc'
    board.write_bytes(b'board')
    calls = []

    class FakeBeta:
        lock = tmp_path / 'native.lock'

        def ready(self):
            return {'automatic_routing': False, 'native_busy_or_uncertain': self.lock.exists()}

        def inspect(self, path, board_path=None):
            assert str(project) == path
            assert board_path in (None, board.name)
            return {'project': str(project), 'board': board.name,
                    'project_sha256': hashlib.sha256(project.read_bytes()).hexdigest(),
                    'files': [{'relative_path': board.name,
                               'sha256': hashlib.sha256(board.read_bytes()).hexdigest()}]}

        def check(self, path, board_path=None, run_drc=False):
            calls.append((path, board_path, run_drc))
            return {'status': 'completed', 'native_compile': True, 'full_drc_pass': False}

    instance = AltiumService(tmp_path / 'jobs', FakeBeta, native_enabled=False)
    return instance, project, board, calls


def test_static_inspection_survives_new_service_instance(service):
    queue, project, board, _ = service
    first = queue.submit('inspect', str(project), request_id='same-request')
    same = queue.submit('inspect', str(project), request_id='same-request')
    assert same['id'] == first['id']
    assert queue.status()['automatic_routing'] is False
    assert queue.run_once()['state'] == 'completed'
    reopened = AltiumService(queue.root, queue.beta_factory, native_enabled=False)
    result = reopened.get_job(first['id'])
    assert result['result']['inspection']['board'] == board.name
    assert result['result']['native_executed'] is False
    assert project.read_bytes() == b'project' and board.read_bytes() == b'board'
    listed = reopened.list_jobs()
    assert listed['total'] == 1 and 'result' not in listed['items'][0]


def test_changed_source_blocks_queued_job(service):
    queue, project, board, _ = service
    job = queue.submit('inspect', str(project))
    board.write_bytes(b'changed')
    result = queue.run_once()
    assert result['id'] == job['id'] and result['state'] == 'blocked'
    assert 'changed after submission' in result['error']


def test_check_defaults_to_blocked_and_does_not_launch(service):
    queue, project, _, calls = service
    job = queue.submit('check', str(project), run_drc=True)
    result = queue.run_once()
    assert result['id'] == job['id'] and result['state'] == 'blocked'
    assert 'disabled' in result['error'] and calls == []


def test_expired_lease_blocks_without_retry(service):
    queue, project, _, _ = service
    job = queue.submit('inspect', str(project))
    with queue._db() as db:
        db.execute("UPDATE jobs SET state='running', worker_id='old', lease_until=? WHERE id=?",
                   (time.time() - 1, job['id']))
    assert queue.recover_expired() == 1
    assert queue.get_job(job['id'])['state'] == 'blocked'
    assert queue.run_once() is None


def test_cancel_and_invalid_reuse(service):
    queue, project, board, _ = service
    job = queue.submit('inspect', str(project), request_id='idempotent')
    with pytest.raises(ValueError, match='different input'):
        queue.submit('check', str(project), request_id='idempotent')
    assert queue.cancel(job['id'])['state'] == 'cancelled'
    with pytest.raises(ValueError, match='Only a queued'):
        queue.cancel(job['id'])
    assert board.read_bytes() == b'board'


def test_database_cannot_be_inside_source_root(service):
    queue, project, _, _ = service
    queue.beta_factory.roots = [project.parent]
    path = project.parent / 'service-db'
    with pytest.raises(ValueError, match='independent'):
        AltiumService(path, queue.beta_factory, native_enabled=False)
    assert not path.exists()


def test_native_guard_does_not_assume_non_windows_capability(service):
    queue, project, _, calls = service
    queue.native_enabled = True
    job = queue.submit('check', str(project))
    result = queue.run_once()
    assert result['id'] == job['id']
    if sys.platform != 'win32':
        assert result['state'] == 'blocked' and calls == []
    else:
        assert result['state'] in ('completed', 'blocked')


@pytest.mark.asyncio
async def test_real_stdio_mcp_worker_and_reconnect(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / 'source'
    source.mkdir()
    project = source / 'Sample.PrjPcb'
    project.write_text('[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Top.SchDoc\n'
                       '[Document2]\nDocumentPath=Board.PcbDoc\n', encoding='ascii')
    (source / 'Top.SchDoc').write_bytes(b'schematic')
    (source / 'Board.PcbDoc').write_bytes(b'board')
    exe = tmp_path / 'X2.EXE'
    exe.write_bytes(b'fixture only')
    env = dict(os.environ, ALTIUM_BETA_PROJECT_ROOTS=str(source), ALTIUM_BETA_EXE=str(exe),
               ALTIUM_BETA_RUN_ROOT=str(tmp_path / 'native-evidence'),
               ALTIUM_SERVICE_ROOT=str(tmp_path / 'service'), ALTIUM_SERVICE_NATIVE_LAUNCH='0',
               PYTHONDONTWRITEBYTECODE='1')
    server = root / 'scripts/altium_service_mcp.py'
    worker = root / 'scripts/altium_service_worker.py'
    params = StdioServerParameters(command=sys.executable, args=['-B', str(server)], env=env)

    async def call(session, name, arguments):
        response = await session.call_tool(name, arguments)
        assert not response.isError, response
        if response.structuredContent is not None:
            return response.structuredContent
        import json
        return json.loads(response.content[0].text)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {'altium_service_status', 'altium_submit_inspection', 'altium_submit_native_check',
                    'altium_get_job', 'altium_get_native_report', 'altium_list_jobs',
                    'altium_cancel_queued_job'} <= names
            status = await call(session, 'altium_service_status', {})
            assert status['automatic_routing'] is False and status['native_launch_enabled'] is False
            first = await call(session, 'altium_submit_inspection',
                               {'project_path': str(project), 'request_id': 'stdio-repeat'})
            same = await call(session, 'altium_submit_inspection',
                              {'project_path': str(project), 'request_id': 'stdio-repeat'})
            assert first['id'] == same['id'] and first['state'] == 'queued'
            native = await call(session, 'altium_submit_native_check', {'project_path': str(project)})
            assert native['state'] == 'queued'

    for _ in range(2):
        completed = subprocess.run([sys.executable, '-B', str(worker), '--once'],
                                   env=env, capture_output=True, text=True, timeout=15)
        assert completed.returncode == 0, completed.stderr

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            inspected = await call(session, 'altium_get_job', {'job_id': first['id']})
            blocked = await call(session, 'altium_get_job', {'job_id': native['id']})
            listed = await call(session, 'altium_list_jobs', {})
            assert inspected['state'] == 'completed'
            assert inspected['result']['inspection']['board'] == 'Board.PcbDoc'
            assert blocked['state'] == 'blocked' and 'disabled' in blocked['error']
            assert listed['total'] == 2
    assert project.read_text(encoding='ascii').startswith('[Design]')
    assert (source / 'Board.PcbDoc').read_bytes() == b'board'
