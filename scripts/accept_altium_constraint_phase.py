"""MCP evidence-access acceptance; deliberately not a board-release acceptance."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from accept_altium_inventory import decode


def require(condition, message):
    if not condition:
        raise ValueError(message)


async def main():
    root = Path(__file__).resolve().parents[1]
    output = root / 'docs/validation/altium-constraint-phase-mcp.json'
    require(not output.exists(), 'Evidence output already exists')
    params = StdioServerParameters(command=sys.executable,
        args=[str(root / 'scripts/altium_bridge_mcp.py')],
        env={**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    evidence = {'status': 'running', 'scope': 'read_only_mcp_evidence_access_not_board_acceptance'}
    try:
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                for suffix in ('constraints_snapshot', 'geometry_snapshot', 'dsn_audit', 'drc_coverage_snapshot'):
                    name = 'altium_wifi_' + suffix
                    require(name in tools and tools[name].annotations.readOnlyHint is True, 'Missing read-only tool')
                    evidence[suffix] = decode(await session.call_tool(name, {}))
                numeric = evidence['constraints_snapshot']
                require(numeric['rule_count'] == 40 and len(numeric['captured_numeric_rules']) == 6,
                        'Numeric rule count mismatch')
                geometry = evidence['geometry_snapshot']
                require(geometry['electrical_layer_ids'] == [1, 2, 3, 32] and geometry['pad_count'] == 200,
                        'Native geometry mismatch')
                pads = []
                for offset in range(0, 200, 50):
                    page = decode(await session.call_tool('altium_wifi_geometry_snapshot',
                        {'section': 'pads', 'offset': offset, 'limit': 50}))
                    require(page['total'] == 200 and page['offset'] == offset, 'Wrong geometry page')
                    pads.extend(page['items'])
                require(len(pads) == 200 and len({row['section'] for row in pads}) == 200,
                        'Incomplete geometry pagination')
                evidence['pad_pages_count'] = len(pads)
                dsn = evidence['dsn_audit']
                require(dsn['audit_valid'] and dsn['counts']['placements'] == 28
                        and not dsn['full_autoroute_authorized'], 'Incorrect DSN audit gate')
                drc = evidence['drc_coverage_snapshot']
                require(drc['counts']['violations'] == 158 and drc['ui_batch_types_enabled'] == 55
                        and drc['status'] == 'blocked', 'Incorrect expanded DRC status')
                require(all(not evidence[key]['automatic_routing_authorized'] for key in (
                    'constraints_snapshot', 'geometry_snapshot', 'drc_coverage_snapshot')), 'Unexpected routing authorization')
        evidence.update(status='passed', board_release='blocked', automatic_routing='not_accepted')
    except Exception as error:
        evidence.update(status='failed', error=str(error))
        raise
    finally:
        with output.open('x', encoding='utf-8') as handle:
            json.dump(evidence, handle, indent=2, allow_nan=False)
    print(json.dumps({'mcp_evidence_access': evidence['status'], 'board_release': 'blocked',
                      'native_violations': 158, 'pad_pages_count': evidence['pad_pages_count']}))


if __name__ == '__main__':
    asyncio.run(main())
