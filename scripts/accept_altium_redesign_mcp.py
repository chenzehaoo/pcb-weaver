"""Verify route-B status through a real stdio MCP session, not direct invocation."""
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
    output = root / 'docs/validation/altium-redesign-mcp.json'
    if output.exists():
        raise ValueError('Evidence already exists')
    params = StdioServerParameters(command=sys.executable,
        args=[str(root / 'scripts/altium_bridge_mcp.py')],
        env={**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = decode(await session.call_tool('altium_wifi_redesign_status', {'include_findings': True}))
            if (result['status'] != 'redesign_required' or not result['native_compile']
                    or result['counts']['violations'] != 159 or result['native_drc_pass']
                    or result['manufacturing_release']
                    or sum(len(row['items']) for row in result['rule_details']) != 159):
                raise ValueError('Invalid redesign evidence')
    with output.open('x', encoding='utf-8') as handle:
        json.dump({'mcp_acceptance': 'passed', 'scope': 'route_b_evidence_not_board_acceptance',
                   'result': result}, handle, indent=2)
    print('Route-B MCP status verified: 159 findings, no manufacturing release.')


if __name__ == '__main__':
    asyncio.run(main())
