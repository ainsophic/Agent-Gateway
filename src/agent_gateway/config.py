"""
config.py — Frozen config dataclasses loaded from environment variables.

All defaults are sane for development. Override via .env for production.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


def _float(key: str, default: float) -> float:
    return float(os.environ.get(key, str(default)))


# ── Per-component config ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class OptimizerConfig:
    max_output_bytes:   int = 8_192
    max_output_lines:   int = 100
    result_ttl_seconds: int = 3_600
    fragment_max_limit: int = 100
    db_path:            str = "./data/results.db"


@dataclass(frozen=True)
class ArbiterConfig:
    max_total_calls:       int   = 50
    max_repeat_calls:      int   = 3
    rate_window_seconds:   float = 5.0
    rate_max_calls:        int   = 10
    oscillation_min_cycle: int   = 2
    oscillation_cycles:    int   = 3
    history_maxlen:        int   = 100


@dataclass(frozen=True)
class GatewayConfig:
    sandbox_timeout_seconds: int = 30
    max_sandbox_age_seconds: int = 1_800
    max_file_bytes:          int = 10_485_760   # 10 MB
    exec_max_output_bytes:   int = 65_536       # 64 KB


# ── Loader ────────────────────────────────────────────────────────────────────

def load_config() -> tuple[OptimizerConfig, ArbiterConfig, GatewayConfig]:
    """
    Read all GATEWAY_* env vars and return the three config objects.
    Call once at startup after load_dotenv().
    """
    optimizer = OptimizerConfig(
        max_output_bytes   = _int("GATEWAY_MAX_OUTPUT_BYTES",   8_192),
        max_output_lines   = _int("GATEWAY_MAX_OUTPUT_LINES",   100),
        result_ttl_seconds = _int("GATEWAY_RESULT_TTL_SECONDS", 3_600),
        fragment_max_limit = _int("GATEWAY_FRAGMENT_MAX_LIMIT", 100),
        db_path            = _str("GATEWAY_DB_PATH",            "./data/results.db"),
    )
    arbiter = ArbiterConfig(
        max_total_calls       = _int("GATEWAY_MAX_TOTAL_CALLS",         50),
        max_repeat_calls      = _int("GATEWAY_MAX_REPEAT_CALLS",        3),
        rate_window_seconds   = _float("GATEWAY_RATE_WINDOW_SECONDS",   5.0),
        rate_max_calls        = _int("GATEWAY_RATE_MAX_CALLS",          10),
        oscillation_min_cycle = _int("GATEWAY_OSCILLATION_MIN_CYCLE",   2),
        oscillation_cycles    = _int("GATEWAY_OSCILLATION_CYCLES",      3),
    )
    gateway = GatewayConfig(
        sandbox_timeout_seconds = _int("GATEWAY_SANDBOX_TIMEOUT_SECONDS", 30),
        max_sandbox_age_seconds = _int("GATEWAY_MAX_SANDBOX_AGE_SECONDS", 1_800),
        max_file_bytes          = _int("GATEWAY_MAX_FILE_BYTES",          10_485_760),
        exec_max_output_bytes   = _int("GATEWAY_EXEC_MAX_OUTPUT_BYTES",   65_536),
    )
    return optimizer, arbiter, gateway
