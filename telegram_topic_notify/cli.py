from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import socket
import sys
import time
from typing import Any, Protocol

from dotenv import load_dotenv
from telethon import Button, TelegramClient, errors, events, functions, types


load_dotenv()


DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_TOPIC_PREFIX = "notify"
SESSION_DIR_APP_NAME = "telegram-topic-notify"
DEFAULT_FORCE_REPLY_PLACEHOLDER = "Reply inside this topic"


class TelegramNotifyError(RuntimeError):
    """Raised when the CLI cannot complete its Telegram workflow."""


@dataclass(frozen=True)
class ReplyRecord:
    chat_id: int
    topic_id: int | None
    message_id: int
    from_user_id: int | None
    reply_to_message_id: int | None
    text: str
    raw_json: str


class TelethonMessageProtocol(Protocol):
    id: int
    chat_id: int | None
    sender_id: int | None
    text: str | None
    raw_text: str | None
    reply_to: Any
    photo: Any
    video: Any
    voice: Any
    audio: Any
    document: Any
    sticker: Any
    gif: Any
    geo: Any
    contact: Any
    poll: Any

    def to_dict(self) -> dict[str, Any]: ...
    def to_json(self, fp: Any = None, default: Any = None, **kwargs: Any) -> str | None: ...


class TelethonConversationProtocol(Protocol):
    async def __aenter__(self) -> TelethonConversationProtocol: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None: ...

    def wait_event(self, event: Any, *, timeout: int | float | None = None) -> Any: ...


class TelethonClientProtocol(Protocol):
    async def start(self, *, bot_token: str) -> Any: ...

    async def disconnect(self) -> Any: ...

    def conversation(
        self,
        entity: int,
        *,
        timeout: float = ...,
        total_timeout: float | None = ...,
        max_messages: int = ...,
        exclusive: bool = ...,
        replies_are_responses: bool = ...,
    ) -> TelethonConversationProtocol: ...

    async def send_message(
        self,
        entity: int,
        message: str,
        *,
        reply_to: int | TelethonMessageProtocol | None = None,
        buttons: Any = None,
    ) -> TelethonMessageProtocol: ...

    async def __call__(self, request: Any) -> Any: ...


def normalize_message(message: str) -> str:
    normalized = message.rstrip("\n")
    if not normalized.strip():
        raise TelegramNotifyError("Notification text is empty.")
    return normalized


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="telegram-topic-notify",
        description="Create a Telegram topic via Telethon, send a message inside it, and print the matching reply.",
    )
    parser.add_argument("message_parts", nargs="*", help="Notification text. Reads stdin when omitted.")
    parser.add_argument("--token", help="Telegram bot token. Falls back to TELEGRAM_BOT_TOKEN.")
    parser.add_argument("--user-id", type=int, help="Target Telegram user id. Falls back to TELEGRAM_USER_ID.")
    parser.add_argument("--api-id", type=int, help="Telegram API ID. Falls back to TG_API_ID.")
    parser.add_argument("--api-hash", help="Telegram API hash. Falls back to TG_API_HASH.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Reply wait timeout in seconds.")
    parser.add_argument("--topic-prefix", default=DEFAULT_TOPIC_PREFIX, help="Prefix for generated topic names.")
    parser.add_argument(
        "--session-name",
        help="Persistent Telethon session name for this worker/client. Falls back to TG_SESSION_NAME or hostname.",
    )
    parser.add_argument("--state-dir", help="Override the local Telethon session directory.")
    parser.add_argument("--json", action="store_true", help="Print the matched reply as JSON instead of plain text.")
    return parser.parse_args(argv)


def resolve_message(args: argparse.Namespace) -> str:
    if args.message_parts:
        message = " ".join(args.message_parts)
    elif not sys.stdin.isatty():
        message = sys.stdin.read()
    else:
        raise TelegramNotifyError("Missing notification text. Pass it as arguments or pipe it via stdin.")

    return normalize_message(message)


def resolve_token_value(token: str | None = None) -> str:
    value = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not value:
        raise TelegramNotifyError("Missing bot token. Use --token or TELEGRAM_BOT_TOKEN.")
    return value


def resolve_user_id_value(user_id: int | str | None = None) -> int:
    raw = user_id if user_id is not None else os.environ.get("TELEGRAM_USER_ID")
    if raw is None:
        raise TelegramNotifyError("Missing Telegram user id. Use --user-id or TELEGRAM_USER_ID.")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise TelegramNotifyError(f"Invalid Telegram user id: {raw!r}") from exc


def resolve_api_id_value(api_id: int | str | None = None) -> int:
    raw = api_id if api_id is not None else os.environ.get("TG_API_ID") or os.environ.get("TELEGRAM_API_ID")
    if raw is None:
        raise TelegramNotifyError("Missing Telegram API ID. Use --api-id or TG_API_ID.")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise TelegramNotifyError(f"Invalid Telegram API ID: {raw!r}") from exc


