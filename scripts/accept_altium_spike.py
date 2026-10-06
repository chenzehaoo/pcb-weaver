"""Real SDK client; native scripts must reply before this can pass."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(root / 'scripts/altium_bridge_mcp.py')],
        env={**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            if not {'altium_test_status', 'altium_test_roundtrip'} <= names:
                raise RuntimeError('Missing tools')
            status = await session.call_tool('altium_test_status', {})
            if status.isError:
                raise RuntimeError(str(status))
            response = await session.call_tool('altium_test_roundtrip', {'confirm_test_copy': True})
            if response.isError:
                raise RuntimeError(str(response))
            data = response.structuredContent
            if data is None:
                blocks = [item.text for item in response.content if item.type == 'text']
                if len(blocks) != 1:
                    raise RuntimeError('Expected one JSON result block')
                data = json.loads(blocks[0])
            if not data or data.get('status') != 'passed':
                raise RuntimeError(str(response))
            print(json.dumps(data, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
