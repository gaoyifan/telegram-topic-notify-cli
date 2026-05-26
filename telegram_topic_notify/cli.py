from __future__ import annotations

import argparse
import contextlib
import hashlib
import httpx
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from dotenv import load_dotenv
from typing import Any, Iterator


load_dotenv()


DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_POLL_TIMEOUT_SECONDS = 20
DEFAULT_TOPIC_PREFIX = "notify"
STATE_DIR_APP_NAME = "telegram-topic-notify"


class TelegramNotifyError(RuntimeError):
    """Raised when the CLI cannot complete its Telegram workflow."""


@dataclass(frozen=True)
class ReplyRecord:
    update_id: int
    chat_id: int
    thread_id: int
    message_id: int
    from_user_id: int | None
    text: str
    raw_json: str


def normalize_message(message: str) -> str:
    normalized = message.rstrip("\n")
    if not normalized.strip():
        raise TelegramNotifyError("Notification text is empty.")
    return normalized


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="telegram-topic-notify",
        description="Send a Telegram notification in its own private-chat topic and print the matching reply.",
    )
    parser.add_argument("message_parts", nargs="*", help="Notification text. Reads stdin when omitted.")
    parser.add_argument("--token", help="Telegram bot token. Falls back to TELEGRAM_BOT_TOKEN.")
    parser.add_argument("--user-id", type=int, help="Target Telegram user id. Falls back to TELEGRAM_USER_ID.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Reply wait timeout in seconds.")
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=DEFAULT_POLL_TIMEOUT_SECONDS,
        help="Single getUpdates long-poll timeout in seconds.",
    )
    parser.add_argument("--topic-prefix", default=DEFAULT_TOPIC_PREFIX, help="Prefix for generated topic names.")
    parser.add_argument("--state-dir", help="Override the shared state directory.")
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
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramNotifyError("Missing bot token. Use --token or TELEGRAM_BOT_TOKEN.")
    return token


def resolve_token(args: argparse.Namespace) -> str:
    return resolve_token_value(args.token)


def resolve_user_id_value(user_id: int | str | None = None) -> int:
    raw = user_id if user_id is not None else os.environ.get("TELEGRAM_USER_ID")
    if raw is None:
        raise TelegramNotifyError("Missing Telegram user id. Use --user-id or TELEGRAM_USER_ID.")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise TelegramNotifyError(f"Invalid Telegram user id: {raw!r}") from exc


def resolve_user_id(args: argparse.Namespace) -> int:
    return resolve_user_id_value(args.user_id)


def default_state_root() -> Path:
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home)
    return Path.home() / ".local" / "state"


def resolve_state_paths(token: str, override_dir: str | None) -> tuple[Path, Path]:
    if override_dir:
        root = Path(override_dir).expanduser()
    else:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        root = default_state_root() / STATE_DIR_APP_NAME / token_hash
    root.mkdir(parents=True, exist_ok=True)
    return root / "state.sqlite3", root / "poll.lock"


