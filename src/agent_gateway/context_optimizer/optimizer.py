"""
context_optimizer/optimizer.py — Intercepts tool outputs after execution.

If the output exceeds configured thresholds, it:
  1. Stores the full content in SQLite with a TTL.
  2. Generates a ref_id Claude can use to retrieve fragments.
  3. Returns a compact summary + head excerpt instead of the full payload.

If the output is within limits, it is returned unchanged (as a string).
"""
from __future__ import annotations

import json
import logging

from agent_gateway.config import OptimizerConfig
from agent_gateway.types import ContentType, RefId, SessionId, ToolName
from agent_gateway.context_optimizer.store import ResultStore, generate_ref_id
from agent_gateway.context_optimizer.detector import detect_content_type
from agent_gateway.context_optimizer import formatters

log = logging.getLogger(__name__)

_TRUNCATION_TEMPLATE = """\
=== RESULTADO TRUNCADO (Context Optimizer) ===
Tipo    : {content_type}
Metadata: {meta}
Total   : {total_lines} líneas | {total_bytes:,} bytes

--- Primeras {shown_lines} líneas ---
{head}
--- Fin de muestra ---

[Ref: {ref_id} | Session: {session_id}]
Usa fetch_fragment(ref_id="{ref_id}", offset={shown_lines}, limit=50) para continuar.\
"""


class ContextOptimizer:
    def __init__(self, config: OptimizerConfig, store: ResultStore) -> None:
        self.config = config
        self.store  = store

    async def process(
        self,
        raw_output: object,
        tool_name:  ToolName,
        session_id: SessionId,
    ) -> str:
        """
        Main entrypoint. Called by middleware AFTER every tool execution.

        Returns either:
          - The original output serialized as a string (within limits)
          - A truncation message with ref_id and retrieval instructions
        """
        content_type = detect_content_type(raw_output)
        serialized   = self._serialize(raw_output, content_type)

        # Measure
        if isinstance(serialized, bytes):
            total_bytes = len(serialized)
            total_lines = 0
        else:
            encoded     = serialized.encode("utf-8", errors="replace")
            total_bytes = len(encoded)
            total_lines = serialized.count("\n") + 1

        # Within threshold → return as-is (no storage cost)
        if (total_bytes <= self.config.max_output_bytes
                and total_lines <= self.config.max_output_lines):
            return serialized if isinstance(serialized, str) else serialized.hex()

        # Over threshold → persist + truncate
        ref_id = generate_ref_id(tool_name)

        raw_bytes = (
            serialized
            if isinstance(serialized, bytes)
            else serialized.encode("utf-8", errors="replace")
        )

        await self.store.save(
            ref_id       = ref_id,
            session_id   = session_id,
            tool_name    = tool_name,
            content_type = content_type,
            raw_content  = raw_bytes,
            total_lines  = total_lines,
            ttl_seconds  = self.config.result_ttl_seconds,
        )

        head, meta  = self._format_head(raw_output, content_type)
        shown_lines = min(self.config.max_output_lines, total_lines)

        log.info(
            "[ContextOptimizer] Truncated tool=%s bytes=%d lines=%d ref_id=%s",
            tool_name, total_bytes, total_lines, ref_id,
        )

        return _TRUNCATION_TEMPLATE.format(
            content_type = content_type.value,
            meta         = meta,
            total_lines  = total_lines,
            total_bytes  = total_bytes,
            shown_lines  = shown_lines,
            head         = head,
            ref_id       = ref_id,
            session_id   = session_id,
        )

    # ── Private ──────────────────────────────────────────────────────────────

    def _serialize(self, raw: object, ct: ContentType) -> str | bytes:
        """Convert raw tool output to a canonical storable form."""
        match ct:
            case ContentType.BINARY:
                return raw if isinstance(raw, bytes) else bytes(raw)
            case ContentType.STRUCTURED:
                return json.dumps(raw, indent=2, default=str, ensure_ascii=False)
            case ContentType.JSON:
                # Re-serialize to normalize whitespace
                try:
                    return json.dumps(
                        json.loads(str(raw)), indent=2,
                        default=str, ensure_ascii=False,
                    )
                except (json.JSONDecodeError, ValueError):
                    return str(raw)
            case _:
                return str(raw)

    def _format_head(
        self,
        raw: object,
        ct:  ContentType,
    ) -> tuple[str, str]:
        n = self.config.max_output_lines
        match ct:
            case ContentType.JSON:
                return formatters.format_json(str(raw), n)
            case ContentType.STRUCTURED:
                return formatters.format_structured(raw, n)
            case ContentType.BINARY:
                data = raw if isinstance(raw, bytes) else bytes(raw)
                return formatters.format_binary(data, n)
            case _:
                return formatters.format_plaintext(str(raw), n)
