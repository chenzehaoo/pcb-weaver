"""Verify accepted silkscreen evidence through a real stdio MCP session."""
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
    output = root / 'docs/validation/altium-silk-mcp.json'
    if output.exists():
        raise ValueError('Evidence already exists')
    params = StdioServerParameters(command=sys.executable,
        args=[str(root / 'scripts/altium_bridge_mcp.py')],
        env={**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = decode(await session.call_tool('altium_wifi_silk_repair_status', {'include_findings': True}))
            if (not result['local_repair_accepted'] or result['before_violations'] != 159
                    or result['after_violations'] != 154 or result['native_drc_pass']
                    or result['manufacturing_release']
                    or sum(len(row['items']) for row in result['findings']) != 154):
                raise ValueError('Invalid silk repair evidence')
    with output.open('x', encoding='utf-8') as handle:
        json.dump({'mcp_acceptance': 'passed', 'scope': 'local_silk_repair_not_board_release',
                   'result': result}, handle, indent=2)
    print('MCP verified: local silk repair accepted, 154 remaining findings, no board release.')


if __name__ == '__main__':
    asyncio.run(main())
