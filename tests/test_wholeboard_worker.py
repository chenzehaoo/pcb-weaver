"""Offline harness ownership and independent AST acceptance tests; no native tools."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import sexpdata


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wholeboard_worker_harness', ROOT / 'scripts/wholeboard_benchmark.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class Queue:
    def __init__(self):
        self.jobs = {'unrelated': {'id':'unrelated', 'status':'queued'}}
        self.events = []
        self.thread = SimpleNamespace(join=lambda:self.events.append('join'))

    def start(self):
        self.events.append('start')

    def close(self):
        self.events.append('close')

    def get(self, identifier):
        return deepcopy(self.jobs[identifier])

    def cancel(self, identifier):
        self.events.append(('cancel', identifier))
        self.jobs[identifier]['cancel_requested'] = True
        return self.get(identifier)


def test_default_workspaces_are_fresh_and_exclusive(tmp_path):
    first = benchmark.benchmark_workspace(tmp_path)
    second = benchmark.benchmark_workspace(tmp_path)
    assert first != second
    for path in (first, second):
        assert path.is_dir() and path.parent == tmp_path / 'benchmark-data'
        assert UUID(path.name).version == 4
        with pytest.raises(ValueError, match='--external-worker'):
            benchmark.benchmark_workspace(tmp_path, path)


def test_existing_shared_workspace_requires_external_worker(tmp_path):
    shared = tmp_path / 'data'
    shared.mkdir()
    marker = shared / 'existing-evidence'
    marker.write_text('unchanged')
    alias = shared / '..' / 'data'
    with pytest.raises(ValueError, match='--external-worker'):
        benchmark.benchmark_workspace(tmp_path, alias)
    assert benchmark.benchmark_workspace(tmp_path, alias, external_worker=True) == shared
    assert marker.read_text() == 'unchanged'


def test_new_explicit_workspace_is_reserved_atomically(tmp_path, monkeypatch):
    target = tmp_path / 'exclusive'
    mkdir = Path.mkdir

    def raced(path, *args, **kwargs):
        if path == target:
            mkdir(path)
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', raced)
    with pytest.raises(ValueError, match='exclusive directory'):
        benchmark.benchmark_workspace(tmp_path, target)


def test_cleanup_cancels_only_owned_unfinished_ids_and_continues_on_error():
    queue = Queue()
    for status in ('queued', 'running', 'completed', 'blocked', 'failed', 'cancelled', 'interrupted'):
        queue.jobs[status] = {'id':status, 'status':status}
    cleanup = benchmark.cancel_owned_jobs(queue, ['missing', *list(queue.jobs)[1:], 'queued'])
    assert cleanup['cancel_requested'] == ['queued', 'running']
    assert [error['job_id'] for error in cleanup['errors']] == ['missing']
    assert queue.events == [('cancel', 'queued'), ('cancel', 'running')]
    assert not queue.jobs['unrelated'].get('cancel_requested')
    assert benchmark.cancel_owned_jobs(queue, ['queued', 'running'])['cancel_requested'] == []


@pytest.mark.parametrize('tag', ['segment', 'via', 'generator', 'generator_version'])
def test_nonrouting_ast_ignores_only_allowed_top_level_nodes(tmp_path, tag):
    before, after = tmp_path / 'before.kicad_pcb', tmp_path / 'after.kicad_pcb'
    before.write_text('(kicad_pcb (version 20241229) (footprint "F" (pad "1" smd rect)))')
    ast = sexpdata.loads(before.read_text())
    ast.append([sexpdata.Symbol(tag), 'changed'])
    after.write_text(sexpdata.dumps(ast))
    assert benchmark.nonrouting_ast(before) == benchmark.nonrouting_ast(after)


@pytest.mark.parametrize('change', [
    '(arc (start 1 2))', '(setup (pad_to_mask_clearance 0.1))',
    '(layers (0 "F.Cu" signal))', '(version 20260101)',
    '(footprint "F" (clearance 0.001))',
    '(footprint "F" (pad "1" smd rect (clearance 0.001)))',
    '(footprint "F" (generator "nested-must-not-be-excluded"))',
])
def test_nonrouting_ast_rejects_rules_geometry_and_nested_changes(tmp_path, change):
    before, after = tmp_path / 'before.kicad_pcb', tmp_path / 'after.kicad_pcb'
    before.write_text('(kicad_pcb (version 20241229))')
    after.write_text('(kicad_pcb (version 20241229) '+change+')')
    assert benchmark.nonrouting_ast(before) != benchmark.nonrouting_ast(after)


class StopBenchmark(BaseException):
    pass


@pytest.mark.parametrize('final_revision', ['input', 'final'])
def test_verify_pair_keeps_both_latest_authentic_bindings(final_revision):
    calls, latest = [], {}

    def verify(project, revision):
        calls.append((project, revision))
        latest[revision] = {'verification_id':len(calls)}
        return latest[revision]

    baseline, check = benchmark.verify_pair(SimpleNamespace(verify_revision=verify), 'project', 'input', final_revision)
    assert baseline is latest['input']
    assert check is latest[final_revision]
    assert calls == [('project', 'input')] + ([] if final_revision == 'input' else [('project', 'final')])
    assert (baseline is check) == (final_revision == 'input')


@pytest.fixture
def run_context(tmp_path, monkeypatch):
    root = tmp_path / 'implementation'
    package = root / 'src' / 'pcb_weaver'
    package.mkdir(parents=True)
    monkeypatch.setattr(benchmark.pcb_weaver, '__file__', str(package / '__init__.py'))
    config, options = root / 'config.json', root / 'options.json'
    config.write_text('{}')
    options.write_text('{"placement_mode":"preserve"}')
    folders = {revision:tmp_path / revision for revision in ('input', 'final')}
    board_name = 'board.kicad_pcb'
    ast = '(kicad_pcb (version 20241229) (footprint "F" (pad "1" smd rect)))'
    for revision, folder in folders.items():
        (folder / 'design').mkdir(parents=True)
        (folder / 'constraints.json').write_text('{}')
        (folder / 'design' / board_name).write_text(ast)
    fixture = folders['input'] / 'design'
    (fixture / 'constraints.json').write_text('{}')
    source = fixture / board_name
    target = folders['final'] / 'design' / board_name
    manifest = {'board':board_name, 'input_board_sha256':benchmark.digest(source)}
    monkeypatch.setattr(benchmark, 'fixture_verified', lambda path:manifest)
    queue = Queue()
    state = {'failure':None, 'submitted':[], 'verified':[], 'queue_workspaces':[]}

    def verified(project, revision):
        folder = folders[revision]
        return {'board':board_name, 'files':{board_name:benchmark.digest(folder / 'design' / board_name)}}, folder

    def verify(project, revision):
        state['verified'].append(revision)
        return {'status':'passed', 'drc':{'errors':0, 'unconnected':0}, 'erc':{'errors':0}}

    engine = SimpleNamespace(toolchain=SimpleNamespace(route_timeout=1), _verified=verified,
                             verify_revision=verify, _electrical_signature=lambda board:board['footprints'])
    monkeypatch.setattr(benchmark, 'EngineeringService', lambda *args:engine)

    def make_queue(workspace, **kwargs):
        state['queue_workspaces'].append(workspace)
        return queue

    monkeypatch.setattr(benchmark, 'JobQueue', make_queue)
    monkeypatch.setattr(benchmark, 'read_board', lambda path:{
        'tracks':int(path == target), 'vias':0, 'footprints':[], 'outline':{}})
    monkeypatch.setattr(benchmark, '_report', lambda *args:{'violations':[]})
    monkeypatch.setattr(benchmark, '_erc_report', lambda *args:{
        '$schema':'https://schemas.kicad.org/erc.v1.json', 'sheets':[]})
    monkeypatch.setattr(benchmark.catalog, 'generate_report', lambda *args:{'status':'generated'})

    @asynccontextmanager
    async def stdio(parameters):
        state['mcp_environment'] = parameters.env
        yield None, None

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name='submit_pcb_completion', inputSchema={})])

        async def call_tool(self, name, arguments):
            if name == 'import_pcb_project':
                payload = {'revision':{'id':'input'}}
            elif name == 'submit_pcb_completion':
                identifier = 'owned-'+str(len(state['submitted'])+1)
                state['submitted'].append(identifier)
                payload = queue.jobs[identifier] = {'id':identifier, 'status':'running', 'stage':'complete',
                    'result':{'steps':{'complete':{'revision':'final', 'best':{'moved_components':0},
                                                 'manufacturing_authorized':False}}}}
            elif name == 'get_engineering_job':
                if state['failure'] == 'poll':
                    raise RuntimeError('poll failed')
                if state['failure'] == 'interrupt':
                    raise StopBenchmark('interrupted after submission')
                queue.jobs[arguments['job_id']]['status'] = state.get('terminal_status', 'completed')
                if 'terminal_status' in state:
                    flow = queue.jobs[arguments['job_id']]['result']['steps']['complete']
                    flow['revision'] = 'input'
                    flow['reason'] = state.get('terminal_reason')
                payload = queue.get(arguments['job_id'])
            else:
                pytest.fail('Unexpected MCP operation: '+name)
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=json.dumps(payload))])

    monkeypatch.setattr(benchmark, 'stdio_client', stdio)
    monkeypatch.setattr(benchmark, 'ClientSession', Session)
    args = SimpleNamespace(root=root, fixture=fixture, options=options, config=config,
                           workspace=tmp_path / 'execution', external_worker=False,
                           output=tmp_path / 'result.json', project='benchmark', repeats=2)
    return SimpleNamespace(args=args, queue=queue, state=state, target=target)


@pytest.mark.parametrize('external', [False, True])
def test_run_worker_ownership_and_recorded_workspace(run_context, external):
    context = run_context
    context.args.external_worker = external
    if external:
        context.args.workspace.mkdir()
    assert asyncio.run(benchmark.run(context.args))
    result = benchmark.read_json(context.args.output)
    assert result['workspace'] == str(context.args.workspace.resolve())
    assert result['worker_mode'] == ('external' if external else 'harness_owned')
    assert result['passed_runs'] == 2
    assert all(run['nonrouting_ast_preserved'] for run in result['runs'])
    assert context.queue.events == ([] if external else ['start', 'close', 'join'])
    assert context.state['queue_workspaces'] == [context.args.workspace.resolve()]
    assert context.state['mcp_environment']['PCB_WEAVER_WORKSPACE'] == result['workspace']
    assert context.state['mcp_environment']['PCB_WEAVER_CONFIG'] == str(context.args.config)
    assert context.state['mcp_environment']['PYTHONOPTIMIZE'] == '0'
    assert not context.queue.jobs['unrelated'].get('cancel_requested')


def test_run_refuses_existing_workspace_before_constructing_queue(run_context):
    context = run_context
    context.args.workspace.mkdir()
    with pytest.raises(ValueError, match='--external-worker'):
        asyncio.run(benchmark.run(context.args))
    assert context.state['queue_workspaces'] == [] and context.queue.events == []


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('failure', ['poll', 'interrupt'])
def test_failed_runs_cancel_only_submitted_ids(run_context, external, failure):
    context = run_context
    context.args.external_worker = external
    context.state['failure'] = failure
    if failure == 'interrupt':
        with pytest.raises(StopBenchmark):
            asyncio.run(benchmark.run(context.args))
    else:
        assert not asyncio.run(benchmark.run(context.args))
    result = benchmark.read_json(context.args.output)
    assert result['status'] == 'failed'
    assert [event[1] for event in context.queue.events if isinstance(event, tuple)] == context.state['submitted']
    lifecycle = [event for event in context.queue.events if isinstance(event, str)]
    assert lifecycle == ([] if external else ['start', 'close', 'join'])
    assert not context.queue.jobs['unrelated'].get('cancel_requested')


def test_independent_ast_gate_rejects_hidden_clearance_change_before_native_checks(run_context):
    context = run_context
    context.args.external_worker = True
    context.target.write_text('(kicad_pcb (version 20241229) '
                              '(footprint "F" (pad "1" smd rect (clearance 0.001))))')
    assert not asyncio.run(benchmark.run(context.args))
    result = benchmark.read_json(context.args.output)
    assert all('Nonrouting board AST changed' in run['error'] for run in result['runs'])
    assert context.state['verified'] == []
    assert context.queue.events == []


@pytest.mark.parametrize('status,reason', [('cancelled', None), ('cancelled', 'Cancelled before rerouting'),
                                         ('failed', 'Completion failed')])
def test_terminal_job_same_revision_is_verified_once_and_reason_preserved(run_context, monkeypatch, status, reason):
    context = run_context
    context.args.repeats = 1
    context.state.update(terminal_status=status, terminal_reason=reason)
    bindings = []

    def report(engine, project, revision, check):
        assert context.state['verified'] == ['input'], 'Repair verification changed'
        bindings.append(check)
        return {'violations':[]}

    monkeypatch.setattr(benchmark, '_report', report)
    assert not asyncio.run(benchmark.run(context.args))
    result = benchmark.read_json(context.args.output)
    entry = result['runs'][0]
    assert entry['job']['status'] == status
    assert entry['job_failure_reason'] == (reason or status)
    assert entry['error'] == 'AssertionError: '+(reason or status)
    assert entry['status'] == 'failed' and result['passed_runs'] == 0
    assert context.state['verified'] == ['input']
    assert len(bindings) == 2 and bindings[0] is bindings[1]
    assert entry['baseline_verification'] == entry['independent_verification']
