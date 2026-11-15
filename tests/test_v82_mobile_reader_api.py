import hashlib
import sqlite3
import unittest
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory

from tools import local_web


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_reader_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                create_time_iso TEXT,
                update_time_iso TEXT,
                message_count INTEGER,
                default_model_slug TEXT,
                is_archived TEXT,
                is_starred TEXT,
                source_platform TEXT,
                memory_space TEXT
            );
            CREATE TABLE messages (
                message_id TEXT,
                node_id TEXT,
                conversation_id TEXT,
                conversation_title TEXT,
                ordinal INTEGER,
                parent_id TEXT,
                role TEXT,
                author_name TEXT,
                create_time REAL,
                create_time_iso TEXT,
                update_time_iso TEXT,
                content_type TEXT,
                text TEXT,
                metadata_json TEXT,
                source_platform TEXT,
                memory_space TEXT
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "c-main", "Irregular reader", "2026-01-01T00:00:00Z",
                    "2026-01-02T00:00:00Z", 48, "conversation-default", "0", "0",
                    "deepseek", "general",
                ),
                (
                    "c-other", "Other", "2026-01-01T00:00:00Z",
                    "2026-01-03T00:00:00Z", 2, "other-default", "0", "0",
                    "chatgpt", "whale",
                ),
                (
                    "c-ambiguous", "Ambiguous", "2026-01-01T00:00:00Z",
                    "2026-01-04T00:00:00Z", 2, "", "0", "0", "chatgpt", "general",
                ),
            ],
        )
        rows = []
        for index in range(48):
            ordinal = None if index in {7, 19, 31} else index // 2
            create_time = None if index in {5, 17, 29} else float((index // 3) * 10)
            message_id = "dup-cross" if index == 24 else f"main-{index:02d}"
            metadata = '{"model_slug":"message-model"}' if index == 24 else "{}"
            rows.append(
                (
                    message_id, f"node-{index:02d}", "c-main", "Irregular reader", ordinal,
                    "" if index == 0 else f"node-{index - 1:02d}",
                    "user" if index % 2 == 0 else "assistant", "", create_time,
                    f"2026-01-01T00:{index:02d}:00Z", f"2026-01-01T00:{index:02d}:30Z",
                    "text", f"message {index}", metadata, "deepseek", "general",
                )
            )
        rows.extend(
            [
                (
                    "dup-cross", "other-0", "c-other", "Other", 0, "", "assistant", "",
                    1.0, "2026-01-01T00:00:01Z", "", "text", "other duplicate", "{}",
                    "chatgpt", "whale",
                ),
                (
                    "other-1", "other-1", "c-other", "Other", 1, "other-0", "user", "",
                    2.0, "2026-01-01T00:00:02Z", "", "text", "other second", "{}",
                    "chatgpt", "whale",
                ),
                (
                    "same-id", "amb-0", "c-ambiguous", "Ambiguous", 0, "", "user", "",
                    1.0, "", "", "text", "ambiguous one", "{}", "chatgpt", "general",
                ),
                (
                    "same-id", "amb-1", "c-ambiguous", "Ambiguous", 1, "", "assistant", "",
                    2.0, "", "", "text", "ambiguous two", "{}", "chatgpt", "general",
                ),
            ]
        )
        conn.executemany(
            """
            INSERT INTO messages (
                message_id, node_id, conversation_id, conversation_title, ordinal, parent_id,
                role, author_name, create_time, create_time_iso, update_time_iso, content_type,
                text, metadata_json, source_platform, memory_space
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


class MobileConversationReaderApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "reader.db"
        create_reader_db(self.db_path)
        self.handler = object.__new__(local_web.AyunOSHandler)
        self.handler.app = local_web.App(self.db_path.resolve(), real=False, readonly=True)
        self.responses: list[tuple[dict, HTTPStatus]] = []
        self.handler.send_json = lambda payload, status=HTTPStatus.OK: self.responses.append(
            (payload, status)
        )
        self.handler.send_error_json = (
            lambda message, status=HTTPStatus.BAD_REQUEST: self.responses.append(
                ({"error": message}, status)
            )
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call_reader(self, **params: str) -> tuple[dict, HTTPStatus]:
        self.responses.clear()
        self.handler.api_conversation_messages({name: [value] for name, value in params.items()})
        self.assertEqual(len(self.responses), 1)
        return self.responses[0]

    def ordered_main_ids(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            return [
                row[0]
                for row in conn.execute(
                    """
                    SELECT message_id FROM messages WHERE conversation_id = 'c-main'
                    ORDER BY ordinal IS NULL, ordinal, create_time IS NULL, create_time, rowid
                    """
                )
            ]

    def test_anchor_page_is_centered_ordered_and_contains_trace_metadata(self) -> None:
        ordered = self.ordered_main_ids()
        anchor = "dup-cross"
        anchor_index = ordered.index(anchor)
        payload, status = self.call_reader(
            conversation_id="c-main", anchor_message_id=anchor, limit="20",
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual([row["message_id"] for row in payload["messages"]], ordered[anchor_index - 10:anchor_index + 10])
        self.assertEqual(len(payload["messages"]), 20)
        self.assertEqual(payload["focus"]["message_id"], anchor)
        self.assertTrue(payload["focus"]["is_loaded"])
        focus = next(row for row in payload["messages"] if row["is_focus"])
        self.assertEqual(focus["ordinal"], 12)
        self.assertEqual(focus["model_slug"], "message-model")
        self.assertEqual(focus["model_trace"]["resolved_model_slug"], "message-model")
        self.assertEqual(focus["default_model_slug"], "conversation-default")
        self.assertEqual(focus["source_platform_label"], "DeepSeek")
        self.assertEqual(payload["conversation"]["total_message_count"], 48)
        self.assertTrue(payload["has_before"])
        self.assertTrue(payload["has_after"])
        self.assertTrue(payload["cursors"]["before"])
        self.assertTrue(payload["cursors"]["after"])

    def test_message_context_returns_two_stable_neighbors_on_each_side(self) -> None:
        ordered = self.ordered_main_ids()
        anchor = "dup-cross"
        anchor_index = ordered.index(anchor)
        before_hash = file_hash(self.db_path)

        self.responses.clear()
        self.handler.api_message_context({
            "conversation_id": ["c-main"],
            "message_id": [anchor],
            "window": ["2"],
        })

        self.assertEqual(len(self.responses), 1)
        payload, status = self.responses[0]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(
            [row["message_id"] for row in payload["messages"]],
            ordered[anchor_index - 2:anchor_index + 3],
        )
        self.assertEqual(len(payload["messages"]), 5)
        self.assertEqual(payload["messages"][2]["message_id"], anchor)
        self.assertTrue(payload["messages"][2]["is_focus"])
        self.assertEqual(file_hash(self.db_path), before_hash)

    def test_before_and_after_pages_do_not_overlap_in_the_same_direction(self) -> None:
        initial, _ = self.call_reader(
            conversation_id="c-main", anchor_message_id="dup-cross", limit="9",
        )
        before_one, _ = self.call_reader(
            conversation_id="c-main", before=initial["cursors"]["before"], limit="7",
        )
        before_two, _ = self.call_reader(
            conversation_id="c-main", before=before_one["cursors"]["before"], limit="7",
        )
        after_one, _ = self.call_reader(
            conversation_id="c-main", after=initial["cursors"]["after"], limit="7",
        )
        after_two, _ = self.call_reader(
            conversation_id="c-main", after=after_one["cursors"]["after"], limit="7",
        )
        groups = [initial, before_one, before_two, after_one, after_two]
        id_sets = [{row["message_id"] for row in group["messages"]} for group in groups]
        for left in range(len(id_sets)):
            for right in range(left + 1, len(id_sets)):
                self.assertFalse(id_sets[left] & id_sets[right])

    def test_duplicate_id_in_another_conversation_is_scoped_but_local_duplicates_conflict(self) -> None:
        payload, status = self.call_reader(
            conversation_id="c-other", anchor_message_id="dup-cross",
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload["conversation"]["conversation_id"], "c-other")
        self.assertEqual(payload["focus"]["message_id"], "dup-cross")

        error, status = self.call_reader(
            conversation_id="c-ambiguous", anchor_message_id="same-id",
        )
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(error["error"], "Ambiguous message anchor")

    def test_cursor_validation_foreign_direction_and_stale_are_distinct_failures(self) -> None:
        initial, _ = self.call_reader(
            conversation_id="c-main", anchor_message_id="dup-cross", limit="8",
        )
        before_cursor = initial["cursors"]["before"]
        malformed, status = self.call_reader(
            conversation_id="c-main", before=before_cursor + "!", limit="8",
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Invalid", malformed["error"])

        foreign, status = self.call_reader(
            conversation_id="c-other", before=before_cursor, limit="8",
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn("another conversation", foreign["error"])

        wrong_direction, status = self.call_reader(
            conversation_id="c-main", after=before_cursor, limit="8",
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn("direction", wrong_direction["error"])

        with sqlite3.connect(self.db_path) as conn:
            first_id = initial["messages"][0]["message_id"]
            conn.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND message_id = ?",
                ("c-main", first_id),
            )
            conn.commit()
        stale, status = self.call_reader(
            conversation_id="c-main", before=before_cursor, limit="8",
        )
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(stale, {"error": "Conversation cursor is stale"})
        self.assertNotIn("has_before", stale)

    def test_boundary_flags_and_strict_request_errors(self) -> None:
        first_id = self.ordered_main_ids()[0]
        last_id = self.ordered_main_ids()[-1]
        first, status = self.call_reader(
            conversation_id="c-main", anchor_message_id=first_id,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(first["has_before"])
        self.assertIsNone(first["cursors"]["before"])
        self.assertTrue(first["has_after"])
        last, status = self.call_reader(
            conversation_id="c-main", anchor_message_id=last_id,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(last["has_before"])
        self.assertFalse(last["has_after"])
        self.assertIsNone(last["cursors"]["after"])

        cases = [
            ({"conversation_id": "c-main"}, HTTPStatus.BAD_REQUEST),
            ({"conversation_id": "c-main", "anchor_message_id": first_id, "after": "x"}, HTTPStatus.BAD_REQUEST),
            ({"conversation_id": "c-main", "anchor_message_id": first_id, "limit": "21"}, HTTPStatus.BAD_REQUEST),
            ({"conversation_id": "missing", "anchor_message_id": first_id}, HTTPStatus.NOT_FOUND),
            ({"conversation_id": "c-main", "anchor_message_id": "missing"}, HTTPStatus.NOT_FOUND),
            ({"conversation_id": "c-main", "anchor_message_id": first_id, "unknown": "x"}, HTTPStatus.BAD_REQUEST),
        ]
        for params, expected_status in cases:
            with self.subTest(params=params):
                _, status = self.call_reader(**params)
                self.assertEqual(status, expected_status)

        self.responses.clear()
        self.handler.api_conversation_messages(
            {"conversation_id": ["c-main"], "anchor_message_id": [first_id, last_id]}
        )
        self.assertEqual(self.responses[0][1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(self.responses[0][0]["error"], "Repeated conversation reader parameter")

    def test_reader_keeps_database_hash_and_legacy_conversation_contract_unchanged(self) -> None:
        before_hash = file_hash(self.db_path)
        _, status = self.call_reader(
            conversation_id="c-main", anchor_message_id="dup-cross",
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(file_hash(self.db_path), before_hash)

        self.responses.clear()
        self.handler.api_conversation({"id": ["c-other"], "focus_message_id": ["dup-cross"]})
        self.assertEqual(len(self.responses), 1)
        legacy, legacy_status = self.responses[0]
        self.assertEqual(legacy_status, HTTPStatus.OK)
        self.assertEqual(set(legacy), {"conversation", "focus_message_id", "messages"})
        self.assertEqual(legacy["focus_message_id"], "dup-cross")
        self.assertEqual(len(legacy["messages"]), 2)
        self.assertEqual(file_hash(self.db_path), before_hash)


if __name__ == "__main__":
    unittest.main()
