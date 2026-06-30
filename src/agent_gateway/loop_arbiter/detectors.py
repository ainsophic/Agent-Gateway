"""
loop_arbiter/detectors.py — Four independent loop detection algorithms.

Each detector is a pure function: takes state + config, returns
CircuitBreakResult if triggered, else None.

Detection order in arbiter.py:
  1. check_budget        (cheapest, absolute limit)
  2. check_exact_repeat  (O(1), updates state.repeat_counters)
  3. check_rate_spike    (O(window_size))
  4. check_oscillation   (most expensive, runs last)
"""
from __future__ import annotations

import time

from agent_gateway.config import ArbiterConfig
from agent_gateway.types import (
    ArgsHash,
    CircuitBreakResult,
    LoopReason,
    ToolName,
)
from agent_gateway.loop_arbiter.state import AgentState


# ── 1. BUDGET_EXCEEDED ────────────────────────────────────────────────────────

def check_budget(
    state:  AgentState,
    config: ArbiterConfig,
) -> CircuitBreakResult | None:
    """
    Fires when total_calls for this session reaches max_total_calls.

    Checked BEFORE state.record() so total_calls reflects executed calls only.
    Example: max=50 → fires on the 51st attempt (total_calls == 50 after 50 recorded calls).
    """
    if state.total_calls >= config.max_total_calls:
        return CircuitBreakResult(
            reason     = LoopReason.BUDGET_EXCEEDED,
            tool_name  = "any",
            args_hash  = "",
            call_count = state.total_calls,
            message    = (
                f"Session tool call budget exhausted "
                f"({state.total_calls}/{config.max_total_calls}).\n"
                "Summarize what has been accomplished and request human "
                "intervention to continue or adjust the task scope."
            ),
        )
    return None


# ── 2. EXACT_REPEAT ───────────────────────────────────────────────────────────

def check_exact_repeat(
    state:  AgentState,
    key:    tuple[ToolName, ArgsHash],
    config: ArbiterConfig,
) -> CircuitBreakResult | None:
    """
    Detects consecutive identical calls: same (tool_name, args_hash) N times.

    Counter resets to 1 when a different key is observed.

    Side effects on state:
      - state.repeat_counters[key] is incremented
      - state.repeat_counters[prev_key] is reset to 0 on key change
      - state.last_key is updated

    Called BEFORE state.record() — this call is not yet in history.
    """
    if key == state.last_key:
        # Same key as previous call → increment consecutive counter
        state.repeat_counters[key] = state.repeat_counters.get(key, 1) + 1
    else:
        # New key → reset previous key's consecutive counter
        if state.last_key is not None:
            state.repeat_counters[state.last_key] = 0
        state.last_key = key
        # Preserve non-consecutive count but reset consecutive window
        state.repeat_counters.setdefault(key, 0)
        state.repeat_counters[key] += 1

    count = state.repeat_counters[key]
    if count >= config.max_repeat_calls:
        tool_name, args_hash = key
        return CircuitBreakResult(
            reason     = LoopReason.EXACT_REPEAT,
            tool_name  = tool_name,
            args_hash  = args_hash,
            call_count = count,
            message    = (
                f"Tool '{tool_name}' was called {count} consecutive times "
                f"with identical arguments (hash: {args_hash}).\n"
                "This indicates a stuck loop. Options:\n"
                "  1. Check if the tool returned an error you are not handling.\n"
                "  2. Try a different tool or modify the arguments.\n"
                "  3. Request human intervention if blocked."
            ),
        )
    return None


# ── 3. RATE_SPIKE ─────────────────────────────────────────────────────────────

def check_rate_spike(
    state:  AgentState,
    config: ArbiterConfig,
) -> CircuitBreakResult | None:
    """
    Detects burst calling: ≥ rate_max_calls in the last rate_window_seconds.

    Uses a sliding window over state.history (which includes the current call
    because state.record() is called before this detector).

    Uses monotonic clock — immune to wall-clock adjustments.
    """
    now          = time.monotonic()
    window_start = now - config.rate_window_seconds
    recent       = [r for r in state.history if r.timestamp >= window_start]

    if len(recent) >= config.rate_max_calls:
        last = recent[-1]
        return CircuitBreakResult(
            reason     = LoopReason.RATE_SPIKE,
            tool_name  = last.tool_name,
            args_hash  = last.args_hash,
            call_count = len(recent),
            message    = (
                f"{len(recent)} tool calls in the last "
                f"{config.rate_window_seconds:.1f}s "
                f"(limit: {config.rate_max_calls}).\n"
                "Slow down: analyze intermediate results before issuing more calls."
            ),
        )
    return None


# ── 4. OSCILLATION ────────────────────────────────────────────────────────────

def check_oscillation(
    state:  AgentState,
    config: ArbiterConfig,
) -> CircuitBreakResult | None:
    """
    Detects repeating patterns: A→B→A→B→A→B, A→B→C→A→B→C→A→B→C, etc.

    Algorithm:
      For each candidate cycle length L in [2, oscillation_min_cycle*2]:
        Extract the last (L * oscillation_cycles) calls as a list of keys.
        If every consecutive window of size L equals the most recent window
        → oscillation confirmed.

    Example (L=2, cycles=3):
      history tail: [A, B, A, B, A, B]
      pattern     : [A, B]
      window[-2:] = [A, B] ✓
      window[-4:-2] = [A, B] ✓
      window[-6:-4] = [A, B] ✓  → confirmed

    Requires history to have at least (L * cycles) entries.
    Called AFTER state.record() — current call is in history.
    """
    h    = list(state.history)
    keys = [(r.tool_name, r.args_hash) for r in h]

    max_cycle = config.oscillation_min_cycle * 2

    for cycle_len in range(2, max_cycle + 1):
        required = cycle_len * config.oscillation_cycles
        if len(keys) < required:
            continue

        # The pattern is the most recent cycle_len keys
        pattern = keys[-cycle_len:]

        # Check that pattern repeats oscillation_cycles times consecutively
        confirmed = True
        for i in range(1, config.oscillation_cycles):
            end   = len(keys) - cycle_len * i
            start = end - cycle_len
            if start < 0 or keys[start:end] != pattern:
                confirmed = False
                break

        if confirmed:
            names = [k[0] for k in pattern]
            return CircuitBreakResult(
                reason     = LoopReason.OSCILLATION,
                tool_name  = names[0],
                args_hash  = pattern[0][1],
                call_count = required,
                message    = (
                    f"Oscillation detected: [{' → '.join(names)}] repeated "
                    f"{config.oscillation_cycles} times.\n"
                    "The agent is cycling between states without progress.\n"
                    "Re-evaluate your strategy or request human intervention."
                ),
            )
    return None
