"""
main.py — MCP server entry point.

Startup sequence:
  1. Load .env
  2. Load config from env vars
  3. Initialize SQLite store
  4. Instantiate all three components
  5. Register MCP tools (executor tools + fetch_fragment)
  6. Start MCP server (stdio or SSE based on MCP_TRANSPORT)

Shutdown:
  - SSE mode: server handles SIGTERM
  - stdio mode: process exits when stdin closes
  - teardown_all() destroys all active E2B sandboxes
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from agent_gateway.config import load_config
from agent_gateway.session import SessionRegistry
from agent_gateway.context_optimizer.optimizer import ContextOptimizer
from agent_gateway.context_optimizer.store import ResultStore
from agent_gateway.loop_arbiter.arbiter import LoopArbiter
from agent_gateway.secure_executor.gateway import SecureExecutorGateway
from agent_gateway.secure_executor.tools import register_executor_tools

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def build_app() -> tuple[FastMCP, SessionRegistry]:
    """
    Assemble all components and return the configured MCP server.
    Separated from run() to allow testing without actually starting the server.
    """
    optimizer_cfg, arbiter_cfg, gateway_cfg = load_config()

    # ── Infrastructure ────────────────────────────────────────────────────────
    store = ResultStore(db_path=optimizer_cfg.db_path)
    await store.initialize()

    purged = await store.purge_expired()
    if purged:
        log.info("Startup: purged %d expired result store entries", purged)

    # ── Components ────────────────────────────────────────────────────────────
    optimizer = ContextOptimizer(config=optimizer_cfg, store=store)
    arbiter   = LoopArbiter(config=arbiter_cfg)
    gateway   = SecureExecutorGateway(config=gateway_cfg)
    registry  = SessionRegistry(arbiter_cfg, gateway_cfg)

    # ── MCP server ────────────────────────────────────────────────────────────
    mcp = FastMCP("AgentGateway")

    # Executor tools (execute_python, execute_shell, execute_javascript,
    #                 upload_to_sandbox, download_from_sandbox, reset_sandbox)
    register_executor_tools(mcp, gateway, registry)

    # fetch_fragment — retrieval tool for the Context Optimizer
    frag_limit = optimizer_cfg.fragment_max_limit

    @mcp.tool()
    async def fetch_fragment(
        ref_id: str,
        offset: int = 0,
        limit:  int = 50,
    ) -> str:
        """
        Retrieve a slice of a previously truncated tool result.

        Args:
            ref_id: Reference ID from the truncation message (e.g. "readfile_a3f2b891").
            offset: Zero-indexed line to start from (default: 0).
            limit:  Number of lines to return (default: 50, max: 100).

        Returns:
            Formatted fragment with position info and pagination hint.
        """
        limit = min(limit, frag_limit)

        raw = await store.load(ref_id)
        if raw is None:
            return (
                f"[Error] ref_id '{ref_id}' not found or expired. "
                "Re-run the original tool to generate a fresh reference."
            )

        lines = raw.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        slice_ = lines[offset : offset + limit]

        if not slice_:
            return f"[Error] offset={offset} is out of range. Total lines: {total}"

        end_idx = offset + len(slice_) - 1
        header  = f"[Fragment: {ref_id} | lines {offset}–{end_idx} of {total}]"

        if offset + limit < total:
            footer = (
                f"[More available: "
                f"fetch_fragment(ref_id='{ref_id}', offset={offset + limit}, limit={limit})]"
            )
        else:
            footer = f"[End of result — {total} total lines]"

        return f"{header}\n" + "\n".join(slice_) + f"\n{footer}"

    return mcp, registry


def run() -> None:
    """Entry point for `agent-gateway` CLI command."""
    load_dotenv()
    transport = os.environ.get("MCP_TRANSPORT", "stdio")

    async def _run() -> None:
        mcp, registry = await build_app()

        # Register cleanup handler
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(registry.teardown_all()),
            )

        log.info("AgentGateway starting — transport=%s", transport)

        if transport == "sse":
            host = os.environ.get("MCP_SSE_HOST", "127.0.0.1")
            port = int(os.environ.get("MCP_SSE_PORT", "8000"))
            log.info("SSE server listening on %s:%d", host, port)
            await mcp.run_sse_async(host=host, port=port)
        else:
            await mcp.run_stdio_async()

    asyncio.run(_run())


if __name__ == "__main__":
    run()
