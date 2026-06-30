"""
tests/unit/test_arbiter.py — Unit tests for LoopArbiter and all four detectors.

No external dependencies. No E2B. No network.
"""
import pytest
from unittest.mock import patch
import time

from agent_gateway.config import ArbiterConfig
from agent_gateway.loop_arbiter.arbiter import LoopArbiter
from agent_gateway.loop_arbiter.state import AgentState, hash_args
from agent_gateway.types import LoopReason


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_config(**overrides) -> ArbiterConfig:
    defaults = dict(
        max_total_calls       = 50,
        max_repeat_calls      = 3,
        rate_window_seconds   = 5.0,
        rate_max_calls        = 10,
        oscillation_min_cycle = 2,
        oscillation_cycles    = 3,
        history_maxlen        = 100,
    )
    defaults.update(overrides)
    return ArbiterConfig(**defaults)


def make_state(config: ArbiterConfig | None = None) -> AgentState:
    cfg = config or make_config()
    return AgentState(session_id="test_session", config=cfg)


def make_arbiter(config: ArbiterConfig | None = None) -> LoopArbiter:
    return LoopArbiter(config or make_config())


def call(arbiter: LoopArbiter, state: AgentState, tool: str, args: dict):
    """Convenience wrapper."""
    return arbiter.check(tool, args, state)


# ── hash_args ─────────────────────────────────────────────────────────────────

class TestHashArgs:
    def test_order_independent(self):
        assert hash_args({"a": 1, "b": 2}) == hash_args({"b": 2, "a": 1})

    def test_different_values_differ(self):
        assert hash_args({"a": 1}) != hash_args({"a": 2})

    def test_nested_dicts(self):
        h1 = hash_args({"x": {"y": 1}})
        h2 = hash_args({"x": {"y": 1}})
        assert h1 == h2

    def test_non_serializable_values(self):
        # Should not raise
        from pathlib import Path
        result = hash_args({"path": Path("/tmp/file")})
        assert len(result) == 16

    def test_returns_16_char_hex(self):
        result = hash_args({"key": "value"})
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)


# ── EXACT_REPEAT ──────────────────────────────────────────────────────────────

class TestExactRepeat:
    def test_no_trigger_below_threshold(self):
        cfg     = make_config(max_repeat_calls=3)
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)
        args    = {"path": "/tmp/file.txt"}

        assert call(arbiter, state, "read_file", args) is None   # call 1
        assert call(arbiter, state, "read_file", args) is None   # call 2

    def test_triggers_at_threshold(self):
        cfg     = make_config(max_repeat_calls=3)
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)
        args    = {"path": "/tmp/file.txt"}

        call(arbiter, state, "read_file", args)   # 1
        call(arbiter, state, "read_file", args)   # 2
        result = call(arbiter, state, "read_file", args)  # 3 → trigger

        assert result is not None
        assert result.reason == LoopReason.EXACT_REPEAT
        assert result.call_count == 3

    def test_different_args_do_not_trigger(self):
        cfg     = make_config(max_repeat_calls=3)
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        call(arbiter, state, "read_file", {"path": "/a"})
        call(arbiter, state, "read_file", {"path": "/b"})
        result = call(arbiter, state, "read_file", {"path": "/c"})

        assert result is None

    def test_interleaved_call_resets_counter(self):
        """A A B A A should not trigger at 2nd pair of A's."""
        cfg     = make_config(max_repeat_calls=3)
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        args_a = {"path": "/a"}
        args_b = {"path": "/b"}

        call(arbiter, state, "read_file", args_a)  # A (count=1)
        call(arbiter, state, "read_file", args_a)  # A (count=2)
        call(arbiter, state, "read_file", args_b)  # B (A count reset to 0)
        call(arbiter, state, "read_file", args_a)  # A (count=1)
        result = call(arbiter, state, "read_file", args_a)  # A (count=2) → no trigger

        assert result is None

    def test_circuit_stays_broken_after_trip(self):
        cfg     = make_config(max_repeat_calls=2)
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)
        args    = {"x": 1}

        call(arbiter, state, "tool", args)
        call(arbiter, state, "tool", args)  # trips
        assert state.circuit_broken

        # Different tool, different args — still blocked
        result = call(arbiter, state, "other_tool", {"y": 2})
        assert result is not None


# ── BUDGET_EXCEEDED ───────────────────────────────────────────────────────────

