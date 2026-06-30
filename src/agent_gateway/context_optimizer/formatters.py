"""
context_optimizer/formatters.py — Extract a head excerpt + metadata summary
from a tool result for each content type.

Each formatter returns (head_str, meta_summary) where:
  head_str     : first max_lines lines/items, ready to embed in truncation message
  meta_summary : one-line description of the full content structure
"""
from __future__ import annotations

import json


def format_json(raw_str: str, max_lines: int) -> tuple[str, str]:
    """
    Parse and pretty-print JSON, returning the first max_lines lines.
    meta_summary describes the root type and key count / array length.
    """
    try:
        data   = json.loads(raw_str)
        pretty = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except (json.JSONDecodeError, ValueError):
        # Fallback: treat as plaintext
        return format_plaintext(raw_str, max_lines)

    lines = pretty.splitlines()
    head  = "\n".join(lines[:max_lines])

    if isinstance(data, dict):
        keys = list(data.keys())
        meta = (
            f"JSON Object | {len(keys)} keys | "
            f"root keys: {keys[:10]}{'...' if len(keys) > 10 else ''}"
        )
    elif isinstance(data, list):
        item_type = type(data[0]).__name__ if data else "empty"
        meta = f"JSON Array | {len(data)} items | item type: {item_type}"
    else:
        meta = f"JSON Primitive | type: {type(data).__name__}"

    return head, meta


def format_structured(data: dict | list, max_lines: int) -> tuple[str, str]:
    """Native Python dict/list — serialize to JSON then delegate."""
    serialized = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    return format_json(serialized, max_lines)


def format_plaintext(text: str, max_lines: int) -> tuple[str, str]:
    """Plain text — return first max_lines lines."""
    lines = text.splitlines()
    head  = "\n".join(lines[:max_lines])
    meta  = f"Plain text | {len(lines)} lines | {len(text.encode('utf-8'))} bytes"
    return head, meta


def format_binary(data: bytes, max_lines: int) -> tuple[str, str]:
    """Binary data — return hex preview of first 256 bytes."""
    preview_bytes = data[:256]
    preview       = " ".join(f"{b:02x}" for b in preview_bytes)
    meta          = f"Binary | {len(data):,} bytes"
    head          = f"[hex preview — first {len(preview_bytes)} bytes]\n{preview}"
    return head, meta
