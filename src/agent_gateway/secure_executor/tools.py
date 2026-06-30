"""
secure_executor/tools.py — MCP tool registrations for the Secure Executor.

Tools registered here are the only surface through which Claude can
execute code or interact with the sandbox filesystem.

All tools funnel through SecureExecutorGateway, which routes to E2B.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from agent_gateway.types import GatewayException, Language, SessionId
from agent_gateway.secure_executor.gateway import SecureExecutorGateway
from agent_gateway.session import SessionRegistry

log = logging.getLogger(__name__)


def register_executor_tools(
    mcp:      FastMCP,
    gateway:  SecureExecutorGateway,
    registry: SessionRegistry,
) -> None:
    """Register all secure executor MCP tools. Call once at startup."""

    @mcp.tool()
    async def execute_python(
        code:       str,
        session_id: SessionId = "default",
    ) -> str:
        """
        Execute Python code in an isolated E2B sandbox.

        Args:
            code:       Python source code to run.
            session_id: Session identifier. Files written persist within the same session.

        Returns:
            Formatted stdout/stderr + exit code + execution time.
        """
        ctx = registry.get_or_create(session_id)
        try:
            result = await gateway.execute_code(
                code            = code,
                sandbox_manager = ctx.sandbox_manager,
                language        = Language.PYTHON,
            )
            return result.to_tool_output()
        except GatewayException as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    async def execute_shell(
        command:    str,
        session_id: SessionId = "default",
    ) -> str:
        """
        Execute a bash command or shell script in an isolated E2B sandbox.

        Args:
            command:    Shell command or multi-line script (bash).
            session_id: Session identifier.

        Returns:
            Formatted stdout/stderr + exit code + execution time.
        """
        ctx = registry.get_or_create(session_id)
        try:
            result = await gateway.execute_shell(
                command         = command,
                sandbox_manager = ctx.sandbox_manager,
            )
            return result.to_tool_output()
        except GatewayException as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    async def execute_javascript(
        code:       str,
        session_id: SessionId = "default",
    ) -> str:
        """
        Execute JavaScript (Node.js) code in an isolated E2B sandbox.

        Args:
            code:       JavaScript source code to run.
            session_id: Session identifier.

        Returns:
            Formatted stdout/stderr + exit code + execution time.
        """
        ctx = registry.get_or_create(session_id)
        try:
            result = await gateway.execute_code(
                code            = code,
                sandbox_manager = ctx.sandbox_manager,
                language        = Language.JAVASCRIPT,
            )
            return result.to_tool_output()
        except GatewayException as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    async def upload_to_sandbox(
        content_hex:  str,
        sandbox_path: str,
        session_id:   SessionId = "default",
    ) -> str:
        """
        Upload a file to the sandbox filesystem.

        Args:
            content_hex:  File content encoded as a lowercase hex string.
            sandbox_path: Target path. Must start with /home/user/ or /tmp/.
            session_id:   Session identifier.

        Returns:
            Confirmation string with byte count and path.

        Example:
            upload_to_sandbox(content_hex="68656c6c6f", sandbox_path="/tmp/hello.txt")
        """
        ctx = registry.get_or_create(session_id)
        try:
            content = bytes.fromhex(content_hex)
            await gateway.upload_file(ctx.sandbox_manager, content, sandbox_path)
            return f"Uploaded {len(content):,} bytes → {sandbox_path}"
        except (ValueError, GatewayException) as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    async def download_from_sandbox(
        sandbox_path: str,
        session_id:   SessionId = "default",
    ) -> str:
        """
        Download a file from the sandbox filesystem.

        Args:
            sandbox_path: Source path in the sandbox (must start with /home/user/ or /tmp/).
            session_id:   Session identifier.

        Returns:
            Hex-encoded file content (use bytes.fromhex() to decode).
        """
        ctx = registry.get_or_create(session_id)
        try:
            content = await gateway.download_file(ctx.sandbox_manager, sandbox_path)
            return content.hex()
        except GatewayException as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    async def reset_sandbox(session_id: SessionId = "default") -> str:
        """
        Destroy and recreate the session sandbox. Clears all files and state.

        Use when you need a completely clean environment mid-session.
        The next tool call will automatically provision a fresh sandbox.

        Args:
            session_id: Session identifier.
        """
        ctx = registry.get_or_create(session_id)
        await ctx.sandbox_manager.teardown()
        return f"Sandbox for session '{session_id}' has been reset. A fresh sandbox will be created on the next execution."
