"""Frozen fixed-placement inputs and repeatable, real MCP completion acceptance."""
import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
import time
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import pcb_weaver
from pcb_weaver import catalog
from pcb_weaver.board import read_board, _tag
from pcb_weaver.jobs import JobQueue
from pcb_weaver.models import CompletionOptions
from pcb_weaver.repair import _report, _erc_report, _violations, _erc_violations
from pcb_weaver.repair_geometry import _read, _serialize
from pcb_weaver.runtime import load_runtime
from pcb_weaver.service import EngineeringService, design_files
from pcb_weaver.storage import digest, read_json, write_json, now

if not __debug__:
    raise RuntimeError('Acceptance must not run with Python optimization enabled')


def files(root):
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob('*')) if p.is_file()}


def prepare(source, output):
    """Remove only routing objects from a sealed revision; never rewrite its rules."""
    source = source.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise ValueError('Use a new output directory')
    revision = read_json(source / 'revision.json')
    original = source / 'design' / revision['board']
    original_files = files(source / 'design')
    if original_files != revision['files']:
        raise ValueError('Source revision file hashes do not match')
    if digest(source / 'constraints.json') != revision['constraints_hash']:
        raise ValueError('Source constraints hash does not match')
    board = read_board(original)
    if board['unsupported']:
        raise ValueError('Do not silently remove unsupported zones/groups/geometry: '+str(board['unsupported']))
    ast = _read(original)[0]
    stripped = [deepcopy(n) for n in ast if _tag(n) not in {'segment', 'via', 'arc'}]
    shutil.copytree(source / 'design', output)
    target = output / original.name
    target.write_bytes(_serialize(stripped))
    shutil.copy2(source / 'constraints.json', output / 'constraints.json')
    after = read_board(target)
    assert after['tracks'] == after['vias'] == 0
    assert after['footprints'] == board['footprints'] and after['nets'] == board['nets']
    assert _read(target)[0] == stripped
    assert files(source / 'design') == original_files
    assert digest(source / 'constraints.json') == digest(output / 'constraints.json')
    for name, sha in original_files.items():
        if name != original.name:
            assert digest(output / name) == sha
    manifest = {'schema_version': 1, 'created': now(), 'source_revision': revision['id'],
        'source_project': revision['project'], 'source_board_sha256': digest(original),
        'board': original.name, 'input_board_sha256': digest(target),
        'files': files(output), 'removed': dict(Counter(_tag(n) for n in ast if _tag(n) in {'segment','via','arc'})),
        'components': len(after['footprints']), 'pads': sum(len(f['pads']) for f in after['footprints']),
        'nets': len(after['nets']), 'layers': len(after['copper_layers']),
        'zone_count': sum(_tag(n) == 'zone' for n in ast),
        'placement_preserved': True, 'rules_preserved': True,
        'scope': 'All tracks/vias removed; accepted placement retained. Not automatic placement or hardware qualification.',
        'manufacturing_authorized': False}
    write_json(output / 'BENCHMARK.json', manifest)
    return manifest


def fixture_verified(fixture):
    manifest = read_json(fixture / 'BENCHMARK.json')
    actual = files(fixture)
    actual.pop('BENCHMARK.json')
    if manifest['files'] != actual:
        raise ValueError('Frozen benchmark input changed')
    return manifest


def benchmark_workspace(root, workspace=None, *, external_worker=False):
    target = (workspace if workspace is not None else root / 'benchmark-data' / uuid4().hex).resolve()
    try:
        target.mkdir(parents=True, exist_ok=external_worker)
    except FileExistsError as error:
        raise ValueError('Existing workspace requires --external-worker; local workers need a new exclusive directory') from error
    return target


def nonrouting_ast(path):
    return [node for node in _read(path)[0]
            if _tag(node) not in {'segment', 'via', 'generator', 'generator_version'}]


def cancel_owned_jobs(queue, identifiers):
    cleanup = {'cancel_requested': [], 'errors': []}
    for identifier in sorted(set(identifiers)):
        try:
            job = queue.get(identifier)
            if job['status'] in {'queued', 'running'} and not job.get('cancel_requested'):
                queue.cancel(identifier)
                cleanup['cancel_requested'].append(identifier)
        except Exception as error:
            cleanup['errors'].append({'job_id':identifier, 'error':type(error).__name__+': '+str(error)})
    return cleanup


def verify_pair(engine, project, input_revision, final_revision):
    """Do not invalidate the baseline's latest-verification binding on the same revision."""
    baseline = engine.verify_revision(project, input_revision)
    check = baseline if final_revision == input_revision else engine.verify_revision(project, final_revision)
    return baseline, check


