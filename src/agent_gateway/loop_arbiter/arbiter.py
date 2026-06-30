"""
loop_arbiter/arbiter.py — LoopArbiter orchestrates all four detectors.

Call order per check():
  1. check_budget        — absolute limit, cheapest, before any state mutation
  2. check_exact_repeat  — updates repeat_counters/last_key, before history record
  3. state.record()      — append to history, increment total_calls
  4. check_rate_spike    — reads history (current call now included)
  5. check_oscillation   — reads history (current call now included)

Once the circuit is tripped, all subsequent calls are blocked immediately.
"""
from __future__ import annotations

import logging

from agent_gateway.config import ArbiterConfig
from agent_gateway.types import CircuitBreakResult, LoopReason, ToolName
from agent_gateway.loop_arbiter.state import AgentState, hash_args
from agent_gateway.loop_arbiter import detectors

log = logging.getLogger(__name__)


class LoopArbiter:
    def __init__(self, config: ArbiterConfig) -> None:
        self.config = config

    def check(
        self,
        tool_name: ToolName,
        args:      dict,
        state:     AgentState,
    ) -> CircuitBreakResult | None:
        """
        Main entrypoint. Called BEFORE tool execution.

        Returns CircuitBreakResult on first triggered detector, else None.
        Caller must raise ToolError(result.format_message()) when result is not None.
        """
        # Once tripped, block everything without touching state
        if state.circuit_broken:
            return CircuitBreakResult(
                reason     = LoopReason.BUDGET_EXCEEDED,
                tool_name  = tool_name,
                args_hash  = "",
                call_count = state.total_calls,
                message    = (
                    "Circuit breaker is active for this session. "
                    "No further tool calls are permitted. "
                    "Request human intervention to reset the session."
                ),
            )

        # 1. Budget check (reads total_calls, no mutation)
        if result := detectors.check_budget(state, self.config):
            self._trip(state, result)
            return result

        args_hash = hash_args(args)
        key       = (tool_name, args_hash)

        # 2. Exact repeat (mutates repeat_counters + last_key, NOT history)
        if result := detectors.check_exact_repeat(state, key, self.config):
            self._trip(state, result)
            return result

        # Record to history — now total_calls is incremented
        state.record(tool_name, args_hash)

        # 3. Rate spike (reads history, which now includes current call)
        if result := detectors.check_rate_spike(state, self.config):
            self._trip(state, result)
            return result

        # 4. Oscillation (most expensive, runs last)
        if result := detectors.check_oscillation(state, self.config):
            self._trip(state, result)
            return result

        return None

    def _trip(self, state: AgentState, result: CircuitBreakResult) -> None:
        state.circuit_broken = True
        state.break_reason   = result.reason.name
        log.warning(
            "[LoopArbiter] Circuit tripped — session=%s reason=%s tool=%s calls=%d",
            state.session_id,
            result.reason.name,
            result.tool_name,
            result.call_count,
        )
