"""
types.py — Shared types, enums, and dataclasses.

No imports from within the package. All other modules import from here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TypeAlias

# ── Type aliases ──────────────────────────────────────────────────────────────

SessionId: TypeAlias = str
ToolName:  TypeAlias = str
ArgsHash:  TypeAlias = str   # 16-char hex prefix of SHA-256
RefId:     TypeAlias = str   # "{tool_prefix}_{uuid8}" e.g. "readfile_a3f2b891"

# ── Enums ─────────────────────────────────────────────────────────────────────

class ContentType(Enum):
    JSON       = "json"
    STRUCTURED = "structured"   # native dict/list (not a JSON string)
    PLAINTEXT  = "plaintext"
    BINARY     = "binary"


class Language(Enum):
    PYTHON     = "python"
    BASH       = "bash"
    JAVASCRIPT = "javascript"


class LoopReason(Enum):
    EXACT_REPEAT    = auto()
    BUDGET_EXCEEDED = auto()
    RATE_SPIKE      = auto()
    OSCILLATION     = auto()


class GatewayError(Enum):
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
    TIMEOUT             = "TIMEOUT"
    FILE_TOO_LARGE      = "FILE_TOO_LARGE"
    INVALID_PATH        = "INVALID_PATH"
    STORE_ERROR         = "STORE_ERROR"

# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class CircuitBreakResult:
    reason:     LoopReason
    tool_name:  ToolName
    args_hash:  ArgsHash
    call_count: int
    message:    str

    def format_message(self) -> str:
        return (
            f"[GATEWAY CIRCUIT BREAKER — {self.reason.name}]\n"
            f"{self.message}\n"
            f"Tool: {self.tool_name} | Calls in session: {self.call_count}"
        )


@dataclass
class ExecutionResult:
    stdout:            str
    stderr:            str
    exit_code:         int
    execution_time_ms: int
    timed_out:         bool
    truncated:         bool   # stdout+stderr were cut at exec_max_output_bytes
    sandbox_id:        str

    def to_tool_output(self) -> str:
        """Format result as a structured string for Claude tool_result content."""
        parts: list[str] = []

        if self.timed_out:
            parts.append("⚠️  TIMEOUT: execution interrupted by time limit")

        if self.stdout.strip():
            parts.append(f"STDOUT:\n{self.stdout}")

        if self.stderr.strip():
            parts.append(f"STDERR:\n{self.stderr}")

        parts.append(
            f"exit_code={self.exit_code} | "
            f"time={self.execution_time_ms}ms | "
            f"sandbox={self.sandbox_id}"
        )

        if self.truncated:
            parts.append("[Output truncated — use fetch_fragment() for the rest]")

        return "\n---\n".join(parts)


# ── Exceptions ────────────────────────────────────────────────────────────────

class GatewayException(Exception):
    """Raised by SecureExecutorGateway for controllable failure modes."""

    def __init__(
        self,
        error:      GatewayError,
        detail:     str,
        session_id: SessionId | None = None,
    ) -> None:
        self.error      = error
        self.detail     = detail
        self.session_id = session_id
        super().__init__(f"[{error.value}] {detail}")