def resolve_api_hash_value(api_hash: str | None = None) -> str:
    value = api_hash or os.environ.get("TG_API_HASH") or os.environ.get("TELEGRAM_API_HASH")
    if not value:
        raise TelegramNotifyError("Missing Telegram API hash. Use --api-hash or TG_API_HASH.")
    return value


def normalize_session_name(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        raise TelegramNotifyError("Telethon session name is empty.")
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", stripped).strip("._-")
    if normalized:
        return normalized
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]


def resolve_session_name_value(session_name: str | None = None) -> str:
    raw = session_name or os.environ.get("TG_SESSION_NAME") or socket.gethostname()
    return normalize_session_name(raw)


def default_state_root() -> Path:
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home)
    return Path.home() / ".local" / "state"


def resolve_session_path(token: str, override_dir: str | None, session_name: str) -> Path:
    if override_dir:
        root = Path(override_dir).expanduser()
    else:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        root = default_state_root() / SESSION_DIR_APP_NAME / token_hash
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{session_name}.session"


def create_telethon_client(session_path: Path, api_id: int, api_hash: str) -> TelethonClientProtocol:
    return TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        receive_updates=True,
        sequential_updates=True,
        raise_last_call_error=True,
    )


def build_topic_name(prefix: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"{prefix}-{timestamp}-{secrets.token_hex(4)}"


def extract_message_text(message: TelethonMessageProtocol) -> str:
    if message.text:
        return str(message.text)
    if message.raw_text:
        return str(message.raw_text)

    media_labels = (
        ("photo", "[photo]"),
        ("video", "[video]"),
        ("voice", "[voice]"),
        ("audio", "[audio]"),
        ("document", "[document]"),
        ("sticker", "[sticker]"),
        ("gif", "[animation]"),
        ("geo", "[location]"),
        ("contact", "[contact]"),
        ("poll", "[poll]"),
    )
    for field_name, label in media_labels:
        if getattr(message, field_name, None):
            return label

    return serialize_message_json(message)


def serialize_message_json(message: TelethonMessageProtocol) -> str:
    to_json = getattr(message, "to_json", None)
    if callable(to_json):
        rendered = to_json(ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if isinstance(rendered, str):
            return rendered

    return json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def extract_topic_id(message: TelethonMessageProtocol) -> int | None:
    reply_to = getattr(message, "reply_to", None)
    if not reply_to or not getattr(reply_to, "forum_topic", False):
        return None

    topic_id = getattr(reply_to, "reply_to_top_id", None)
    if topic_id is None:
        topic_id = getattr(reply_to, "reply_to_msg_id", None)
    if topic_id is None:
        return None
    return int(topic_id)


def extract_reply_to_message_id(message: TelethonMessageProtocol) -> int | None:
    reply_to = getattr(message, "reply_to", None)
    reply_to_message_id = getattr(reply_to, "reply_to_msg_id", None)
    if reply_to_message_id is None:
        return None
    return int(reply_to_message_id)


def reply_record_from_message(message: TelethonMessageProtocol) -> ReplyRecord:
    chat_id = message.chat_id
    if chat_id is None:
        raise TelegramNotifyError("Telethon returned a reply without chat_id.")
    raw_json = serialize_message_json(message)

    return ReplyRecord(
        chat_id=int(chat_id),
        topic_id=extract_topic_id(message),
        message_id=int(message.id),
        from_user_id=int(message.sender_id) if message.sender_id is not None else None,
        reply_to_message_id=extract_reply_to_message_id(message),
        text=extract_message_text(message),
        raw_json=raw_json,
    )


def extract_created_topic_id(updates: Any) -> int:
    fallback_ids: list[int] = []
    for update in getattr(updates, "updates", []):
        message = getattr(update, "message", None)
        action = getattr(message, "action", None)
        if action is not None and action.__class__.__name__ == "MessageActionTopicCreate":
            topic_id = extract_topic_id(message)
            if isinstance(topic_id, int):
                return topic_id

            message_id = getattr(message, "id", None)
            if isinstance(message_id, int):
                fallback_ids.append(message_id)

        update_id = getattr(update, "id", None)
        if isinstance(update_id, int):
            fallback_ids.append(update_id)

    if fallback_ids:
        return fallback_ids[0]
    raise TelegramNotifyError("CreateForumTopic succeeded but did not return a topic id.")


async def create_topic(client: TelethonClientProtocol, chat_id: int, topic_name: str) -> int:
    updates = await client(functions.messages.CreateForumTopicRequest(peer=chat_id, title=topic_name))
    return extract_created_topic_id(updates)


async def send_notification(
    client: TelethonClientProtocol,
    chat_id: int,
    topic_id: int,
    text: str,
) -> Any:
    return await client(
        functions.messages.SendMessageRequest(
            peer=chat_id,
            message=text,
            reply_to=types.InputReplyToMessage(reply_to_msg_id=topic_id, top_msg_id=topic_id),
            reply_markup=TelegramClient.build_reply_markup(
                Button.force_reply(single_use=True, placeholder=DEFAULT_FORCE_REPLY_PLACEHOLDER)
            ),
        )
    )


async def ask_user_via_telegram_async(
    question: str,
    *,
    token: str | None = None,
    user_id: int | str | None = None,
    api_id: int | str | None = None,
    api_hash: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    topic_prefix: str = DEFAULT_TOPIC_PREFIX,
    state_dir: str | None = None,
    session_name: str | None = None,
    client_factory: Any = create_telethon_client,
) -> ReplyRecord:
    resolved_token = resolve_token_value(token)
    resolved_user_id = resolve_user_id_value(user_id)
    resolved_api_id = resolve_api_id_value(api_id)
    resolved_api_hash = resolve_api_hash_value(api_hash)
    resolved_session_name = resolve_session_name_value(session_name)
    resolved_timeout = max(1, int(timeout_seconds))
    message = normalize_message(question)
    session_path = resolve_session_path(
        token=resolved_token,
        override_dir=state_dir,
        session_name=resolved_session_name,
    )

    client = client_factory(session_path, resolved_api_id, resolved_api_hash)
    topic_id: int | None = None
    try:
        await client.start(bot_token=resolved_token)
        topic_name = build_topic_name(topic_prefix)
        topic_id = await create_topic(client, resolved_user_id, topic_name)

        async with client.conversation(
            resolved_user_id,
            timeout=resolved_timeout,
            total_timeout=resolved_timeout,
            max_messages=100,
            exclusive=False,
            replies_are_responses=False,
        ) as conversation:
            reply_handle = conversation.wait_event(
                events.NewMessage(
                    incoming=True,
                    from_users=resolved_user_id,
                    func=lambda event: extract_topic_id(event) == topic_id,
                ),
                timeout=resolved_timeout,
            )
            await send_notification(client, resolved_user_id, topic_id, message)
            reply = await reply_handle
            return reply_record_from_message(reply)
    except asyncio.TimeoutError as exc:
        if topic_id is None:
            raise TelegramNotifyError(f"Timed out after {resolved_timeout}s waiting for Telegram to create the topic.") from exc
        raise TelegramNotifyError(
            f"Timed out after {resolved_timeout}s waiting for a reply in Telegram topic {topic_id}."
        ) from exc
    except errors.RPCError as exc:
        raise TelegramNotifyError(f"Telethon RPC failed: {exc.__class__.__name__}: {exc}") from exc
    except OSError as exc:
        raise TelegramNotifyError(f"Telethon connection failed: {exc}") from exc
    finally:
        await client.disconnect()


def ask_user_via_telegram(
    question: str,
    *,
    token: str | None = None,
    user_id: int | str | None = None,
    api_id: int | str | None = None,
    api_hash: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    topic_prefix: str = DEFAULT_TOPIC_PREFIX,
    state_dir: str | None = None,
    session_name: str | None = None,
    client_factory: Any = create_telethon_client,
) -> ReplyRecord:
    return asyncio.run(
        ask_user_via_telegram_async(
            question,
            token=token,
            user_id=user_id,
            api_id=api_id,
            api_hash=api_hash,
            timeout_seconds=timeout_seconds,
            topic_prefix=topic_prefix,
            state_dir=state_dir,
            session_name=session_name,
            client_factory=client_factory,
        )
    )


def reply_to_payload(reply: ReplyRecord) -> dict[str, Any]:
    return {
        "reply_text": reply.text,
        "chat_id": reply.chat_id,
        "topic_id": reply.topic_id,
        "message_id": reply.message_id,
        "from_user_id": reply.from_user_id,
        "reply_to_message_id": reply.reply_to_message_id,
        "raw_message": json.loads(reply.raw_json),
    }


def render_reply(reply: ReplyRecord, as_json: bool) -> str:
    if not as_json:
        return reply.text
    return json.dumps(reply_to_payload(reply), ensure_ascii=False, indent=2, sort_keys=True)


def run(argv: list[str] | None = None) -> str:
    args = parse_args(argv)
    message = resolve_message(args)
    reply = ask_user_via_telegram(
        message,
        token=resolve_token_value(args.token),
        user_id=resolve_user_id_value(args.user_id),
        api_id=resolve_api_id_value(args.api_id),
        api_hash=resolve_api_hash_value(args.api_hash),
        timeout_seconds=args.timeout,
        topic_prefix=args.topic_prefix,
        state_dir=args.state_dir,
        session_name=resolve_session_name_value(args.session_name),
    )
    return render_reply(reply, as_json=args.json)


def main(argv: list[str] | None = None) -> int:
    try:
        output = run(argv)
    except TelegramNotifyError as exc:
        print(f"telegram-topic-notify: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