def encode_params(params: dict[str, Any]) -> dict[str, str]:
    encoded: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded[key] = "true" if value else "false"
        elif isinstance(value, (dict, list, tuple)):
            encoded[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            encoded[key] = str(value)
    return encoded


class TelegramBotApi:
    def __init__(self, token: str) -> None:
        self._client = httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}/",
            follow_redirects=False,
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _request_timeout(params: dict[str, Any]) -> httpx.Timeout:
        long_poll_seconds = int(params.get("timeout", 0) or 0)
        read_timeout = max(long_poll_seconds + 30, 60)
        return httpx.Timeout(connect=10.0, read=float(read_timeout), write=30.0, pool=30.0)

    def call(self, method: str, **params: Any) -> Any:
        try:
            response = self._client.post(
                method,
                data=encode_params(params),
                timeout=self._request_timeout(params),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            raise TelegramNotifyError(f"{method} failed with HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise TelegramNotifyError(f"{method} failed: {exc}") from exc

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise TelegramNotifyError(f"{method} returned invalid JSON: {response.text}") from exc

        if not payload.get("ok"):
            description = payload.get("description", "Unknown Telegram API error")
            raise TelegramNotifyError(f"{method} failed: {description}")
        return payload["result"]


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                topic_name TEXT NOT NULL,
                notification_message_id INTEGER,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                replied_at REAL
            );

            CREATE TABLE IF NOT EXISTS replies (
                update_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                message_id INTEGER NOT NULL,
                from_user_id INTEGER,
                is_bot INTEGER NOT NULL,
                is_topic_message INTEGER NOT NULL,
                text TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                consumed_by_request_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_replies_lookup
            ON replies (chat_id, thread_id, from_user_id, is_bot, is_topic_message, consumed_by_request_id, update_id);
            """
        )

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - 7 * 24 * 60 * 60
        self._conn.execute("DELETE FROM replies WHERE created_at < ?", (cutoff,))
        self._conn.execute("DELETE FROM requests WHERE status != 'waiting' AND created_at < ?", (cutoff,))

    def get_update_offset(self) -> int | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = 'update_offset'").fetchone()
        return int(row["value"]) if row else None

    def set_update_offset(self, offset: int) -> None:
        self._conn.execute(
            """
            INSERT INTO settings (key, value) VALUES ('update_offset', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(offset),),
        )

    def create_request(self, request_id: str, chat_id: int, thread_id: int, topic_name: str) -> None:
        self._conn.execute(
            """
            INSERT INTO requests (request_id, chat_id, thread_id, topic_name, status, created_at)
            VALUES (?, ?, ?, ?, 'waiting', ?)
            """,
            (request_id, chat_id, thread_id, topic_name, time.time()),
        )

    def mark_request_sent(self, request_id: str, notification_message_id: int) -> None:
        self._conn.execute(
            "UPDATE requests SET notification_message_id = ? WHERE request_id = ?",
            (notification_message_id, request_id),
        )

    def set_request_status(self, request_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE requests SET status = ?, replied_at = CASE WHEN ? = 'done' THEN ? ELSE replied_at END WHERE request_id = ?",
            (status, status, time.time(), request_id),
        )

    def record_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        self._conn.execute(
            """
            INSERT OR IGNORE INTO replies (
                update_id,
                chat_id,
                thread_id,
                message_id,
                from_user_id,
                is_bot,
                is_topic_message,
                text,
                raw_json,
                created_at,
                consumed_by_request_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                int(update["update_id"]),
                int(chat.get("id", 0)),
                int(message["message_thread_id"]) if message.get("message_thread_id") is not None else None,
                int(message["message_id"]),
                int(sender["id"]) if sender.get("id") is not None else None,
                1 if sender.get("is_bot") else 0,
                1 if message.get("is_topic_message") else 0,
                extract_message_text(message),
                json.dumps(message, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                time.time(),
            ),
        )

    def claim_reply(self, request_id: str, chat_id: int, thread_id: int, from_user_id: int) -> ReplyRecord | None:
        row = self._conn.execute(
            """
            SELECT update_id, chat_id, thread_id, message_id, from_user_id, text, raw_json
            FROM replies
            WHERE consumed_by_request_id IS NULL
              AND chat_id = ?
              AND thread_id = ?
              AND from_user_id = ?
              AND is_bot = 0
              AND is_topic_message = 1
            ORDER BY update_id ASC
            LIMIT 1
            """,
            (chat_id, thread_id, from_user_id),
        ).fetchone()
        if row is None:
            return None

        cursor = self._conn.execute(
            """
            UPDATE replies
            SET consumed_by_request_id = ?
            WHERE update_id = ? AND consumed_by_request_id IS NULL
            """,
            (request_id, row["update_id"]),
        )
        if cursor.rowcount != 1:
            return None

        self._conn.execute(
            "UPDATE requests SET status = 'done', replied_at = ? WHERE request_id = ?",
            (time.time(), request_id),
        )
        return ReplyRecord(
            update_id=int(row["update_id"]),
            chat_id=int(row["chat_id"]),
            thread_id=int(row["thread_id"]),
            message_id=int(row["message_id"]),
            from_user_id=int(row["from_user_id"]) if row["from_user_id"] is not None else None,
            text=str(row["text"]),
            raw_json=str(row["raw_json"]),
        )


def extract_message_text(message: dict[str, Any]) -> str:
    if message.get("text"):
        return str(message["text"])
    if message.get("caption"):
        return str(message["caption"])

    media_labels = {
        "photo": "[photo]",
        "video": "[video]",
        "voice": "[voice]",
        "audio": "[audio]",
        "document": "[document]",
        "sticker": "[sticker]",
        "animation": "[animation]",
        "location": "[location]",
        "contact": "[contact]",
        "poll": "[poll]",
    }
    for field, label in media_labels.items():
        if message.get(field):
            return label

    if message.get("forum_topic_created"):
        name = (message["forum_topic_created"] or {}).get("name")
        return f"[topic-created] {name}".strip()

    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@contextlib.contextmanager
def maybe_acquire_lock(lock_path: Path) -> Iterator[bool]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_topic_name(prefix: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"{prefix}-{timestamp}-{secrets.token_hex(4)}"


def create_topic(api: TelegramBotApi, chat_id: int, topic_name: str) -> int:
    result = api.call("createForumTopic", chat_id=chat_id, name=topic_name)
    thread_id = result.get("message_thread_id")
    if thread_id is None:
        raise TelegramNotifyError("createForumTopic succeeded but did not return message_thread_id.")
    return int(thread_id)


def send_notification(api: TelegramBotApi, chat_id: int, thread_id: int, text: str) -> int:
    result = api.call("sendMessage", chat_id=chat_id, message_thread_id=thread_id, text=text)
    message_id = result.get("message_id")
    if message_id is None:
        raise TelegramNotifyError("sendMessage succeeded but did not return message_id.")
    return int(message_id)


def pump_updates(api: TelegramBotApi, store: StateStore, lock_path: Path, poll_timeout: int) -> bool:
    with maybe_acquire_lock(lock_path) as locked:
        if not locked:
            return False

        offset = store.get_update_offset()
        updates = api.call(
            "getUpdates",
            offset=offset,
            timeout=max(1, poll_timeout),
            allowed_updates=["message"],
        )
        next_offset = offset
        for update in updates:
            update_id = int(update["update_id"])
            store.record_update(update)
            next_offset = update_id + 1 if next_offset is None else max(next_offset, update_id + 1)
        if next_offset is not None and next_offset != offset:
            store.set_update_offset(next_offset)
        return True


def wait_for_reply(
    api: TelegramBotApi,
    store: StateStore,
    lock_path: Path,
    request_id: str,
    chat_id: int,
    thread_id: int,
    from_user_id: int,
    timeout_seconds: int,
    poll_timeout_seconds: int,
) -> ReplyRecord:
    deadline = time.monotonic() + timeout_seconds
    while True:
        reply = store.claim_reply(request_id=request_id, chat_id=chat_id, thread_id=thread_id, from_user_id=from_user_id)
        if reply is not None:
            return reply

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TelegramNotifyError(
                f"Timed out after {timeout_seconds}s waiting for a reply in Telegram topic {thread_id}."
            )

        long_poll_timeout = min(max(1, int(remaining)), max(1, poll_timeout_seconds))
        did_poll = pump_updates(api=api, store=store, lock_path=lock_path, poll_timeout=long_poll_timeout)
        if not did_poll:
            time.sleep(min(0.5, remaining))


def reply_to_payload(reply: ReplyRecord) -> dict[str, Any]:
    return {
        "reply_text": reply.text,
        "update_id": reply.update_id,
        "chat_id": reply.chat_id,
        "thread_id": reply.thread_id,
        "message_id": reply.message_id,
        "from_user_id": reply.from_user_id,
        "raw_message": json.loads(reply.raw_json),
    }


def ask_user_via_telegram(
    question: str,
    *,
    token: str | None = None,
    user_id: int | str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    poll_timeout_seconds: int = DEFAULT_POLL_TIMEOUT_SECONDS,
    topic_prefix: str = DEFAULT_TOPIC_PREFIX,
    state_dir: str | None = None,
) -> ReplyRecord:
    resolved_token = resolve_token_value(token)
    resolved_user_id = resolve_user_id_value(user_id)
    message = normalize_message(question)
    db_path, lock_path = resolve_state_paths(token=resolved_token, override_dir=state_dir)

    api = TelegramBotApi(resolved_token)
    store = StateStore(db_path)
    request_id: str | None = None
    try:
        store.prune()
        topic_name = build_topic_name(topic_prefix)
        thread_id = create_topic(api=api, chat_id=resolved_user_id, topic_name=topic_name)
        request_id = secrets.token_hex(16)
        store.create_request(
            request_id=request_id,
            chat_id=resolved_user_id,
            thread_id=thread_id,
            topic_name=topic_name,
        )
        try:
            notification_message_id = send_notification(
                api=api,
                chat_id=resolved_user_id,
                thread_id=thread_id,
                text=message,
            )
        except Exception:
            store.set_request_status(request_id, "failed")
            raise
        store.mark_request_sent(request_id=request_id, notification_message_id=notification_message_id)
        try:
            return wait_for_reply(
                api=api,
                store=store,
                lock_path=lock_path,
                request_id=request_id,
                chat_id=resolved_user_id,
                thread_id=thread_id,
                from_user_id=resolved_user_id,
                timeout_seconds=max(1, timeout_seconds),
                poll_timeout_seconds=max(1, poll_timeout_seconds),
            )
        except Exception as exc:
            status = "timed_out" if isinstance(exc, TelegramNotifyError) and str(exc).startswith("Timed out after") else "failed"
            store.set_request_status(request_id, status)
            raise
    finally:
        store.close()
        api.close()


def render_reply(reply: ReplyRecord, as_json: bool) -> str:
    if not as_json:
        return reply.text
    return json.dumps(reply_to_payload(reply), ensure_ascii=False, indent=2, sort_keys=True)


def run(argv: list[str] | None = None) -> str:
    args = parse_args(argv)
    message = resolve_message(args)
    reply = ask_user_via_telegram(
        message,
        token=resolve_token(args),
        user_id=resolve_user_id(args),
        timeout_seconds=args.timeout,
        poll_timeout_seconds=args.poll_timeout,
        topic_prefix=args.topic_prefix,
        state_dir=args.state_dir,
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
