"""Real stdio MCP acceptance of captured numeric constraints, not routing."""
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
    params = StdioServerParameters(command=sys.executable,
        args=[str(root / 'scripts/altium_bridge_mcp.py')],
        env={**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            if 'altium_wifi_constraints_snapshot' not in names:
                raise ValueError('Missing constraints MCP tool')
            result = decode(await session.call_tool('altium_wifi_constraints_snapshot', {}))
            if (result['rule_count'] != 40 or len(result['captured_numeric_rules']) != 6 or
                    len(result['uncaptured_numeric_rules']) != 34 or result['automatic_routing_authorized']):
                raise ValueError('Wrong snapshot or readiness')
    evidence = {'acceptance': 'passed', 'scope': 'read_only_numeric_constraints_mcp', 'snapshot': result}
    output = root / 'docs/validation/altium-constraints-mcp.json'
    with output.open('x', encoding='utf-8') as handle:
        json.dump(evidence, handle, indent=2)
    print(json.dumps({'acceptance': 'passed', 'numeric_rules': 6, 'full_routing': 'not_authorized'}))


if __name__ == '__main__':
    asyncio.run(main())
