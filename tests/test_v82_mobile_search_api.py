import sqlite3
import unittest
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.read_only_retriever import search_all_memory_conversations
from tools import local_web


def create_search_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                update_time REAL,
                update_time_iso TEXT,
                message_count INTEGER,
                source_platform TEXT,
                memory_space TEXT
            );
            CREATE TABLE messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                conversation_title TEXT,
                ordinal INTEGER,
                role TEXT,
                create_time REAL,
                create_time_iso TEXT,
                text TEXT,
                source_platform TEXT,
                memory_space TEXT
            );
            """
        )
        conversations = [
            ("c-whale", "Whale plan", 300, "2026-07-21T00:05:00Z", 2, "deepseek", "whale"),
            ("c-general", "General discussion", 700, "2026-07-21T00:11:40Z", 1, "chatgpt", "general"),
            ("c-archive", "Archived notes", 500, "2026-07-21T00:08:20Z", 1, "deepseek", "archive"),
            ("c-title-only", "alpha beta title only", 900, "2026-07-21T00:15:00Z", 1, "chatgpt", "general"),
        ]
        conn.executemany(
            """
            INSERT INTO conversations
                (conversation_id, title, update_time, update_time_iso, message_count, source_platform, memory_space)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            conversations,
        )
        messages = [
            (
                "m-whale-best", "c-whale", "Whale plan", 0, "assistant", 100,
                "2026-07-21T00:01:40Z", "alpha beta alpha beta", "deepseek", "whale",
            ),
            (
                "m-whale-new", "c-whale", "Whale plan", 1, "user", 300,
                "2026-07-21T00:05:00Z", "alpha appears once and beta appears once", "deepseek", "whale",
            ),
            (
                "m-general", "c-general", "General discussion", 0, "assistant", 700,
                "2026-07-21T00:11:40Z", "alpha beta", "chatgpt", "general",
            ),
            (
                "m-archive", "c-archive", "Archived notes", 0, "assistant", 500,
                "2026-07-21T00:08:20Z", "alpha beta archive", "deepseek", "archive",
            ),
            (
                "m-title-only", "c-title-only", "alpha beta title only", 0, "assistant", 900,
                "2026-07-21T00:15:00Z", "body does not match", "chatgpt", "general",
            ),
        ]
        conn.executemany(
            """
            INSERT INTO messages
                (message_id, conversation_id, conversation_title, ordinal, role, create_time,
                 create_time_iso, text, source_platform, memory_space)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            messages,
        )


class MobileSearchHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "mobile-search.db"
        create_search_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_all_spaces_body_hits_are_grouped_with_deterministic_relevance(self) -> None:
        result = search_all_memory_conversations(
            "alpha beta",
            db_path=self.db_path,
            sort="relevance",
            limit=20,
        )

        self.assertEqual(result["total_conversations"], 3)
        self.assertEqual([item["conversation_id"] for item in result["items"]], [
            "c-whale", "c-general", "c-archive",
        ])
        self.assertNotIn("c-title-only", {item["conversation_id"] for item in result["items"]})
        whale = result["items"][0]
        self.assertEqual(whale["best_hit"]["message_id"], "m-whale-best")
        self.assertEqual(whale["best_hit"]["memory_space"], "whale")
        self.assertEqual(whale["match_count"], 2)
        self.assertEqual(whale["other_match_count"], 1)
        self.assertEqual([hit["message_id"] for hit in whale["other_hits"]], ["m-whale-new"])
        self.assertEqual(
            {item["best_hit"]["memory_space"] for item in result["items"]},
            {"whale", "general", "archive"},
        )

    def test_latest_sort_uses_latest_matching_message_per_conversation(self) -> None:
        result = search_all_memory_conversations(
            "alpha beta",
            db_path=self.db_path,
            sort="latest",
            limit=20,
        )

        self.assertEqual([item["conversation_id"] for item in result["items"]], [
            "c-general", "c-archive", "c-whale",
        ])
        self.assertEqual(result["items"][2]["best_hit"]["message_id"], "m-whale-new")

    def test_pagination_is_over_conversation_groups(self) -> None:
        first = search_all_memory_conversations(
            "alpha beta", db_path=self.db_path, sort="relevance", offset=0, limit=1,
        )
        second = search_all_memory_conversations(
            "alpha beta", db_path=self.db_path, sort="relevance", offset=1, limit=1,
        )
        last = search_all_memory_conversations(
            "alpha beta", db_path=self.db_path, sort="relevance", offset=2, limit=1,
        )

        self.assertEqual(first["next_offset"], 1)
        self.assertTrue(first["has_more"])
        self.assertEqual(second["next_offset"], 2)
        self.assertTrue(second["has_more"])
        self.assertIsNone(last["next_offset"])
        self.assertFalse(last["has_more"])
        self.assertEqual(
            [first["items"][0]["conversation_id"], second["items"][0]["conversation_id"], last["items"][0]["conversation_id"]],
            ["c-whale", "c-general", "c-archive"],
        )


class MobileSearchApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "mobile-search-api.db"
        create_search_db(self.db_path)
        self.handler = object.__new__(local_web.AyunOSHandler)
        self.handler.app = local_web.App(self.db_path.resolve(), real=False, readonly=True)
        self.responses: list[tuple[dict, HTTPStatus]] = []
        self.handler.send_json = lambda payload, status=HTTPStatus.OK: self.responses.append((payload, status))
        self.handler.send_error_json = lambda message, status=HTTPStatus.BAD_REQUEST: self.responses.append(
            ({"error": message}, status)
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call_search(self, **params: str) -> tuple[dict, HTTPStatus]:
        self.responses.clear()
        self.handler.api_search({name: [value] for name, value in params.items()})
        self.assertEqual(len(self.responses), 1)
        return self.responses[0]

    def test_opt_in_api_returns_group_contract_and_filters_source_before_page(self) -> None:
        payload, status = self.call_search(
            q="alpha beta",
            scope="all",
            aggregate="conversation",
            sort="latest",
            source_platform="deepseek",
            offset="0",
            limit="1",
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload["total_conversations"], 2)
        self.assertEqual(payload["items"][0]["conversation_id"], "c-archive")
        self.assertEqual(payload["items"][0]["best_hit"]["source_platform"], "deepseek")
        self.assertEqual(payload["items"][0]["best_hit"]["source_platform_label"], "DeepSeek")
        self.assertEqual(payload["items"][0]["best_hit"]["source"], "message")
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["next_offset"], 1)
        self.assertEqual(payload["sort"], "latest")
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(payload["offset"], 0)

    @patch.object(local_web, "search_all_memory_conversations")
    @patch.object(local_web, "retrieve")
    def test_legacy_search_contract_remains_default(self, retrieve, enhanced_search) -> None:
        retrieve.return_value = [
            {
                "hit_source": "message",
                "title": "Whale plan",
                "update_time_iso": "2026-07-21T00:01:40Z",
                "role": "assistant",
                "snippet": "alpha beta",
                "conversation_id": "c-whale",
                "message_id": "m-whale-best",
                "source_platform": "deepseek",
                "memory_space": "whale",
            }
        ]

        payload, status = self.call_search(q="alpha beta", scope="all")

        self.assertEqual(status, HTTPStatus.OK)
        enhanced_search.assert_not_called()
        retrieve.assert_called_once_with(
            "alpha beta", top_k=20, memory_space="whale", db_path=self.db_path.resolve(),
        )
        self.assertEqual(set(payload), {"items", "memory_space"})
        self.assertEqual(payload["memory_space"], "whale")
        self.assertEqual(payload["items"][0]["message_id"], "m-whale-best")

    def test_opt_in_api_rejects_invalid_sort_offset_and_limit(self) -> None:
        cases = [
            ({"sort": "random", "offset": "0", "limit": "20"}, "Invalid search sort"),
            ({"sort": "relevance", "offset": "-1", "limit": "20"}, "Invalid search offset"),
            ({"sort": "relevance", "offset": "0", "limit": "21"}, "Invalid search limit"),
        ]
        for values, expected_error in cases:
            with self.subTest(values=values):
                payload, status = self.call_search(
                    q="alpha beta", scope="all", aggregate="conversation", **values,
                )
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertEqual(payload["error"], expected_error)


if __name__ == "__main__":
    unittest.main()
