"""
tests/unit/test_optimizer.py — Unit tests for Context Optimizer pipeline.

Uses a real in-memory SQLite store (tmpdir fixture).
No E2B. No network.
"""
import json
import pytest
import os
import tempfile

from agent_gateway.config import OptimizerConfig
from agent_gateway.context_optimizer.store import ResultStore, generate_ref_id
from agent_gateway.context_optimizer.detector import detect_content_type
from agent_gateway.context_optimizer.optimizer import ContextOptimizer
from agent_gateway.types import ContentType


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = ResultStore(db_path=db_path)
    await s.initialize()
    return s


def make_optimizer(store, max_bytes=100, max_lines=5) -> ContextOptimizer:
    cfg = OptimizerConfig(
        max_output_bytes   = max_bytes,
        max_output_lines   = max_lines,
        result_ttl_seconds = 3600,
        fragment_max_limit = 100,
    )
    return ContextOptimizer(config=cfg, store=store)


# ── generate_ref_id ───────────────────────────────────────────────────────────

class TestGenerateRefId:
    def test_format(self):
        ref = generate_ref_id("read_file")
        parts = ref.split("_")
        # prefix + suffix
        assert len(parts) == 2
        assert parts[0] == "readfile"  # underscores stripped, max 8 chars
        assert len(parts[1]) == 8      # uuid8

    def test_uniqueness(self):
        ids = {generate_ref_id("tool") for _ in range(100)}
        assert len(ids) == 100

    def test_long_tool_name_truncated(self):
        ref = generate_ref_id("very_long_tool_name_that_exceeds_limit")
        prefix = ref.split("_")[0]
        assert len(prefix) <= 8

    def test_tool_name_without_underscores(self):
        ref = generate_ref_id("search")
        assert ref.startswith("search_")


# ── detect_content_type ───────────────────────────────────────────────────────

class TestDetectContentType:
    def test_bytes(self):
        assert detect_content_type(b"\x00\x01\x02") == ContentType.BINARY

    def test_bytearray(self):
        assert detect_content_type(bytearray(b"hello")) == ContentType.BINARY

    def test_dict(self):
        assert detect_content_type({"a": 1}) == ContentType.STRUCTURED

    def test_list(self):
        assert detect_content_type([1, 2, 3]) == ContentType.STRUCTURED

    def test_json_string(self):
        assert detect_content_type('{"key": "value"}') == ContentType.JSON

    def test_json_array_string(self):
        assert detect_content_type('[1, 2, 3]') == ContentType.JSON

    def test_invalid_json_string(self):
        assert detect_content_type("{not valid json}") == ContentType.PLAINTEXT

    def test_plain_string(self):
        assert detect_content_type("Hello, world!") == ContentType.PLAINTEXT

    def test_integer(self):
        assert detect_content_type(42) == ContentType.PLAINTEXT

    def test_none(self):
        assert detect_content_type(None) == ContentType.PLAINTEXT


# ── ResultStore ───────────────────────────────────────────────────────────────

class TestResultStore:
    async def test_save_and_load(self, store):
        ref_id = "test_12345678"
        content = b"line1\nline2\nline3"

        await store.save(
            ref_id       = ref_id,
            session_id   = "sess1",
            tool_name    = "test_tool",
            content_type = ContentType.PLAINTEXT,
            raw_content  = content,
            total_lines  = 3,
            ttl_seconds  = 3600,
        )

        result = await store.load(ref_id)
        assert result == content

    async def test_load_nonexistent_returns_none(self, store):
        result = await store.load("nonexistent_ref")
        assert result is None

    async def test_expired_entry_returns_none(self, store):
        ref_id = "exp_12345678"
        await store.save(
            ref_id       = ref_id,
            session_id   = "sess1",
            tool_name    = "tool",
            content_type = ContentType.PLAINTEXT,
            raw_content  = b"data",
            total_lines  = 1,
            ttl_seconds  = -1,  # Already expired
        )
        result = await store.load(ref_id)
        assert result is None

    async def test_purge_expired(self, store):
        await store.save(
            ref_id="live_12345678", session_id="s", tool_name="t",
            content_type=ContentType.PLAINTEXT, raw_content=b"alive",
            total_lines=1, ttl_seconds=3600,
        )
        await store.save(
            ref_id="dead_12345678", session_id="s", tool_name="t",
            content_type=ContentType.PLAINTEXT, raw_content=b"dead",
            total_lines=1, ttl_seconds=-1,
        )
        deleted = await store.purge_expired()
        assert deleted == 1

        assert await store.load("live_12345678") is not None
        assert await store.load("dead_12345678") is None


# ── ContextOptimizer ──────────────────────────────────────────────────────────

class TestContextOptimizer:
    async def test_small_output_passthrough(self, store):
        opt    = make_optimizer(store, max_bytes=10000, max_lines=1000)
        result = await opt.process("hello world", "test_tool", "sess1")
        assert result == "hello world"

    async def test_large_plaintext_triggers_truncation(self, store):
        opt     = make_optimizer(store, max_bytes=50, max_lines=3)
        big_txt = "\n".join(f"line {i}" for i in range(100))
        result  = await opt.process(big_txt, "test_tool", "sess1")

        assert "RESULTADO TRUNCADO" in result
        assert "fetch_fragment" in result
        assert "ref_id" in result.lower() or "Ref:" in result

    async def test_large_json_triggers_truncation(self, store):
        opt    = make_optimizer(store, max_bytes=50, max_lines=3)
        data   = {"key": "value", "items": list(range(100))}
        result = await opt.process(data, "json_tool", "sess1")

        assert "RESULTADO TRUNCADO" in result
        assert "JSON" in result

    async def test_ref_id_in_truncation_message(self, store):
        opt    = make_optimizer(store, max_bytes=10, max_lines=1)
        result = await opt.process("A" * 200, "my_tool", "sess1")

        # ref_id format: "mytool_{8hex}"
        assert "mytool_" in result

    async def test_full_content_stored_and_retrievable(self, store):
        opt    = make_optimizer(store, max_bytes=10, max_lines=1)
        lines  = [f"line_{i}" for i in range(20)]
        big    = "\n".join(lines)
        result = await opt.process(big, "test_tool", "sess1")

        # Extract ref_id from result
        for part in result.split():
            if "_" in part and len(part.split("_")) == 2:
                candidate = part.strip('[]"')
                raw = await store.load(candidate)
                if raw is not None:
                    stored_text = raw.decode()
                    assert "line_19" in stored_text
                    return

        pytest.fail("Could not find a valid ref_id in truncation message")

    async def test_binary_content_passthrough_within_limit(self, store):
        opt    = make_optimizer(store, max_bytes=10000, max_lines=1000)
        binary = b"\x00\x01\x02\x03"
        result = await opt.process(binary, "bin_tool", "sess1")
        # Small binary returns hex
        assert result == binary.hex()

    async def test_structured_dict_within_limit(self, store):
        opt    = make_optimizer(store, max_bytes=10000, max_lines=1000)
        data   = {"name": "test", "value": 42}
        result = await opt.process(data, "struct_tool", "sess1")
        parsed = json.loads(result)
        assert parsed["name"] == "test"
