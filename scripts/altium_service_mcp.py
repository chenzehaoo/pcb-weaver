"""Local stdio MCP facade for the durable Altium developer-beta service."""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from altium_service import AltiumService


server = FastMCP('Altium Local Developer Beta', instructions=(
    'Local developer beta only. Static inspection does not run Altium. '
    'Native checks use isolated copies and require explicit native launch enablement. '
    'Automatic routing, full DRC coverage and manufacturing release are unavailable.'))


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_service_status() -> dict:
    """Return configuration, queue counts and honest native capability flags."""
    return AltiumService().status()


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def altium_submit_inspection(project_path: str, board_path: str | None = None,
                             request_id: str | None = None) -> dict:
    """Queue a static inspection of an allowlisted Altium project."""
    return AltiumService().submit('inspect', project_path, board_path, request_id=request_id)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def altium_submit_native_check(project_path: str, board_path: str | None = None,
                               run_drc: bool = False, request_id: str | None = None) -> dict:
    """Queue isolated native compile and optional selected-rule DRC; no routing."""
    return AltiumService().submit('check', project_path, board_path, run_drc, request_id)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_get_job(job_id: str) -> dict:
    """Read a durable job and its result or blocking reason."""
    return AltiumService().get_job(job_id)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_get_native_report(run_id: str, offset: int = 0, limit: int = 50) -> dict:
    """Read a completed isolated native compile or selected-rule DRC report."""
    from altium_beta import Beta
    return Beta().report(run_id, offset, limit)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_list_jobs(offset: int = 0, limit: int = 20) -> dict:
    """List local Altium developer-beta jobs."""
    return AltiumService().list_jobs(offset, limit)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def altium_cancel_queued_job(job_id: str) -> dict:
    """Cancel only a queued job; never interrupt an active Altium process."""
    return AltiumService().cancel(job_id)


if __name__ == '__main__':
    server.run(transport='stdio')
