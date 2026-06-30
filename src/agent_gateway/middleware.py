"""
middleware.py — tool_middleware() wraps domain tools with the full pipeline:

  1. LoopArbiter.check()    — BEFORE execution (circuit break)
  2. tool_fn(**kwargs)      — actual tool execution
  3. ContextOptimizer.process() — AFTER execution (compress output)

Usage:
    @mcp.tool()
    @tool_middleware("read_file", arbiter, optimizer, registry)
    async def read_file(path: str, session_id: str = "default") -> str:
        ...

The `session_id` kwarg is used to look up the session but excluded from
the args hash (it's infrastructure, not logic).
"""
from __future__ import annotations

import functools
import logging
from typing import Awaitable, Callable

from agent_gateway.loop_arbiter.arbiter import LoopArbiter
from agent_gateway.context_optimizer.optimizer import ContextOptimizer
from agent_gateway.session import SessionRegistry
from agent_gateway.types import ToolName

log = logging.getLogger(__name__)


def tool_middleware(
    tool_name: ToolName,
    arbiter:   LoopArbiter,
    optimizer: ContextOptimizer,
    registry:  SessionRegistry,
) -> Callable[[Callable[..., Awaitable]], Callable]:
    """
    Decorator factory. Wraps an async tool function with:
      1. Loop check (raises ValueError on circuit break)
      2. Tool execution
      3. Context optimization of output

    The wrapped function must accept `session_id: str = "default"` as a kwarg.
    """
    def decorator(fn: Callable[..., Awaitable]) -> Callable:
        @functools.wraps(fn)
        async def wrapper(**kwargs) -> str:
            session_id = kwargs.get("session_id", "default")
            ctx        = registry.get_or_create(session_id)

            # Exclude session_id from the args hash (it's routing metadata)
            tool_args = {k: v for k, v in kwargs.items() if k != "session_id"}

            # 1. Loop check — raises on circuit break
            break_result = arbiter.check(tool_name, tool_args, ctx.state)
            if break_result:
                log.warning(
                    "[Middleware] Circuit break on tool=%s session=%s reason=%s",
                    tool_name, session_id, break_result.reason.name,
                )
                raise ValueError(break_result.format_message())

            # 2. Execute
            raw_output = await fn(**kwargs)

            # 3. Optimize output
            return await optimizer.process(
                raw_output = raw_output,
                tool_name  = tool_name,
                session_id = session_id,
            )

        return wrapper
    return decorator
