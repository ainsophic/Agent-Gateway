"""
tests/integration/test_full_pipeline.py — Integration tests requiring a real
E2B_API_KEY and network access.

Run with: pytest tests/integration/ -v -m integration

These are NOT run by default (`pytest tests/unit/` skips this directory).
"""
import os
import pytest

from agent_gateway.config import load_config
from agent_gateway.context_optimizer.optimizer import ContextOptimizer
from agent_gateway.context_optimizer.store import ResultStore
from agent_gateway.loop_arbiter.arbiter import LoopArbiter
from agent_gateway.loop_arbiter.state import AgentState
from agent_gateway.secure_executor.gateway import SecureExecutorGateway
from agent_gateway.secure_executor.sandbox_manager import SandboxManager
from agent_gateway.types import Language

pytestmark = pytest.mark.integration

requires_e2b = pytest.mark.skipif(
    not os.environ.get("E2B_API_KEY"),
    reason="E2B_API_KEY not set — skipping integration tests",
)


@pytest.fixture
async def store(tmp_path):
    s = ResultStore(db_path=str(tmp_path / "integration.db"))
    await s.initialize()
    return s


@requires_e2b
class TestRealSandboxExecution:
    async def test_python_execution_roundtrip(self):
        """Execute real Python code in a real E2B sandbox."""
        from agent_gateway.config import GatewayConfig

        gateway = SecureExecutorGateway(GatewayConfig())
        manager = SandboxManager("integration_test", GatewayConfig())

        try:
            result = await gateway.execute_code(
                "print('hello from e2b')",
                manager,
                language=Language.PYTHON,
            )
            assert "hello from e2b" in result.stdout
            assert result.exit_code == 0
        finally:
            await manager.teardown()

    async def test_shell_execution_roundtrip(self):
        from agent_gateway.config import GatewayConfig

        gateway = SecureExecutorGateway(GatewayConfig())
        manager = SandboxManager("integration_test_2", GatewayConfig())

        try:
            result = await gateway.execute_shell("echo $((2 + 2))", manager)
            assert "4" in result.stdout
        finally:
            await manager.teardown()

    async def test_file_upload_download_roundtrip(self):
        from agent_gateway.config import GatewayConfig

        gateway = SecureExecutorGateway(GatewayConfig())
        manager = SandboxManager("integration_test_3", GatewayConfig())

        try:
            content = b"test file content 12345"
            await gateway.upload_file(manager, content, "/tmp/roundtrip.txt")
            downloaded = await gateway.download_file(manager, "/tmp/roundtrip.txt")
            assert downloaded == content
        finally:
            await manager.teardown()

    async def test_session_persists_files_across_calls(self):
        """Files written in one execute_code call should be visible in the next."""
        from agent_gateway.config import GatewayConfig

        gateway = SecureExecutorGateway(GatewayConfig())
        manager = SandboxManager("integration_test_4", GatewayConfig())

        try:
            await gateway.execute_code(
                "with open('/tmp/state.txt', 'w') as f: f.write('persisted')",
                manager,
            )
            result = await gateway.execute_code(
                "print(open('/tmp/state.txt').read())",
                manager,
            )
            assert "persisted" in result.stdout
        finally:
            await manager.teardown()


class TestCircuitBreakerInjection:
    """Does not require E2B — tests the ToolError injection contract."""

    async def test_n_identical_calls_trips_breaker(self):
        from agent_gateway.config import ArbiterConfig

        cfg     = ArbiterConfig(max_repeat_calls=3, max_total_calls=100)
        state   = AgentState(session_id="s1", config=cfg)
        arbiter = LoopArbiter(cfg)

        args = {"query": "SELECT * FROM users"}
        r1 = arbiter.check("query_db", args, state)
        r2 = arbiter.check("query_db", args, state)
        r3 = arbiter.check("query_db", args, state)

        assert r1 is None
        assert r2 is None
        assert r3 is not None
        assert "GATEWAY CIRCUIT BREAKER" in r3.format_message()


class TestLargeOutputFetchFragment:
    """Does not require E2B — tests the truncation + retrieval contract."""

    async def test_large_output_then_fetch_tail(self, store):
        from agent_gateway.config import OptimizerConfig

        cfg = OptimizerConfig(max_output_bytes=100, max_output_lines=10)
        opt = ContextOptimizer(config=cfg, store=store)

        lines = [f"row_{i}: data" for i in range(1000)]
        big   = "\n".join(lines)

        truncated = await opt.process(big, "query_db", "session_x")
        assert "RESULTADO TRUNCADO" in truncated

        # Extract ref_id
        ref_id = None
        for token in truncated.split():
            cleaned = token.strip('[]"')
            if cleaned.startswith("querydb_"):
                ref_id = cleaned
                break

        assert ref_id is not None

        raw = await store.load(ref_id)
        assert raw is not None
        decoded = raw.decode()
        assert "row_999" in decoded
