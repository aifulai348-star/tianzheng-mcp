from mcp.server.fastmcp import FastMCP
from .cli import store, capabilities, doctor, acknowledge, acknowledgement

mcp = FastMCP('tianzheng-local')


@mcp.tool()
def system_diagnose() -> dict:
    """Inspect local AutoCAD 2024/T20 V9 environment without creating a drawing."""
    return doctor()


@mcp.tool()
def list_capabilities() -> dict:
    """List structured operations and their validation scope."""
    return capabilities()


@mcp.tool()
def queue_submit(recipe: dict) -> dict:
    """Enqueue ordered steps; worker must be started explicitly using worker_start."""
    return {'job_id': store().submit(recipe)}


@mcp.tool()
def queue_acknowledge(job_id: str, side_effects: str) -> dict:
    """Close an unknown_outcome job after a human inspected the drawing.

    Unknown is not the same as nothing happened: the timeout or crash may have
    left a wall, a column or a half-driven palette behind. So side_effects is
    required and must say what the drawing actually shows (or that it is
    unchanged). It is stored on the job row and as a receipt under the state
    directory before the job is closed. Only unknown_outcome may be
    acknowledged, and it is never replayed. The OS queue lock is held while
    checking, so an abandoned worker cannot still be running against the same
    queue.
    """
    return acknowledge(job_id, side_effects)


@mcp.tool()
def queue_status(job_id: str) -> dict:
    """Read state, durable context, step results and recovery evidence."""
    out = store().get(job_id)
    receipt = acknowledgement(job_id)
    if receipt:
        out['acknowledgement'] = receipt
    return out


@mcp.tool()
def queue_cancel(job_id: str) -> dict:
    """Cancel queued work or request cancellation at a running step boundary."""
    store().cancel(job_id)
    return {'cancel_requested': True}


@mcp.tool()
def worker_start() -> dict:
    """Start a hidden worker; attaches only to an already running Tianzheng CAD."""
    from .worker import start
    return start()


@mcp.tool()
def worker_stop() -> dict:
    """Request a graceful stop: the worker exits at the next step/job boundary."""
    from .worker import stop
    return stop()


@mcp.tool()
def worker_status() -> dict:
    """Read last worker state; a blocked COM call may leave a stale timestamp."""
    from .worker import status
    return status()


def serve():
    mcp.run(transport='stdio')
