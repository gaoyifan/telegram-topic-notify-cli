from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from telegram_topic_notify.cli import (
    TelegramNotifyError,
    ask_user_via_telegram_async,
    normalize_session_name,
    reply_record_from_message,
    resolve_session_path,
    run,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class FakeReplyTo:
    reply_to_msg_id: int | None
    reply_to_top_id: int | None = None
    forum_topic: bool = False


@dataclass
class FakeMessage:
    id: int
    chat_id: int
    sender_id: int | None
    text: str | None = None
    raw_text: str | None = None
    reply_to: FakeReplyTo | None = None
    photo: object | None = None
    video: object | None = None
    voice: object | None = None
    audio: object | None = None
    document: object | None = None
    sticker: object | None = None
    gif: object | None = None
    geo: object | None = None
    contact: object | None = None
    poll: object | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "text": self.text,
            "raw_text": self.raw_text,
        }
        if self.reply_to is not None:
            payload["reply_to"] = {
                "reply_to_msg_id": self.reply_to.reply_to_msg_id,
                "reply_to_top_id": self.reply_to.reply_to_top_id,
                "forum_topic": self.reply_to.forum_topic,
            }
        if self.photo is not None:
            payload["photo"] = True
        if self.document is not None:
            payload["document"] = True
        return payload

    def to_json(self, fp=None, default=None, **kwargs):
        payload = self.to_dict()
        if fp is not None:
            return json.dump(payload, fp, default=default, **kwargs)
        return json.dumps(payload, default=default, **kwargs)


class FakeNonSerializableTelethonMessage(FakeMessage):
    def to_dict(self) -> dict[str, object]:
        return {"nested_message": object()}

    def to_json(self, fp=None, default=None, **kwargs):
        payload = {
            "id": self.id,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "reply_to": {
                "reply_to_msg_id": self.reply_to.reply_to_msg_id if self.reply_to else None,
                "reply_to_top_id": self.reply_to.reply_to_top_id if self.reply_to else None,
                "forum_topic": self.reply_to.forum_topic if self.reply_to else False,
            },
        }
        if fp is not None:
            return json.dump(payload, fp, default=default, **kwargs)
        return json.dumps(payload, default=default, **kwargs)


@dataclass
class MessageActionTopicCreate:
    title: str


@dataclass
class FakeServiceMessage:
    id: int
    action: MessageActionTopicCreate
    reply_to: FakeReplyTo | None = None


@dataclass
class FakeTopicCreatedUpdate:
    message: FakeServiceMessage


@dataclass
class FakeUpdates:
    updates: list[object]


