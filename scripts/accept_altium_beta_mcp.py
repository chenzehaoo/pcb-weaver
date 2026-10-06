"""End-to-end local beta acceptance using two distinct native projects via MCP."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from accept_altium_inventory import decode


async def main():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / 'altium-beta.mcp.json').read_text())['mcpServers']['altium-developer-beta']
    output = root / 'docs/validation/altium-beta-mcp-acceptance.json'
    if output.exists():
        raise ValueError('Acceptance already exists')
    params = StdioServerParameters(command=config['command'], args=config['args'],
        env={**os.environ, **config['env'], 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    wifi = 'D:/Altium/AD26-Examples/Examples/Mini PC/Mini PC - WiFi/WiFi_miniPCIe.PrjPcb'
    spirit = 'D:/Altium/AD26-Examples/Examples/SpiritLevel-SL1/SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb'
    evidence = {'status': 'failed', 'scope': 'two_project_local_native_mcp_beta',
                'automatic_routing': False, 'manufacturing_release': False}
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            required = {'altium_beta_status', 'altium_beta_inspect', 'altium_beta_check', 'altium_beta_report'}
            if not required <= tools:
                raise ValueError('Missing beta MCP tools')
            ready = decode(await session.call_tool('altium_beta_status', {}))
            if not ready['altium_available'] or ready['native_busy_or_uncertain']:
                raise ValueError('Native environment unavailable')
            wi = decode(await session.call_tool('altium_beta_inspect', {'project_path': wifi, 'board_path': 'WiFi.PcbDoc'}))
            sp = decode(await session.call_tool('altium_beta_inspect', {'project_path': spirit}))
            if len(wi['files']) != 6 or len(sp['files']) != 9 or len(wi['available_boards']) != 2:
                raise ValueError('Project inspection mismatch')
            spirit_result = decode(await session.call_tool('altium_beta_check', {'project_path': spirit}))
            wifi_result = decode(await session.call_tool('altium_beta_check',
                {'project_path': wifi, 'board_path': 'WiFi.PcbDoc', 'run_drc': True}))
            wifi_report = decode(await session.call_tool('altium_beta_report', {'run_id': wifi_result['run_id']}))
            spirit_report = decode(await session.call_tool('altium_beta_report', {'run_id': spirit_result['run_id']}))
            if (not spirit_result['native_compile'] or spirit_result['drc_requested']
                    or not wifi_result['native_compile'] or not wifi_result['drc_requested']
                    or wifi_result['full_drc_pass'] or wifi_report['full_drc_pass']
                    or wifi_report['drc_counts']['rule_rows'] != 6
                    or spirit_report['findings_total'] != 0
                    or not wifi_report['files_unchanged'] or not spirit_report['files_unchanged']):
                raise ValueError('Native results or coverage gate mismatch')
            evidence.update(status='passed', wifi_run_id=wifi_result['run_id'],
                            spirit_run_id=spirit_result['run_id'],
                            wifi_drc_counts=wifi_report['drc_counts'],
                            wifi_selected_rules_pass=wifi_report['selected_rules_pass'],
                            full_drc_pass=False, source_files_unchanged=True,
                            tool_names=sorted(required))
    with output.open('x', encoding='utf-8') as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
