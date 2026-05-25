from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from telegram_topic_notify.cli import (
    DEFAULT_POLL_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TOPIC_PREFIX,
    ask_user_via_telegram,
    reply_to_payload,
)


mcp = FastMCP("telegram-topic-notify", json_response=True)


class AskUserReply(BaseModel):
    reply_text: str = Field(description="用户回复的文本内容。")
    update_id: int = Field(description="匹配到的 Telegram update_id。")
    chat_id: int = Field(description="用户私聊的 chat id。")
    thread_id: int = Field(description="本次问题所在 topic 的线程 id。")
    message_id: int = Field(description="用户回复消息的 message id。")
    from_user_id: int | None = Field(description="回复消息发送者的 Telegram user id。")
    raw_message: dict[str, Any] = Field(description="Telegram 原始消息对象。")


@mcp.tool(title="询问用户")
def ask_user(
    question: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    poll_timeout_seconds: int = DEFAULT_POLL_TIMEOUT_SECONDS,
    topic_prefix: str = DEFAULT_TOPIC_PREFIX,
    state_dir: str | None = None,
) -> AskUserReply:
    """询问用户一个问题，并等待用户回复。

    适合在你需要澄清需求、确认选择或收集补充信息时使用。

    Args:
        question: 发送给用户的问题或说明。
        timeout_seconds: 等待用户回复的最长时间，默认 60 秒。
        poll_timeout_seconds: 单次长轮询等待时间。
        topic_prefix: 为本次会话创建的 topic 名前缀。
        state_dir: 可选的共享本地状态目录；通常不需要设置。
    """
    reply = ask_user_via_telegram(
        question,
        timeout_seconds=timeout_seconds,
        poll_timeout_seconds=poll_timeout_seconds,
        topic_prefix=topic_prefix,
        state_dir=state_dir,
    )
    return AskUserReply(**reply_to_payload(reply))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
