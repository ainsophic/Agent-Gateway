"""
loop_arbiter/state.py — Per-session mutable state tracked by the LoopArbiter.

AgentState is created once per session and mutated on every tool call.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field

from agent_gateway.config import ArbiterConfig
from agent_gateway.types import SessionId, ToolName, ArgsHash


# ── Hashing ───────────────────────────────────────────────────────────────────

def hash_args(args: dict) -> ArgsHash:
    """
    Stable, order-independent 16-char hex hash of tool arguments.

    Properties:
    - sort_keys=True  : {"a":1,"b":2} == {"b":2,"a":1}
    - default=str     : handles Path, datetime, and other non-serializable types
    - ensure_ascii=False: preserves Unicode in hash input
    """
    canonical = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ── Records ───────────────────────────────────────────────────────────────────

@dataclass
class ToolCallRecord:
    tool_name:  ToolName
    args_hash:  ArgsHash
    timestamp:  float   # time.monotonic()
    call_index: int     # global monotonic counter within the session


# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class AgentState:
    """
    Mutable state for one agent session.

    Mutated by LoopArbiter.check() on every tool call.
    Reset only when the session is destroyed.
    """
    session_id: SessionId
    config:     ArbiterConfig

    # Populated in __post_init__ because deque needs maxlen from config
    history: deque[ToolCallRecord] = field(init=False)

    # Consecutive call tracking
    # key = (tool_name, args_hash) → consecutive call count
    # Reset to 0 when a different key is observed
    repeat_counters: dict[tuple[ToolName, ArgsHash], int] = field(
        default_factory=dict
    )
    last_key: tuple[ToolName, ArgsHash] | None = None

    # Global counters
    total_calls: int   = 0
    started_at:  float = field(default_factory=time.monotonic)

    # Circuit breaker state
    circuit_broken: bool       = False
    break_reason:   str | None = None

    def __post_init__(self) -> None:
        self.history: deque[ToolCallRecord] = deque(
            maxlen=self.config.history_maxlen
        )

    def record(self, tool_name: ToolName, args_hash: ArgsHash) -> ToolCallRecord:
        """
        Append the current tool call to history and increment total_calls.
        Called AFTER exact-repeat check, BEFORE rate and oscillation checks.
        """
        rec = ToolCallRecord(
            tool_name  = tool_name,
            args_hash  = args_hash,
            timestamp  = time.monotonic(),
            call_index = self.total_calls,
        )
        self.history.append(rec)
        self.total_calls += 1
        return rec
