"""Separate local Altium read-only developer beta MCP server."""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from altium_beta import Beta

server = FastMCP('Altium Developer Beta', instructions=(
    'This server checks allowed local Altium projects on isolated copies. '
    'Compile and selected-rule DRC results are not manufacturing approval.'))


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_beta_status() -> dict:
    """Check local installation, allowed project roots and native busy state."""
    return Beta().ready()


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_beta_inspect(project_path: str, board_path: str | None = None) -> dict:
    """Inspect one allowed PrjPcb and its explicit document dependencies."""
    return Beta().inspect(project_path, board_path)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def altium_beta_check(project_path: str, board_path: str | None = None, run_drc: bool = False) -> dict:
    """Copy an allowed project and run native compilation and optional selected-rule DRC."""
    return Beta().check(project_path, board_path, run_drc)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_beta_report(run_id: str, offset: int = 0, limit: int = 50) -> dict:
    """Return a validated run result and a page of native DRC findings."""
    return Beta().report(run_id, offset, limit)


if __name__ == '__main__':
    server.run(transport='stdio')