class FakeTelethonBackend:
    def __init__(self) -> None:
        self.bot_sender_id = 999000
        self.next_message_id = 1000
        self.next_topic_id = 500000
        self.created_topics: list[tuple[int, int, str]] = []
        self.sent_messages: list[FakeMessage] = []
        self.sent_buttons: list[object] = []
        self.clients: list[FakeTelethonClient] = []
        self.incoming_messages: list[FakeMessage] = []
        self.condition = asyncio.Condition()
        self.raise_timeout = False

    def create_factory(self):
        def factory(session_path: Path, api_id: int, api_hash: str) -> FakeTelethonClient:
            client = FakeTelethonClient(self, session_path=Path(session_path), api_id=api_id, api_hash=api_hash)
            self.clients.append(client)
            return client

        return factory

    async def create_topic(self, *, chat_id: int, title: str) -> FakeUpdates:
        service_message_id = self.next_message_id
        self.next_message_id += 1
        topic_id = self.next_topic_id
        self.next_topic_id += 1
        self.created_topics.append((chat_id, topic_id, title))
        return FakeUpdates(
            updates=[
                FakeTopicCreatedUpdate(
                    message=FakeServiceMessage(
                        id=service_message_id,
                        action=MessageActionTopicCreate(title=title),
                        reply_to=FakeReplyTo(reply_to_msg_id=None, reply_to_top_id=topic_id, forum_topic=True),
                    )
                )
            ]
        )

    async def register_sent_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None,
        reply_to_message_id: int | None,
        reply_markup: object,
    ) -> FakeMessage:
        reply_header = None
        if topic_id is not None:
            reply_to_id = reply_to_message_id
            if reply_to_id == topic_id:
                reply_to_id = None
            reply_header = FakeReplyTo(
                reply_to_msg_id=reply_to_id,
                reply_to_top_id=topic_id,
                forum_topic=True,
            )

        message = FakeMessage(
            id=self.next_message_id,
            chat_id=chat_id,
            sender_id=self.bot_sender_id,
            text=text,
            raw_text=text,
            reply_to=reply_header,
        )
        self.next_message_id += 1
        async with self.condition:
            self.sent_messages.append(message)
            self.sent_buttons.append(reply_markup)
            self.condition.notify_all()
        return message

    async def wait_for_sent_count(self, count: int) -> None:
        async with self.condition:
            await asyncio.wait_for(self.condition.wait_for(lambda: len(self.sent_messages) >= count), timeout=1)

    async def wait_for_matching_message(self, *, builder: object, chat_id: int, timeout: int | float | None) -> FakeMessage:
        if self.raise_timeout:
            raise asyncio.TimeoutError

        index = 0
        while True:
            async with self.condition:
                await asyncio.wait_for(self.condition.wait_for(lambda: len(self.incoming_messages) > index), timeout=timeout)
                message = self.incoming_messages[index]
                index += 1

            if self._matches(builder, message, chat_id=chat_id):
                return message

    def _matches(self, builder: object, message: FakeMessage, *, chat_id: int) -> bool:
        if message.chat_id != chat_id:
            return False

        from_users = getattr(builder, "from_users", None)
        if from_users is not None:
            if isinstance(from_users, (list, tuple, set, frozenset)):
                if message.sender_id not in from_users:
                    return False
            elif message.sender_id != from_users:
                return False

        incoming = getattr(builder, "incoming", None)
        if incoming and message.sender_id == self.bot_sender_id:
            return False

        func = getattr(builder, "func", None)
        if func is not None and not func(message):
            return False

        return True

    async def deliver_message(
        self,
        *,
        chat_id: int,
        reply_text: str,
        sender_id: int,
        topic_id: int | None,
        reply_to_message_id: int | None,
    ) -> None:
        reply_header = None
        if topic_id is not None:
            reply_header = FakeReplyTo(
                reply_to_msg_id=reply_to_message_id,
                reply_to_top_id=topic_id,
                forum_topic=True,
            )
        elif reply_to_message_id is not None:
            reply_header = FakeReplyTo(reply_to_msg_id=reply_to_message_id, forum_topic=False)

        message = FakeMessage(
            id=self.next_message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            text=reply_text,
            raw_text=reply_text,
            reply_to=reply_header,
        )
        self.next_message_id += 1
        async with self.condition:
            self.incoming_messages.append(message)
            self.condition.notify_all()


class FakeTelethonClient:
    def __init__(self, backend: FakeTelethonBackend, *, session_path: Path, api_id: int, api_hash: str) -> None:
        self.backend = backend
        self.session_path = session_path
        self.api_id = api_id
        self.api_hash = api_hash
        self.started_bot_token: str | None = None
        self.disconnected = False

    async def start(self, *, bot_token: str) -> None:
        self.started_bot_token = bot_token

    async def disconnect(self) -> None:
        self.disconnected = True

    async def __call__(self, request: object) -> FakeUpdates:
        request_name = request.__class__.__name__
        if request_name == "CreateForumTopicRequest":
            return await self.backend.create_topic(chat_id=int(request.peer), title=str(request.title))
        if request_name == "SendMessageRequest":
            reply_to = request.reply_to
            topic_id = getattr(reply_to, "top_msg_id", None)
            if topic_id is None:
                topic_id = getattr(reply_to, "reply_to_msg_id", None)
            return await self.backend.register_sent_message(
                chat_id=int(request.peer),
                text=str(request.message),
                topic_id=int(topic_id) if topic_id is not None else None,
                reply_to_message_id=getattr(reply_to, "reply_to_msg_id", None),
                reply_markup=request.reply_markup,
            )
        raise AssertionError(f"Unexpected request: {request!r}")

    def conversation(
        self,
        entity: int,
        *,
        timeout: float = 60,
        total_timeout: float | None = None,
        max_messages: int = 100,
        exclusive: bool = False,
        replies_are_responses: bool = False,
    ) -> FakeConversation:
        return FakeConversation(self.backend, chat_id=entity, timeout=timeout)


