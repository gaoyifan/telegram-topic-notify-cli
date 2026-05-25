from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_topic_notify.cli import StateStore, extract_message_text


class ExtractMessageTextTests(unittest.TestCase):
    def test_prefers_text(self) -> None:
        self.assertEqual(extract_message_text({"text": "hello", "caption": "ignored"}), "hello")

    def test_falls_back_to_media_label(self) -> None:
        self.assertEqual(extract_message_text({"photo": [{"file_id": "1"}]}), "[photo]")


class StateStoreTests(unittest.TestCase):
    def test_claim_reply_only_matches_user_topic_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.sqlite3")
            try:
                store.create_request("req-1", chat_id=100, thread_id=42, topic_name="notify-1")

                store.record_update(
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 10,
                            "chat": {"id": 100},
                            "from": {"id": 999, "is_bot": True},
                            "message_thread_id": 42,
                            "is_topic_message": True,
                            "text": "bot message",
                        },
                    }
                )
                self.assertIsNone(store.claim_reply("req-1", chat_id=100, thread_id=42, from_user_id=100))

                store.record_update(
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 11,
                            "chat": {"id": 100},
                            "from": {"id": 100, "is_bot": False},
                            "message_thread_id": 42,
                            "is_topic_message": False,
                            "text": "plain DM reply",
                        },
                    }
                )
                self.assertIsNone(store.claim_reply("req-1", chat_id=100, thread_id=42, from_user_id=100))

                store.record_update(
                    {
                        "update_id": 3,
                        "message": {
                            "message_id": 12,
                            "chat": {"id": 100},
                            "from": {"id": 100, "is_bot": False},
                            "message_thread_id": 7,
                            "is_topic_message": True,
                            "text": "wrong thread",
                        },
                    }
                )
                self.assertIsNone(store.claim_reply("req-1", chat_id=100, thread_id=42, from_user_id=100))

                store.record_update(
                    {
                        "update_id": 4,
                        "message": {
                            "message_id": 13,
                            "chat": {"id": 100},
                            "from": {"id": 100, "is_bot": False},
                            "message_thread_id": 42,
                            "is_topic_message": True,
                            "text": "expected reply",
                        },
                    }
                )
                reply = store.claim_reply("req-1", chat_id=100, thread_id=42, from_user_id=100)
                self.assertIsNotNone(reply)
                assert reply is not None
                self.assertEqual(reply.text, "expected reply")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