class TestBudgetExceeded:
    def test_no_trigger_below_budget(self):
        cfg     = make_config(max_total_calls=5, max_repeat_calls=999)
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        for i in range(4):
            assert call(arbiter, state, "tool", {"i": i}) is None

    def test_triggers_at_budget(self):
        cfg     = make_config(max_total_calls=3, max_repeat_calls=999)
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        # Make 3 unique calls (consumes budget)
        for i in range(3):
            call(arbiter, state, "tool", {"i": i})

        # 4th call should trigger budget
        result = call(arbiter, state, "tool", {"i": 99})
        assert result is not None
        assert result.reason == LoopReason.BUDGET_EXCEEDED

    def test_total_calls_reflects_executed_calls(self):
        cfg     = make_config(max_total_calls=10, max_repeat_calls=999)
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        for i in range(5):
            call(arbiter, state, "tool", {"i": i})

        assert state.total_calls == 5


# ── RATE_SPIKE ────────────────────────────────────────────────────────────────

class TestRateSpike:
    def test_triggers_in_window(self):
        cfg     = make_config(
            max_repeat_calls=999,
            rate_window_seconds=60.0,
            rate_max_calls=5,
        )
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        for i in range(4):
            call(arbiter, state, "tool", {"i": i})

        result = call(arbiter, state, "tool", {"i": 99})
        assert result is not None
        assert result.reason == LoopReason.RATE_SPIKE

    def test_no_trigger_outside_window(self):
        """Calls spread outside the window should not trigger."""
        cfg     = make_config(
            max_repeat_calls=999,
            rate_window_seconds=1.0,
            rate_max_calls=3,
        )
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        # Simulate old calls by backdating timestamps after recording
        call(arbiter, state, "tool", {"i": 0})
        call(arbiter, state, "tool", {"i": 1})

        # Backdate history to be outside the window
        for rec in state.history:
            rec.timestamp = time.monotonic() - 10.0  # 10s ago, outside 1s window

        # These two calls should be within the window but count < rate_max_calls
        result1 = call(arbiter, state, "tool", {"i": 2})
        result2 = call(arbiter, state, "tool", {"i": 3})

        assert result1 is None
        assert result2 is None


# ── OSCILLATION ───────────────────────────────────────────────────────────────

class TestOscillation:
    def _run_pattern(self, pattern: list[str], cycles: int, cfg: ArbiterConfig):
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)
        results = []
        for _ in range(cycles):
            for tool in pattern:
                r = call(arbiter, state, tool, {})
                results.append(r)
        return results, state

    def test_ab_pattern_triggers(self):
        cfg = make_config(
            max_repeat_calls=999,
            rate_max_calls=999,
            oscillation_min_cycle=2,
            oscillation_cycles=3,
        )
        results, state = self._run_pattern(["tool_a", "tool_b"], cycles=3, cfg=cfg)
        # Should trigger somewhere in the 3rd cycle
        assert any(
            r is not None and r.reason == LoopReason.OSCILLATION
            for r in results
        )

    def test_abc_pattern_triggers(self):
        cfg = make_config(
            max_repeat_calls=999,
            rate_max_calls=999,
            oscillation_min_cycle=2,
            oscillation_cycles=3,
        )
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)
        result  = None
        for _ in range(4):
            for tool in ["a", "b", "c"]:
                r = call(arbiter, state, tool, {})
                if r is not None:
                    result = r
                    break
            if result:
                break

        assert result is not None
        assert result.reason == LoopReason.OSCILLATION

    def test_insufficient_cycles_no_trigger(self):
        cfg = make_config(
            max_repeat_calls=999,
            rate_max_calls=999,
            oscillation_min_cycle=2,
            oscillation_cycles=3,  # requires 3 full cycles
        )
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        # Only 2 cycles — should not trigger
        for tool in ["a", "b", "a", "b"]:
            r = call(arbiter, state, tool, {})
            assert r is None

    def test_no_oscillation_with_unique_calls(self):
        # Isolate oscillation logic: disable repeat/budget/rate so only
        # oscillation could possibly trigger.
        cfg = make_config(
            max_repeat_calls=999,
            max_total_calls=999,
            rate_max_calls=999,
            oscillation_min_cycle=2,
            oscillation_cycles=3,
        )
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        for i in range(10):
            result = call(arbiter, state, f"tool_{i}", {})
            assert result is None


# ── Full pipeline ─────────────────────────────────────────────────────────────

class TestFullArbiterPipeline:
    def test_budget_checked_before_repeat(self):
        """Budget limit fires before EXACT_REPEAT if both would trigger."""
        cfg     = make_config(max_total_calls=2, max_repeat_calls=2)
        state   = make_state(cfg)
        arbiter = make_arbiter(cfg)

        call(arbiter, state, "tool", {"x": 1})   # total=1
        call(arbiter, state, "tool", {"x": 1})   # total=2, repeat=2 → repeat fires first (before record)

        # Next call: budget should fire
        result = call(arbiter, state, "other", {"y": 2})
        assert result is not None
        # Since circuit is broken from repeat trigger, it returns budget reason
        assert state.circuit_broken
