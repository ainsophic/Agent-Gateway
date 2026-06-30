"""
secure_executor/sandbox_manager.py — Manages one E2B sandbox per agent session.

Strategy: session-scoped
  - One sandbox is created lazily on first use within a session.
  - It is reused across all tool calls in that session (files persist).
  - It is replaced automatically if it exceeds max_sandbox_age_seconds.
  - It is destroyed when teardown() is called (session ends).

IMPORTANT: If E2B is unavailable, raises GatewayException with
SANDBOX_UNAVAILABLE. Never falls back to local execution.
"""
from __future__ import annotations

import logging
import time

from agent_gateway.config import GatewayConfig
from agent_gateway.types import GatewayError, GatewayException, SessionId

log = logging.getLogger(__name__)


class SandboxManager:
    def __init__(self, session_id: SessionId, config: GatewayConfig) -> None:
        self._session_id  = session_id
        self._config      = config
        self._sandbox     = None
        self._created_at: float | None = None

    async def get(self):
        """
        Return the active sandbox for this session.
        Creates or refreshes the sandbox as needed.

        Raises:
            GatewayException(SANDBOX_UNAVAILABLE) if E2B is unreachable.
        """
        if self._sandbox is None or self._is_stale():
            await self.teardown()
            await self._create()
        return self._sandbox

    async def teardown(self) -> None:
        """
        Kill the sandbox. Best-effort — never raises.
        Call when the session ends or to force a clean environment.
        """
        if self._sandbox is not None:
            try:
                await self._sandbox.kill()
                log.info(
                    "[SandboxManager] session=%s killed sandbox=%s",
                    self._session_id, self._sandbox.id,
                )
            except Exception as exc:
                log.warning(
                    "[SandboxManager] session=%s kill failed (ignored): %s",
                    self._session_id, exc,
                )
            finally:
                self._sandbox    = None
                self._created_at = None

    # ── Private ──────────────────────────────────────────────────────────────

    def _is_stale(self) -> bool:
        if self._created_at is None:
            return True
        return (time.monotonic() - self._created_at) > self._config.max_sandbox_age_seconds

    async def _create(self) -> None:
        try:
            from e2b_code_interpreter import AsyncSandbox  # lazy import for testability

            self._sandbox    = await AsyncSandbox.create(
                timeout=self._config.sandbox_timeout_seconds
            )
            self._created_at = time.monotonic()

            log.info(
                "[SandboxManager] session=%s created sandbox=%s",
                self._session_id, self._sandbox.id,
            )
        except ImportError:
            raise GatewayException(
                error      = GatewayError.SANDBOX_UNAVAILABLE,
                detail     = "e2b-code-interpreter is not installed. Run: uv pip install e2b-code-interpreter",
                session_id = self._session_id,
            )
        except Exception as exc:
            raise GatewayException(
                error      = GatewayError.SANDBOX_UNAVAILABLE,
                detail     = f"E2B sandbox creation failed: {exc}",
                session_id = self._session_id,
            ) from exc
