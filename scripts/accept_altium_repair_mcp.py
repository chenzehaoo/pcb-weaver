"""Real MCP acceptance of read-only local repair evidence."""
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
    output = root / 'docs/validation/altium-local-repair-mcp.json'
    if output.exists():
        raise ValueError('Evidence already exists')
    params = StdioServerParameters(command=sys.executable,
        args=[str(root / 'scripts/altium_bridge_mcp.py')],
        env={**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = decode(await session.call_tool('altium_wifi_local_repair_status', {}))
            if (result['repair_acceptance'] != 'passed' or
                    result['report_diff']['before']['violations'] != 158 or
                    result['report_diff']['after']['violations'] != 157):
                raise ValueError('Repair evidence rejected')
    with output.open('x', encoding='utf-8') as handle:
        json.dump({'mcp_acceptance': 'passed', 'scope': 'isolated_board_repair_evidence',
                   'result': result}, handle, indent=2)
    print('MCP local repair evidence acceptance passed; no full-project release.')


if __name__ == '__main__':
    asyncio.run(main())
