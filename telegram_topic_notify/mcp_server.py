from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from telegram_topic_notify.cli import ask_user_via_telegram_async


mcp = FastMCP("telegram-topic-notify", json_response=True)


class AskUserReply(BaseModel):
    reply_text: str = Field(description="用户回复的文本内容。")


@mcp.tool(title="询问用户")
async def ask_user(question: str) -> AskUserReply:
    """询问用户一个问题并返回回复。"""
    reply = await ask_user_via_telegram_async(question)
    return AskUserReply(reply_text=reply.text)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