async def run(args):
    root = args.root.resolve(strict=True)
    fixture = args.fixture.resolve(strict=True)
    manifest = fixture_verified(fixture)
    options = CompletionOptions.model_validate(read_json(args.options))
    if options.placement_mode != 'preserve':
        raise ValueError('This benchmark requires placement_mode=preserve')
    if Path(pcb_weaver.__file__).resolve().parent != root / 'src' / 'pcb_weaver':
        raise ValueError('Python package is not loaded from the benchmark implementation root')
    if args.output.exists():
        raise ValueError('Never overwrite a benchmark result; choose a new output')
    config_path = (args.config or root / 'toolchain.unified.json').resolve(strict=True)
    external_worker = args.external_worker
    workspace = benchmark_workspace(root, args.workspace, external_worker=external_worker)
    workspace, config = load_runtime(workspace, config_path)
    engine = EngineeringService(workspace, config)
    queue = JobQueue(workspace, engine=engine)
    parameters = StdioServerParameters(command=sys.executable, args=['-m','pcb_weaver.server'],
        env={**os.environ, 'PCB_WEAVER_WORKSPACE':str(workspace),
             'PCB_WEAVER_CONFIG':str(config_path), 'PYTHONPATH':str(root / 'src'), 'PYTHONOPTIMIZE':'0'})
    implementation = files(root / 'src' / 'pcb_weaver')
    implementation = {k:v for k,v in implementation.items() if '__pycache__' not in k and k.endswith('.py')}
    result = {'status':'running', 'created':now(), 'fixture':manifest, 'runs':[],
        'options':options.model_dump(), 'expected_runs':args.repeats,
        'workspace':str(workspace), 'worker_mode':'external' if external_worker else 'harness_owned',
        'transport':'Official MCP SDK stdio; submitter disconnects; independent worker persists jobs',
        'implementation_sha256':implementation, 'config':config,
        'config_sha256':digest(config_path), 'harness_sha256':digest(Path(__file__)),
        'manufacturing_authorized':False}
    owned_job_ids = set()
    def save():
        write_json(args.output,result)
    async def call(client, name, arguments):
        response = await asyncio.wait_for(client.call_tool(name,arguments), 180)
        if response.isError:
            raise RuntimeError(str(response.content))
        return json.loads(next(c.text for c in response.content if c.type == 'text'))
    try:
        if not external_worker:
            queue.start()
        for repetition in range(1,args.repeats+1):
            entry = {'repetition':repetition, 'status':'running', 'project':f'{args.project}-{repetition}'}
            result['runs'].append(entry)
            save()
            try:
                async with stdio_client(parameters) as (read,write):
                    async with ClientSession(read,write) as client:
                        await client.initialize()
                        schema = next(t for t in (await client.list_tools()).tools if t.name == 'submit_pcb_completion')
                        entry['schema'] = schema.inputSchema
                        imported = await call(client,'import_pcb_project',{'project':entry['project'],
                            'board_path':str(fixture / manifest['board']),
                            'constraints':read_json(fixture / 'constraints.json')})
                        entry['input_revision'] = imported['revision']['id']
                        initial, initial_root = engine._verified(entry['project'],entry['input_revision'])
                        expected = design_files(fixture)
                        if initial['files'] != expected:
                            raise ValueError('Import changed frozen design files or rules')
                        if read_json(initial_root / 'constraints.json') != read_json(fixture / 'constraints.json'):
                            raise ValueError('Import changed frozen constraints')
                        job = await call(client,'submit_pcb_completion',{'project':entry['project'],
                            'revision':entry['input_revision'], 'options':options.model_dump()})
                        entry['job_id'] = job['id']
                        owned_job_ids.add(job['id'])
                        save()
                entry['submitter_disconnected'] = True
                print(json.dumps({'project':entry['project'],'job':entry['job_id'],'stage':'disconnected'}),flush=True)
                async with stdio_client(parameters) as (read,write):
                    async with ClientSession(read,write) as client:
                        await client.initialize()
                        deadline = time.monotonic()+options.time_budget_seconds+engine.toolchain.route_timeout+600
                        previous = None
                        while True:
                            job = await call(client,'get_engineering_job',{'job_id':entry['job_id']})
                            marker = (job['status'],job.get('stage'))
                            if marker != previous:
                                print(json.dumps({'job':job['id'],'status':marker[0],'stage':marker[1]}),flush=True)
                                previous = marker
                            if job['status'] not in {'queued','running'}:
                                break
                            if time.monotonic() > deadline:
                                await call(client,'cancel_engineering_job',{'job_id':entry['job_id']})
                                raise TimeoutError('Completion timeout; cancellation requested')
                            await asyncio.sleep(8)
                entry['job'] = job
                flow = job.get('result',{}).get('steps',{}).get('complete',{})
                if job['status'] != 'completed':
                    entry['job_failure_reason'] = flow.get('reason') or job.get('result',{}).get('error') or job['status']
                entry['revision'] = flow.get('revision',entry['input_revision'])
                entry['metrics'] = flow.get('best')
                save()
                initial, initial_root = engine._verified(entry['project'],entry['input_revision'])
                final, final_root = engine._verified(entry['project'],entry['revision'])
                source = initial_root / 'design' / initial['board']
                target = final_root / 'design' / final['board']
                assert digest(source) == manifest['input_board_sha256'], 'Import changed frozen board'
                assert nonrouting_ast(source) == nonrouting_ast(target), 'Nonrouting board AST changed'
                entry['nonrouting_ast_preserved'] = True
                before,after = read_board(source),read_board(target)
                assert before['tracks'] == before['vias'] == 0
                assert engine._electrical_signature(before) == engine._electrical_signature(after)
                assert before['footprints'] == after['footprints'], 'Footprint geometry/placement changed'
                assert before['outline'] == after['outline']
                assert set(initial['files']) == set(final['files']), 'Companion file set changed'
                assert read_json(initial_root / 'constraints.json') == read_json(final_root / 'constraints.json')
                for name, sha in initial['files'].items():
                    if name != initial['board']:
                        assert digest(final_root / 'design' / name) == sha, 'Non-board design file changed: '+name
                baseline, check = verify_pair(engine,entry['project'],entry['input_revision'],entry['revision'])
                entry['independent_verification'] = check
                entry['baseline_verification'] = baseline
                new_drc = _violations(_report(engine,entry['project'],entry['revision'],check))-_violations(
                    _report(engine,entry['project'],entry['input_revision'],baseline))
                new_erc = _erc_violations(_erc_report(engine,entry['project'],entry['revision'],check))-_erc_violations(
                    _erc_report(engine,entry['project'],entry['input_revision'],baseline))
                entry['new_findings'] = {'drc':sum(new_drc.values()), 'erc':sum(new_erc.values())}
                entry['report'] = catalog.generate_report(engine,entry['project'],entry['revision'])
                assert job['status'] == 'completed', entry.get('job_failure_reason',job['status'])
                assert not new_drc and not new_erc, 'New native findings relative to unrouted input'
                assert check['status'] == 'passed'
                assert (check['drc']['errors'],check['drc']['unconnected'],check['erc']['errors']) == (0,0,0)
                assert after['tracks'] > 0 and not flow['manufacturing_authorized']
                assert flow['best']['moved_components'] == 0
                entry.update(status='passed',board_sha256=digest(target))
            except (Exception,AssertionError) as error:
                entry.update(status='failed',error=type(error).__name__+': '+str(error))
                entry['cleanup'] = cancel_owned_jobs(queue, [entry['job_id']] if 'job_id' in entry else [])
            fixture_verified(fixture)
            save()
            print(json.dumps({'project':entry['project'],'status':entry['status'],'error':entry.get('error')}),flush=True)
        actual_implementation = files(root / 'src' / 'pcb_weaver')
        actual_implementation = {k:v for k,v in actual_implementation.items() if '__pycache__' not in k and k.endswith('.py')}
        if actual_implementation != implementation:
            raise ValueError('Implementation changed during benchmark; rerun with frozen code')
        if digest(config_path) != result['config_sha256'] or digest(Path(__file__)) != result['harness_sha256']:
            raise ValueError('Execution configuration or acceptance script changed during benchmark')
        result['passed_runs'] = sum(r['status']=='passed' for r in result['runs'])
        result['success_rate'] = result['passed_runs']/args.repeats
        result['status'] = 'passed' if result['passed_runs']==args.repeats else 'failed'
    except BaseException as error:
        result.update(status='failed',error=type(error).__name__+': '+str(error))
        raise
    finally:
        result['cleanup'] = cancel_owned_jobs(queue, owned_job_ids)
        if result['cleanup']['errors']:
            result['status'] = 'failed'
        try:
            if not external_worker:
                queue.close()
                if queue.thread:
                    queue.thread.join()
        finally:
            save()
    print(json.dumps({'status':result['status'],'output':str(args.output)}),flush=True)
    return result['status'] == 'passed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--source',type=Path,required=True)
    prep.add_argument('--output',type=Path,required=True)
    execute = commands.add_parser('run')
    for key in ('root','fixture','options','output'):
        execute.add_argument('--'+key,type=Path,required=True)
    execute.add_argument('--project',required=True)
    execute.add_argument('--config',type=Path)
    execute.add_argument('--workspace',type=Path,
        help='Execution workspace; defaults to a fresh root/benchmark-data/<uuid> directory')
    execute.add_argument('--external-worker',action='store_true',
        help='Use an independently managed worker; required for any existing workspace')
    execute.add_argument('--repeats',type=int,choices=range(1,6),default=2)
    args = parser.parse_args()
    if args.command == 'prepare':
        print(json.dumps(prepare(args.source,args.output)),flush=True)
    else:
        sys.exit(0 if asyncio.run(run(args)) else 1)


if __name__ == '__main__':
    main()
