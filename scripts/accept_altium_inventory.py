"""Real MCP read-only inventory acceptance; not native routing acceptance."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def decode(response):
    if response.isError:
        raise RuntimeError(str(response))
    if response.structuredContent is not None:
        return response.structuredContent
    blocks = [c.text for c in response.content if c.type == 'text']
    if len(blocks) != 1:
        raise RuntimeError('Expected exactly one JSON block')
    return json.loads(blocks[0])


async def main():
    root = Path(__file__).resolve().parents[1]
    output = root / 'docs/validation/altium-wifi-mcp-inventory.json'
    if output.exists():
        raise RuntimeError('Acceptance output already exists')
    params = StdioServerParameters(command=sys.executable,
        args=[str(root / 'scripts/altium_bridge_mcp.py')],
        env={**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    evidence = {'status': 'running', 'scope': 'read_only_mcp_inventory', 'sections': {}}
    try:
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                if 'altium_wifi_inventory_snapshot' not in names:
                    raise RuntimeError('Missing snapshot tool')
                summary = decode(await session.call_tool('altium_wifi_inventory_snapshot', {}))
                gate = summary['gate']
                if not gate['inventory_valid'] or gate['full_autoroute']['authorized']:
                    raise RuntimeError('Incorrect readiness gate')
                evidence['gate'] = gate
                for section, expected in {'components': 28, 'pads': 200, 'nets': 27, 'rules': 40}.items():
                    rows = []
                    for offset in range(0, expected, 37):
                        page = decode(await session.call_tool('altium_wifi_inventory_snapshot',
                            {'section': section, 'offset': offset, 'limit': 37}))
                        if page['total'] != expected:
                            raise RuntimeError('Unexpected section count')
                        rows.extend(page['items'])
                    if len(rows) != expected:
                        raise RuntimeError('Incomplete pagination')
                    evidence['sections'][section] = rows
        evidence.update(status='passed', automatic_routing='blocked', manufacturing_authorized=False)
    except Exception as error:
        evidence.update(status='failed', error=str(error))
        raise
    finally:
        output.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps({'status': evidence['status'], 'counts': evidence['gate']['counts'],
                      'automatic_routing': evidence['automatic_routing']}))


if __name__ == '__main__':
    asyncio.run(main())
