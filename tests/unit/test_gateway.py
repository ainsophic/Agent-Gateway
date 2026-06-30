"""
tests/unit/test_gateway.py — Unit tests for SecureExecutorGateway and helpers.

E2B is fully mocked — no network, no API key required.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_gateway.config import GatewayConfig
from agent_gateway.secure_executor.gateway import SecureExecutorGateway
from agent_gateway.secure_executor.language_detector import detect_language
from agent_gateway.secure_executor.sandbox_manager import SandboxManager
from agent_gateway.types import GatewayError, GatewayException, Language


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_config(**overrides) -> GatewayConfig:
    defaults = dict(
        sandbox_timeout_seconds = 30,
        max_sandbox_age_seconds = 1800,
        max_file_bytes          = 1024,
        exec_max_output_bytes   = 256,
    )
    defaults.update(overrides)
    return GatewayConfig(**defaults)


def make_mock_sandbox(
    sandbox_id: str = "sb_test123",
    stdout: list[str] | None = None,
    stderr: list[str] | None = None,
    has_error: bool = False,
) -> MagicMock:
    """Create a mock E2B AsyncSandbox."""
    sandbox = MagicMock()
    sandbox.id = sandbox_id

    # Mock execution result
    exec_result        = MagicMock()
    exec_result.error  = MagicMock() if has_error else None
    exec_result.logs   = MagicMock()
    exec_result.logs.stdout = stdout or ["Hello, World!"]
    exec_result.logs.stderr = stderr or []

    sandbox.run_code = AsyncMock(return_value=exec_result)
    sandbox.kill     = AsyncMock()
    sandbox.files    = MagicMock()
    sandbox.files.write  = AsyncMock()
    sandbox.files.read   = AsyncMock(return_value=b"file content")

    return sandbox


def make_mock_sandbox_manager(sandbox: MagicMock) -> SandboxManager:
    """SandboxManager whose get() returns the mock sandbox."""
    manager     = MagicMock(spec=SandboxManager)
    manager.get = AsyncMock(return_value=sandbox)
    manager._session_id = "test_session"
    return manager


# ── Language detector ─────────────────────────────────────────────────────────

class TestLanguageDetector:
    def test_python_def(self):
        assert detect_language("def foo():\n    pass") == Language.PYTHON

    def test_python_import(self):
        assert detect_language("import os\nprint(os.getcwd())") == Language.PYTHON

    def test_bash_shebang(self):
        assert detect_language("#!/bin/bash\necho hello") == Language.BASH

    def test_bash_env_shebang(self):
        assert detect_language("#!/usr/bin/env bash\nls -la") == Language.BASH

    def test_bash_echo(self):
        assert detect_language("echo 'hello world'") == Language.BASH

    def test_bash_dollar_var(self):
        assert detect_language("echo $HOME") == Language.BASH

    def test_javascript_console_log(self):
        assert detect_language("console.log('hello')") == Language.JAVASCRIPT

    def test_javascript_require(self):
        assert detect_language("const fs = require('fs')") == Language.JAVASCRIPT

    def test_python_shebang_overrides_heuristic(self):
        # Even if it has bash-like content, shebang wins
        code = "#!/usr/bin/env python3\necho = 'not bash'"
        assert detect_language(code) == Language.PYTHON

    def test_empty_code_defaults_python(self):
        assert detect_language("") == Language.PYTHON

    def test_ambiguous_defaults_python(self):
        assert detect_language("42") == Language.PYTHON

    def test_node_shebang(self):
        assert detect_language("#!/usr/bin/env node\nconsole.log(1)") == Language.JAVASCRIPT


# ── SecureExecutorGateway.execute_code ────────────────────────────────────────

class TestExecuteCode:
    async def test_basic_execution(self):
        sandbox = make_mock_sandbox(stdout=["Hello, World!"])
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        result = await gateway.execute_code("print('Hello, World!')", manager)

        assert "Hello, World!" in result.stdout
        assert result.exit_code == 0
        assert result.timed_out is False
        assert result.sandbox_id == "sb_test123"

    async def test_execution_with_stderr(self):
        sandbox = make_mock_sandbox(
            stdout=["output"],
            stderr=["warning: something"],
            has_error=True,
        )
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        result = await gateway.execute_code("bad code", manager)

        assert result.exit_code == 1
        assert "warning" in result.stderr

    async def test_timeout_returns_timed_out_result(self):
        sandbox = make_mock_sandbox()
        sandbox.run_code = AsyncMock(side_effect=TimeoutError("timeout"))
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        result = await gateway.execute_code("import time; time.sleep(999)", manager)

        assert result.timed_out is True
        assert result.exit_code == -1

    async def test_output_truncated_at_limit(self):
        # exec_max_output_bytes = 10 bytes
        big_output = "A" * 1000
        sandbox    = make_mock_sandbox(stdout=[big_output])
        manager    = make_mock_sandbox_manager(sandbox)
        gateway    = SecureExecutorGateway(make_config(exec_max_output_bytes=10))

        result = await gateway.execute_code("...", manager)

        assert result.truncated is True
        assert len(result.stdout.encode()) <= 10 + len("\n[... truncated ...]") + 5

    async def test_language_auto_detected(self):
        sandbox = make_mock_sandbox(stdout=["result"])
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        await gateway.execute_code("def foo(): pass", manager)

        # run_code was called with language="python"
        call_kwargs = sandbox.run_code.call_args
        assert call_kwargs.kwargs.get("language") == "python" or \
               (call_kwargs.args and "python" in str(call_kwargs))

    async def test_explicit_language_overrides_detection(self):
        sandbox = make_mock_sandbox(stdout=[""])
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        await gateway.execute_code("echo hello", manager, language=Language.BASH)

        call_kwargs = sandbox.run_code.call_args
        # Verify bash was passed (not python, which heuristics might pick)
        called_with_bash = (
            call_kwargs.kwargs.get("language") == "bash" or
            "bash" in str(call_kwargs)
        )
        assert called_with_bash

    async def test_sandbox_error_raises_gateway_exception(self):
        sandbox      = make_mock_sandbox()
        sandbox.run_code = AsyncMock(side_effect=Exception("connection refused"))
        manager      = make_mock_sandbox_manager(sandbox)
        gateway      = SecureExecutorGateway(make_config())

        with pytest.raises(GatewayException) as exc_info:
            await gateway.execute_code("code", manager)

        assert exc_info.value.error == GatewayError.SANDBOX_UNAVAILABLE


# ── SecureExecutorGateway.execute_shell ───────────────────────────────────────

class TestExecuteShell:
    async def test_shell_wraps_as_bash(self):
        sandbox = make_mock_sandbox(stdout=["file.txt"])
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        await gateway.execute_shell("ls /tmp", manager)

        # Verify the code passed to run_code contains the command
        called_code = sandbox.run_code.call_args.args[0]
        assert "ls /tmp" in called_code
        assert "#!/bin/bash" in called_code


# ── SecureExecutorGateway.upload_file / download_file ────────────────────────

class TestFileOperations:
    async def test_upload_valid_path(self):
        sandbox = make_mock_sandbox()
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config(max_file_bytes=1024))

        await gateway.upload_file(manager, b"hello", "/tmp/test.txt")
        sandbox.files.write.assert_called_once_with("/tmp/test.txt", b"hello")

    async def test_upload_home_user_path(self):
        sandbox = make_mock_sandbox()
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        await gateway.upload_file(manager, b"data", "/home/user/file.txt")
        sandbox.files.write.assert_called_once()

    async def test_upload_invalid_path_raises(self):
        sandbox = make_mock_sandbox()
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        with pytest.raises(GatewayException) as exc_info:
            await gateway.upload_file(manager, b"data", "/etc/passwd")

        assert exc_info.value.error == GatewayError.INVALID_PATH

    async def test_upload_root_path_raises(self):
        sandbox = make_mock_sandbox()
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        with pytest.raises(GatewayException) as exc_info:
            await gateway.upload_file(manager, b"data", "/")

        assert exc_info.value.error == GatewayError.INVALID_PATH

    async def test_upload_too_large_raises(self):
        sandbox = make_mock_sandbox()
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config(max_file_bytes=10))

        with pytest.raises(GatewayException) as exc_info:
            await gateway.upload_file(manager, b"X" * 100, "/tmp/big.bin")

        assert exc_info.value.error == GatewayError.FILE_TOO_LARGE

    async def test_download_valid_path(self):
        sandbox = make_mock_sandbox()
        sandbox.files.read = AsyncMock(return_value=b"file content")
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        content = await gateway.download_file(manager, "/tmp/output.txt")
        assert content == b"file content"

    async def test_download_invalid_path_raises(self):
        sandbox = make_mock_sandbox()
        manager = make_mock_sandbox_manager(sandbox)
        gateway = SecureExecutorGateway(make_config())

        with pytest.raises(GatewayException) as exc_info:
            await gateway.download_file(manager, "/root/secret")

        assert exc_info.value.error == GatewayError.INVALID_PATH


# ── SandboxManager ────────────────────────────────────────────────────────────

class TestSandboxManager:
    async def test_unavailable_e2b_raises_gateway_exception(self):
        """If E2B creation fails, get() must raise GatewayException, never fallback."""
        import sys
        import types as _types

        fake_module = _types.ModuleType("e2b_code_interpreter")

        class _FailingAsyncSandbox:
            @staticmethod
            async def create(*args, **kwargs):
                raise Exception("E2B unreachable")

        fake_module.AsyncSandbox = _FailingAsyncSandbox

        manager = SandboxManager("sess1", make_config())

        with patch.dict(sys.modules, {"e2b_code_interpreter": fake_module}):
            with pytest.raises(GatewayException) as exc_info:
                await manager.get()

        assert exc_info.value.error == GatewayError.SANDBOX_UNAVAILABLE
        assert "E2B unreachable" in exc_info.value.detail

    async def test_teardown_does_not_raise_on_error(self):
        """teardown() must never raise even if sandbox.kill() fails."""
        manager = SandboxManager("sess1", make_config())

        mock_sandbox      = MagicMock()
        mock_sandbox.id   = "sb_test"
        mock_sandbox.kill = AsyncMock(side_effect=Exception("kill failed"))

        manager._sandbox    = mock_sandbox
        manager._created_at = 0.0

        # Should not raise
        await manager.teardown()
        assert manager._sandbox is None


# ── ExecutionResult.to_tool_output ───────────────────────────────────────────

class TestExecutionResultFormat:
    def _make_result(self, **kwargs):
        from agent_gateway.types import ExecutionResult
        defaults = dict(
            stdout="output line", stderr="", exit_code=0,
            execution_time_ms=42, timed_out=False, truncated=False,
            sandbox_id="sb_abc",
        )
        defaults.update(kwargs)
        return ExecutionResult(**defaults)

    def test_basic_format(self):
        r = self._make_result()
        out = r.to_tool_output()
        assert "STDOUT" in out
        assert "exit_code=0" in out
        assert "42ms" in out

    def test_timeout_prefix(self):
        r   = self._make_result(timed_out=True, exit_code=-1)
        out = r.to_tool_output()
        assert "TIMEOUT" in out

    def test_truncation_note(self):
        r   = self._make_result(truncated=True)
        out = r.to_tool_output()
        assert "truncated" in out.lower()

    def test_no_stdout_section_when_empty(self):
        r   = self._make_result(stdout="")
        out = r.to_tool_output()
        assert "STDOUT" not in out

    def test_stderr_shown(self):
        r   = self._make_result(stderr="error: bad input")
        out = r.to_tool_output()
        assert "STDERR" in out
        assert "bad input" in out
