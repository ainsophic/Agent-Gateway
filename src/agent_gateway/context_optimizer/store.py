"""
context_optimizer/store.py — Persistent SQLite store for truncated tool results.

Schema:
  result_store(ref_id, session_id, tool_name, created_at, expires_at,
               content_type, raw_content, total_bytes, total_lines)

Lifecycle:
  1. initialize() — called once at startup, creates tables if needed
  2. save()       — called by ContextOptimizer when output exceeds threshold
  3. load()       — called by fetch_fragment tool at Claude's request
  4. purge_expired() — call periodically to clean up expired rows
"""
from __future__ import annotations

import logging
import os
import time
import uuid

import aiosqlite

from agent_gateway.types import ContentType, RefId, SessionId, ToolName

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS result_store (
    ref_id        TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    content_type  TEXT NOT NULL,
    raw_content   BLOB NOT NULL,
    total_bytes   INTEGER NOT NULL,
    total_lines   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session
    ON result_store(session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_expiry
    ON result_store(expires_at);
"""


def generate_ref_id(tool_name: ToolName) -> RefId:
    """
    Generate a human-readable unique reference ID.

    Format: "{tool_prefix}_{uuid8}"
    Example: "readfile_a3f2b891"

    tool_prefix = first 8 non-underscore chars of tool_name (lowercase)
    uuid8       = first 8 chars of a random UUID4 hex
    """
    prefix = tool_name.lower().replace("_", "")[:8]
    suffix = uuid.uuid4().hex[:8]
    return f"{prefix}_{suffix}"


class ResultStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def initialize(self) -> None:
        """
        Create the DB file (and parent dirs) and run schema migrations.
        Enables WAL mode for safe concurrent reads.
        Must be called once at startup before any other method.
        """
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(_SCHEMA)
            await db.commit()

        log.info("[ResultStore] Initialized at %s", self._db_path)

    async def save(
        self,
        ref_id:       RefId,
        session_id:   SessionId,
        tool_name:    ToolName,
        content_type: ContentType,
        raw_content:  bytes,
        total_lines:  int,
        ttl_seconds:  int,
    ) -> None:
        """
        Persist a full tool result.
        Uses INSERT OR REPLACE so duplicate ref_ids are overwritten safely.
        """
        now       = time.time()
        raw_bytes = (
            raw_content
            if isinstance(raw_content, bytes)
            else raw_content.encode("utf-8", errors="replace")
        )
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO result_store
                  (ref_id, session_id, tool_name, created_at, expires_at,
                   content_type, raw_content, total_bytes, total_lines)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref_id,
                    session_id,
                    tool_name,
                    now,
                    now + ttl_seconds,
                    content_type.value,
                    raw_bytes,
                    len(raw_bytes),
                    total_lines,
                ),
            )
            await db.commit()

        log.debug(
            "[ResultStore] Saved ref_id=%s bytes=%d ttl=%ds",
            ref_id, len(raw_bytes), ttl_seconds,
        )

    async def load(self, ref_id: RefId) -> bytes | None:
        """
        Retrieve raw_content bytes for a ref_id.
        Returns None if not found or expired.
        """
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT raw_content FROM result_store "
                "WHERE ref_id = ? AND expires_at > ?",
                (ref_id, time.time()),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def purge_expired(self) -> int:
        """
        Delete all expired rows.
        Returns the number of rows deleted.
        Call on startup and periodically (e.g., every N saves).
        """
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM result_store WHERE expires_at < ?",
                (time.time(),),
            )
            await db.commit()
            deleted = cursor.rowcount

        if deleted:
            log.info("[ResultStore] Purged %d expired entries", deleted)

        return deleted
