"""
session.py — SessionContext groups all per-session objects.
             SessionRegistry manages the active session map.

One SessionContext is created per unique session_id (= one Claude conversation).
It holds:
  - state           : LoopArbiter's mutable call history
  - sandbox_manager : E2B sandbox lifecycle for this session
"""
from __future__ import annotations

import logging

from agent_gateway.config import ArbiterConfig, GatewayConfig
from agent_gateway.types import SessionId
from agent_gateway.loop_arbiter.state import AgentState
from agent_gateway.secure_executor.sandbox_manager import SandboxManager

log = logging.getLogger(__name__)


class SessionContext:
    """All per-session mutable state in one container."""

    def __init__(
        self,
        session_id:      SessionId,
        state:           AgentState,
        sandbox_manager: SandboxManager,
    ) -> None:
        self.session_id      = session_id
        self.state           = state
        self.sandbox_manager = sandbox_manager


class SessionRegistry:
    """
    Thread-safe dict of active sessions.
    get_or_create() is the only entrypoint — sessions are lazily created.
    teardown() destroys a session's sandbox and removes it from the registry.
    """

    def __init__(
        self,
        arbiter_config: ArbiterConfig,
        gateway_config: GatewayConfig,
    ) -> None:
        self._sessions:      dict[SessionId, SessionContext] = {}
        self._arbiter_config = arbiter_config
        self._gateway_config = gateway_config

    def get_or_create(self, session_id: SessionId) -> SessionContext:
        if session_id not in self._sessions:
            log.info("[SessionRegistry] Creating session=%s", session_id)
            self._sessions[session_id] = SessionContext(
                session_id      = session_id,
                state           = AgentState(
                    session_id = session_id,
                    config     = self._arbiter_config,
                ),
                sandbox_manager = SandboxManager(
                    session_id = session_id,
                    config     = self._gateway_config,
                ),
            )
        return self._sessions[session_id]

    async def teardown(self, session_id: SessionId) -> None:
        """Destroy a session's sandbox and remove it from the registry."""
        if ctx := self._sessions.pop(session_id, None):
            await ctx.sandbox_manager.teardown()
            log.info("[SessionRegistry] Destroyed session=%s", session_id)

    async def teardown_all(self) -> None:
        """Destroy all active sessions. Call on server shutdown."""
        for session_id in list(self._sessions):
            await self.teardown(session_id)
