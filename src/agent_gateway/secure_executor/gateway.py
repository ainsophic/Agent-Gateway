"""
secure_executor/gateway.py — Routes all code/shell execution through E2B microVMs.

Contract:
  - NEVER executes code on the host process.
  - Raises GatewayException on E2B unavailability — no local fallback.
  - Truncates stdout/stderr at exec_max_output_bytes.
  - Validates file paths (must start with /home/user/ or /tmp/).

Public API:
  execute_code(code, sandbox_manager, language?, timeout?) → ExecutionResult
  execute_shell(command, sandbox_manager, timeout?)        → ExecutionResult
  upload_file(sandbox_manager, content, sandbox_path)     → None
  download_file(sandbox_manager, sandbox_path)            → bytes
"""
from __future__ import annotations

import logging
import time

from agent_gateway.config import GatewayConfig
from agent_gateway.types import (
    ExecutionResult,
    GatewayError,
    GatewayException,
    Language,
    SessionId,
)
from agent_gateway.secure_executor.sandbox_manager import SandboxManager
from agent_gateway.secure_executor.language_detector import detect_language

log = logging.getLogger(__name__)

_ALLOWED_PATH_PREFIXES = ("/home/user/", "/tmp/")


class SecureExecutorGateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config

    async def execute_code(
        self,
        code:            str,
        sandbox_manager: SandboxManager,
        language:        Language | None = None,
        timeout:         int | None      = None,
    ) -> ExecutionResult:
        """
        Execute code in the session's E2B sandbox.

        Language is auto-detected if not specified.
        Timeout defaults to GatewayConfig.sandbox_timeout_seconds.
        """
        lang    = language or detect_language(code)
        timeout = timeout  or self.config.sandbox_timeout_seconds
        sandbox = await sandbox_manager.get()

        log.debug(
            "[Gateway] execute_code lang=%s timeout=%ds sandbox=%s",
            lang.value, timeout, sandbox.id,
        )

        t_start = time.monotonic()
        try:
            execution = await sandbox.run_code(
                code,
                language=lang.value,
                timeout=timeout,
            )
        except TimeoutError:
            elapsed = int((time.monotonic() - t_start) * 1000)
            log.warning("[Gateway] Execution timed out after %dms", elapsed)
            return ExecutionResult(
                stdout            = "",
                stderr            = "Execution timed out.",
                exit_code         = -1,
                execution_time_ms = elapsed,
                timed_out         = True,
                truncated         = False,
                sandbox_id        = sandbox.id,
            )
        except Exception as exc:
            raise GatewayException(
                error      = GatewayError.SANDBOX_UNAVAILABLE,
                detail     = f"Sandbox execution error: {exc}",
                session_id = sandbox_manager._session_id,
            ) from exc

        elapsed_ms = int((time.monotonic() - t_start) * 1000)

        raw_stdout = "\n".join(execution.logs.stdout) if execution.logs.stdout else ""
        raw_stderr = "\n".join(execution.logs.stderr) if execution.logs.stderr else ""

        stdout, trunc_out = self._truncate(raw_stdout)
        stderr, trunc_err = self._truncate(raw_stderr)

        return ExecutionResult(
            stdout            = stdout,
            stderr            = stderr,
            exit_code         = 1 if execution.error else 0,
            execution_time_ms = elapsed_ms,
            timed_out         = False,
            truncated         = trunc_out or trunc_err,
            sandbox_id        = sandbox.id,
        )

    async def execute_shell(
        self,
        command:         str,
        sandbox_manager: SandboxManager,
        timeout:         int | None = None,
    ) -> ExecutionResult:
        """
        Execute a shell command by wrapping it as a bash script.
        Delegates to execute_code with Language.BASH.
        """
        script = f"#!/bin/bash\nset -euo pipefail\n{command}"
        return await self.execute_code(
            code            = script,
            sandbox_manager = sandbox_manager,
            language        = Language.BASH,
            timeout         = timeout,
        )

    async def upload_file(
        self,
        sandbox_manager: SandboxManager,
        content:         bytes,
        sandbox_path:    str,
    ) -> None:
        """
        Upload bytes to the sandbox filesystem.

        Raises:
            GatewayException(FILE_TOO_LARGE) if content exceeds max_file_bytes.
            GatewayException(INVALID_PATH)   if sandbox_path is outside allowed prefixes.
        """
        self._validate_path(sandbox_path)

        if len(content) > self.config.max_file_bytes:
            raise GatewayException(
                error  = GatewayError.FILE_TOO_LARGE,
                detail = (
                    f"File size {len(content):,} bytes exceeds limit "
                    f"{self.config.max_file_bytes:,} bytes"
                ),
            )

        sandbox = await sandbox_manager.get()
        await sandbox.files.write(sandbox_path, content)
        log.debug("[Gateway] Uploaded %d bytes to %s", len(content), sandbox_path)

    async def download_file(
        self,
        sandbox_manager: SandboxManager,
        sandbox_path:    str,
    ) -> bytes:
        """
        Download bytes from the sandbox filesystem.

        Raises:
            GatewayException(INVALID_PATH) if sandbox_path is outside allowed prefixes.
        """
        self._validate_path(sandbox_path)
        sandbox = await sandbox_manager.get()
        content = await sandbox.files.read(sandbox_path)
        log.debug("[Gateway] Downloaded %d bytes from %s", len(content), sandbox_path)
        return content

    # ── Private ──────────────────────────────────────────────────────────────

    def _truncate(self, text: str) -> tuple[str, bool]:
        """Cut text at exec_max_output_bytes. Returns (text, was_truncated)."""
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= self.config.exec_max_output_bytes:
            return text, False
        cut = encoded[: self.config.exec_max_output_bytes]
        return cut.decode("utf-8", errors="replace") + "\n[... truncated ...]", True

    def _validate_path(self, path: str) -> None:
        if not any(path.startswith(p) for p in _ALLOWED_PATH_PREFIXES):
            raise GatewayException(
                error  = GatewayError.INVALID_PATH,
                detail = (
                    f"sandbox_path must start with one of {_ALLOWED_PATH_PREFIXES}. "
                    f"Got: '{path}'"
                ),
            )