class FakeConversation:
    def __init__(self, backend: FakeTelethonBackend, *, chat_id: int, timeout: float) -> None:
        self.backend = backend
        self.chat_id = chat_id
        self.timeout = timeout

    async def __aenter__(self) -> FakeConversation:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def wait_event(self, event: object, *, timeout: int | float | None = None):
        return self.backend.wait_for_matching_message(
            builder=event,
            chat_id=self.chat_id,
            timeout=self.timeout if timeout is None else timeout,
        )


def test_normalize_session_name_rejects_blank() -> None:
    with pytest.raises(TelegramNotifyError, match="session name is empty"):
        normalize_session_name("   ")


def test_resolve_session_path_uses_persistent_session_name(tmp_path: Path) -> None:
    path_a = resolve_session_path("token-1", str(tmp_path), "worker-a")
    path_b = resolve_session_path("token-1", str(tmp_path), "worker-b")

    assert path_a == tmp_path / "worker-a.session"
    assert path_b == tmp_path / "worker-b.session"


def test_reply_record_from_message_maps_topic_metadata() -> None:
    reply = reply_record_from_message(
        FakeMessage(
            id=7,
            chat_id=42,
            sender_id=99,
            text=None,
            raw_text=None,
            photo=object(),
            reply_to=FakeReplyTo(reply_to_msg_id=6, reply_to_top_id=5, forum_topic=True),
        )
    )

    assert reply.chat_id == 42
    assert reply.topic_id == 5
    assert reply.message_id == 7
    assert reply.from_user_id == 99
    assert reply.reply_to_message_id == 6
    assert reply.text == "[photo]"


def test_reply_record_from_message_uses_telethon_json_for_non_serializable_payloads() -> None:
    reply = reply_record_from_message(
        FakeNonSerializableTelethonMessage(
            id=9,
            chat_id=43,
            sender_id=100,
            text=None,
            raw_text=None,
            reply_to=FakeReplyTo(reply_to_msg_id=8, reply_to_top_id=7, forum_topic=True),
        )
    )

    payload = json.loads(reply.raw_json)
    assert payload["id"] == 9
    assert payload["reply_to"]["reply_to_top_id"] == 7
    assert json.loads(reply.text)["reply_to"]["reply_to_msg_id"] == 8


@pytest.mark.anyio
async def test_ask_user_creates_topic_and_sends_inside_it(tmp_path: Path) -> None:
    backend = FakeTelethonBackend()
    task = asyncio.create_task(
        ask_user_via_telegram_async(
            "需要更多上下文吗？",
            token="bot-token",
            user_id=123456789,
            api_id=12345,
            api_hash="hash",
            state_dir=str(tmp_path),
            session_name="worker-a",
            client_factory=backend.create_factory(),
        )
    )

    await backend.wait_for_sent_count(1)
    prompt = backend.sent_messages[0]
    markup = backend.sent_buttons[0]
    topic_id = backend.created_topics[0][1]
    await backend.deliver_message(
        chat_id=prompt.chat_id,
        reply_text="请继续",
        sender_id=123456789,
        topic_id=topic_id,
        reply_to_message_id=prompt.id,
    )
    reply = await task

    assert backend.created_topics[0][0] == 123456789
    assert backend.created_topics[0][2].startswith("notify-")
    assert prompt.text == "需要更多上下文吗？"
    assert prompt.reply_to is not None
    assert prompt.reply_to.forum_topic is True
    assert prompt.reply_to.reply_to_top_id == topic_id
    assert prompt.reply_to.reply_to_msg_id is None
    assert markup.__class__.__name__ == "ReplyKeyboardForceReply"
    assert markup.single_use is True
    assert reply.text == "请继续"
    assert reply.topic_id == topic_id
    assert reply.reply_to_message_id == prompt.id
    assert backend.clients[0].session_path == tmp_path / "worker-a.session"
    assert backend.clients[0].disconnected is True


