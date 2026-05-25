from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

import telegram_topic_notify.mcp_server as mcp_server
from telegram_topic_notify.cli import ReplyRecord, TelegramNotifyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def set_fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_USER_ID", "123456789")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def fake_reply(text: str) -> ReplyRecord:
    return ReplyRecord(
        update_id=321,
        chat_id=123456789,
        thread_id=12345,
        message_id=67890,
        from_user_id=123456789,
        text=text,
        raw_json=json.dumps({"text": text}, ensure_ascii=False),
    )


@pytest.mark.anyio
async def test_mcp_lists_ask_user_tool() -> None:
    async with create_connected_server_and_client_session(mcp_server.mcp, raise_exceptions=True) as session:
        tools = await session.list_tools()

    tool = next(tool for tool in tools.tools if tool.name == "ask_user")
    assert "询问用户一个问题" in (tool.description or "")
    assert "question" in tool.inputSchema["properties"]


@pytest.mark.anyio
async def test_mcp_tool_returns_structured_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "ask_user_via_telegram", lambda question, **_: fake_reply(f"reply:{question}"))

    async with create_connected_server_and_client_session(mcp_server.mcp, raise_exceptions=True) as session:
        result = await session.call_tool("ask_user", {"question": "需要更多上下文吗？"})

    assert result.isError is False
    assert result.structuredContent["reply_text"] == "reply:需要更多上下文吗？"
    assert result.structuredContent["thread_id"] == 12345
    text_block = next(content for content in result.content if isinstance(content, types.TextContent))
    assert "reply:需要更多上下文吗？" in text_block.text


@pytest.mark.anyio
async def test_stdio_mcp_server_supports_initialize_list_and_call() -> None:
    script = """
import json
import telegram_topic_notify.mcp_server as server
from telegram_topic_notify.cli import ReplyRecord

def fake(question, **kwargs):
    return ReplyRecord(
        update_id=7,
        chat_id=123456789,
        thread_id=7001,
        message_id=8002,
        from_user_id=123456789,
        text=f"stdio:{question}",
        raw_json=json.dumps({"text": f"stdio:{question}"}, ensure_ascii=False),
    )

server.ask_user_via_telegram = fake
server.main()
""".strip()

    env = os.environ.copy()
    env["TELEGRAM_BOT_TOKEN"] = "test-token"
    env["TELEGRAM_USER_ID"] = "123456789"

    server_params = StdioServerParameters(
        command="uv",
        args=["--directory", str(PROJECT_ROOT), "run", "python", "-c", script],
        env=env,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("ask_user", {"question": "请确认是否继续"})

    assert any(tool.name == "ask_user" for tool in tools.tools)
    assert result.isError is False
    assert result.structuredContent["reply_text"] == "stdio:请确认是否继续"


@pytest.mark.anyio
async def test_mcp_tool_reports_execution_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(question: str, **_: object) -> ReplyRecord:
        raise TelegramNotifyError(f"failed:{question}")

    monkeypatch.setattr(mcp_server, "ask_user_via_telegram", fail)

    async with create_connected_server_and_client_session(mcp_server.mcp, raise_exceptions=False) as session:
        result = await session.call_tool("ask_user", {"question": "需要确认失败路径"})

    assert result.isError is True
    text_block = next(content for content in result.content if isinstance(content, types.TextContent))
    assert "failed:需要确认失败路径" in text_block.text
