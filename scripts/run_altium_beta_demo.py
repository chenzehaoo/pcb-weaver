"""Run every beta MCP tool on the prepared demo and persist each response."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from accept_altium_inventory import decode
from altium_project_snapshot import ROOT, _read, _sha, _write_new
from altium_silk_status import snapshot as silk_snapshot


def save(path, value):
    _write_new(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


async def main():
    package = ROOT / 'docs/validation/altium-redesign/developer-demo-v1/demo-manifest.json'
    manifest = json.loads(_read(package))
    root = ROOT / 'docs/validation/altium-beta-demo-v1'
    root.mkdir(exist_ok=False)
    config_path = ROOT / 'altium-beta.mcp.json'
    config = json.loads(_read(config_path))['mcpServers']['altium-developer-beta']
    server = StdioServerParameters(command=config['command'], args=config['args'],
        env={**os.environ, **config['env'], 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    evidence = {'status': 'failed', 'example_project': manifest['project'],
                'example_board': manifest['board'], 'started_utc': datetime.now(timezone.utc).isoformat(),
                'mcp_config': str(config_path), 'steps': [], 'beta_run_id': None}
    save(root / 'configuration.json', config)
    save(root / 'example-manifest.json', manifest)
    try:
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialization = await session.initialize()
                listed = await session.list_tools()
                names = {item.name for item in listed.tools}
                expected = {'altium_beta_status', 'altium_beta_inspect',
                            'altium_beta_check', 'altium_beta_report'}
                if not expected <= names:
                    raise ValueError('Beta tools missing')
                save(root / '00-session.json', {'initialize': initialization.model_dump(mode='json'),
                                               'tools': [item.model_dump(mode='json') for item in listed.tools]})

                async def call(number, name, arguments):
                    started = datetime.now(timezone.utc)
                    elapsed = time.monotonic()
                    response = decode(await session.call_tool(name, arguments))
                    record = {'step': number, 'tool': name, 'arguments': arguments,
                              'started_utc': started.isoformat(),
                              'duration_ms': round((time.monotonic() - elapsed) * 1000),
                              'response': response}
                    save(root / f'{number:02d}-{name}.json', record)
                    evidence['steps'].append({'step': number, 'tool': name,
                                              'file': f'{number:02d}-{name}.json',
                                              'duration_ms': record['duration_ms']})
                    return response

                before = await call(1, 'altium_beta_status', {})
                if not before['altium_available'] or before['native_busy_or_uncertain']:
                    raise ValueError('Altium unavailable or busy')
                inspected = await call(2, 'altium_beta_inspect', {'project_path': manifest['project']})
                if inspected['project'] != manifest['project'] or len(inspected['files']) != 2:
                    raise ValueError('Example inspection mismatch')
                checked = await call(3, 'altium_beta_check',
                                     {'project_path': manifest['project'], 'run_drc': True})
                evidence['beta_run_id'] = checked.get('run_id')
                report = await call(4, 'altium_beta_report',
                                    {'run_id': checked['run_id'], 'offset': 0, 'limit': 200})
                after = await call(5, 'altium_beta_status', {})
                if after['native_busy_or_uncertain']:
                    raise ValueError('Native lock remains after completed run')
        if (not checked['native_compile'] or checked['full_drc_pass'] or
                not checked['files_unchanged'] or checked['drc_counts']['rule_rows'] < 1 or
                report['run_id'] != checked['run_id'] or report['full_drc_pass'] or
                report['findings_total'] != checked['drc_counts']['violations']):
            raise ValueError('MCP result validation failed')
        for row in manifest['files']:
            if _sha(_read(Path(row['source']))) != row['source_sha256'] or _sha(_read(Path(row['copy']))) != row['copy_sha256']:
                raise ValueError('Example or parent source changed')
        history = silk_snapshot()
        if _sha(_read(Path(manifest['board']))) != _sha(_read(Path(history['board']))):
            raise ValueError('Example board differs from historical full DRC board')
        evidence.update(status='passed', native_compile=True,
                        selected_rules_pass=checked['selected_rules_pass'],
                        selected_rule_rows=checked['drc_counts']['rule_rows'],
                        selected_rule_violations=checked['drc_counts']['violations'],
                        full_drc_pass=False, manufacturing_release=False,
                        source_files_unchanged=True, historical_full_rule_violations=history['after_violations'],
                        historical_comparison_board_sha256=_sha(_read(Path(manifest['board']))),
                        ended_utc=datetime.now(timezone.utc).isoformat())
    except Exception as error:
        evidence['error'] = str(error)
        raise
    finally:
        save(root / 'summary.json', evidence)
    print(json.dumps({'status': evidence['status'], 'run_id': evidence['beta_run_id'],
                      'selected_rule_rows': evidence['selected_rule_rows'],
                      'selected_rule_violations': evidence['selected_rule_violations'],
                      'historical_full_rule_violations': evidence['historical_full_rule_violations'],
                      'output': str(root)}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
