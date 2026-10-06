"""Exercise the configured local Altium service without native Altium execution."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from altium_beta import Beta


ROOT = Path(__file__).resolve().parents[1]
PROJECT = Path('D:/Altium/AD26-Examples/Examples/SpiritLevel-SL1/'
               'SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb')


def decode(response):
    if response.isError:
        raise RuntimeError(str(response))
    if response.structuredContent is not None:
        return response.structuredContent
    return json.loads(response.content[0].text)


async def main():
    config = json.loads((ROOT / 'altium-service.mcp.json').read_text(encoding='utf-8'))
    config = config['mcpServers']['altium-local-developer-beta']
    env = dict(os.environ, **config['env'])
    beta = Beta(roots=config['env']['ALTIUM_BETA_PROJECT_ROOTS'].split(os.pathsep),
                run_root=config['env']['ALTIUM_BETA_RUN_ROOT'], exe=config['env']['ALTIUM_BETA_EXE'])
    before = beta.inspect(str(PROJECT))
    params = StdioServerParameters(command=config['command'], args=config['args'], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tool_names = {tool.name for tool in (await client.list_tools()).tools}
            status = decode(await client.call_tool('altium_service_status', {}))
            if status['native_launch_enabled'] or status['automatic_routing']:
                raise ValueError('Acceptance requires native launch and routing disabled')
            request_id = 'service-accept-' + uuid.uuid4().hex
            queued = decode(await client.call_tool('altium_submit_inspection',
                {'project_path': str(PROJECT), 'request_id': request_id}))
            if queued['state'] != 'queued':
                raise ValueError('Inspection was not queued')

    worker = subprocess.run([config['command'], '-B', str(ROOT / 'scripts/altium_service_worker.py'),
                             '--once'], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    if worker.returncode != 0:
        raise RuntimeError(worker.stderr)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            completed = decode(await client.call_tool('altium_get_job', {'job_id': queued['id']}))
            listed = decode(await client.call_tool('altium_list_jobs', {'limit': 100}))
    after = beta.inspect(str(PROJECT))
    if (completed['state'] != 'completed' or completed['result']['native_executed']
            or completed['result']['inspection'] != before or after != before
            or not any(item['id'] == queued['id'] for item in listed['items'])):
        raise ValueError('Persistent MCP inspection did not pass')
    evidence = {'status': 'passed', 'scope': 'local_mcp_static_inspection_only',
                'job_id': queued['id'], 'project': str(PROJECT),
                'project_sha256': before['project_sha256'], 'file_count': before['file_count'],
                'mcp_tools': sorted(tool_names), 'worker_returncode': worker.returncode,
                'source_unchanged': True, 'native_executed': False,
                'automatic_routing': False, 'manufacturing_release': False}
    output = ROOT / 'docs/validation/altium-service-beta/acceptance.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
