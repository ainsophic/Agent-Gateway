"""
context_optimizer/detector.py — Detect the content type of a raw tool output.

Priority order:
  1. bytes          → BINARY
  2. dict | list    → STRUCTURED (native Python, not a JSON string)
  3. str that parses as JSON → JSON
  4. anything else  → PLAINTEXT (coerced to str)
"""
from __future__ import annotations

import json

from agent_gateway.types import ContentType


def detect_content_type(raw: object) -> ContentType:
    if isinstance(raw, (bytes, bytearray)):
        return ContentType.BINARY

    if isinstance(raw, (dict, list)):
        return ContentType.STRUCTURED

    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped and stripped[0] in ("{", "[", '"'):
            try:
                json.loads(stripped)
                return ContentType.JSON
            except (json.JSONDecodeError, ValueError):
                pass
        return ContentType.PLAINTEXT

    # int, float, bool, None, custom objects → stringify as plaintext
    return ContentType.PLAINTEXT