@pytest.mark.anyio
async def test_ask_user_ignores_messages_outside_the_topic(tmp_path: Path) -> None:
    backend = FakeTelethonBackend()
    task = asyncio.create_task(
        ask_user_via_telegram_async(
            "请在 topic 中回复",
            token="bot-token",
            user_id=123456789,
            api_id=12345,
            api_hash="hash",
            state_dir=str(tmp_path),
            session_name="worker-topic-filter",
            client_factory=backend.create_factory(),
        )
    )

    await backend.wait_for_sent_count(1)
    prompt = backend.sent_messages[0]
    topic_id = backend.created_topics[0][1]

    await backend.deliver_message(
        chat_id=prompt.chat_id,
        reply_text="这是主通道里的消息",
        sender_id=123456789,
        topic_id=None,
        reply_to_message_id=None,
    )
    await asyncio.sleep(0)
    assert task.done() is False

    await backend.deliver_message(
        chat_id=prompt.chat_id,
        reply_text="这是别的 topic",
        sender_id=123456789,
        topic_id=topic_id + 100,
        reply_to_message_id=prompt.id,
    )
    await asyncio.sleep(0)
    assert task.done() is False

    await backend.deliver_message(
        chat_id=prompt.chat_id,
        reply_text="这是正确 topic 的回复",
        sender_id=123456789,
        topic_id=topic_id,
        reply_to_message_id=prompt.id,
    )
    reply = await task

    assert reply.text == "这是正确 topic 的回复"
    assert reply.topic_id == topic_id


@pytest.mark.anyio
async def test_ask_user_reports_timeout_with_topic_id(tmp_path: Path) -> None:
    backend = FakeTelethonBackend()
    backend.raise_timeout = True

    with pytest.raises(TelegramNotifyError, match=r"Timed out after 1s waiting for a reply in Telegram topic \d+\."):
        await ask_user_via_telegram_async(
            "请回复",
            token="bot-token",
            user_id=123456789,
            api_id=12345,
            api_hash="hash",
            timeout_seconds=1,
            state_dir=str(tmp_path),
            session_name="worker-timeout",
            client_factory=backend.create_factory(),
        )


@pytest.mark.anyio
async def test_concurrent_requests_use_distinct_sessions_and_topics(tmp_path: Path) -> None:
    backend = FakeTelethonBackend()
    client_factory = backend.create_factory()

    task_a = asyncio.create_task(
        ask_user_via_telegram_async(
            "first question",
            token="bot-token",
            user_id=123456789,
            api_id=12345,
            api_hash="hash",
            state_dir=str(tmp_path),
            session_name="session-a",
            client_factory=client_factory,
        )
    )
    task_b = asyncio.create_task(
        ask_user_via_telegram_async(
            "second question",
            token="bot-token",
            user_id=123456789,
            api_id=12345,
            api_hash="hash",
            state_dir=str(tmp_path),
            session_name="session-b",
            client_factory=client_factory,
        )
    )

    await backend.wait_for_sent_count(2)
    prompts_by_text = {message.text: message for message in backend.sent_messages}
    topics_by_text = {
        prompt_text: prompt.reply_to.reply_to_top_id
        for prompt_text, prompt in prompts_by_text.items()
        if prompt.reply_to is not None
    }

    await backend.deliver_message(
        chat_id=prompts_by_text["second question"].chat_id,
        reply_text="reply-b",
        sender_id=123456789,
        topic_id=topics_by_text["second question"],
        reply_to_message_id=prompts_by_text["second question"].id,
    )
    await backend.deliver_message(
        chat_id=prompts_by_text["first question"].chat_id,
        reply_text="reply-a",
        sender_id=123456789,
        topic_id=topics_by_text["first question"],
        reply_to_message_id=prompts_by_text["first question"].id,
    )

    reply_a, reply_b = await asyncio.gather(task_a, task_b)

    assert reply_a.text == "reply-a"
    assert reply_b.text == "reply-b"
    assert reply_a.topic_id == topics_by_text["first question"]
    assert reply_b.topic_id == topics_by_text["second question"]
    assert {client.session_path for client in backend.clients} == {
        tmp_path / "session-a.session",
        tmp_path / "session-b.session",
    }


def test_run_renders_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_reply = reply_record_from_message(
        FakeMessage(
            id=88,
            chat_id=123456789,
            sender_id=123456789,
            text="好的",
            raw_text="好的",
            reply_to=FakeReplyTo(reply_to_msg_id=77, reply_to_top_id=66, forum_topic=True),
        )
    )

    monkeypatch.setattr("telegram_topic_notify.cli.ask_user_via_telegram", lambda *args, **kwargs: fake_reply)

    output = run(
        [
            "--token",
            "bot-token",
            "--user-id",
            "123456789",
            "--api-id",
            "12345",
            "--api-hash",
            "hash",
            "--session-name",
            "worker-json",
            "--json",
            "请确认",
        ]
    )

    payload = json.loads(output)
    assert payload["reply_text"] == "好的"
    assert payload["topic_id"] == 66
    assert payload["reply_to_message_id"] == 77
