from __future__ import annotations

import json
from io import BytesIO
import threading
from types import SimpleNamespace
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from tests.test_semantic_search_preview import create_preview_semantic_db


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_ROOT = PROJECT_ROOT / "data" / "test"


class SemanticSearchPreviewApiTest(unittest.TestCase):
    def test_api_returns_summary_results(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            semantic_db = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(semantic_db)

            with local_server(semantic_db) as base_url:
                data = fetch_json(f"{base_url}/api/semantic-summary-search-preview?q={quote('鲸鱼')}")

        self.assertTrue(data["readonly"])
        self.assertFalse(data["sent_to_model"])
        self.assertTrue(data["provider_is_test_only"])
        self.assertEqual("deterministic_test", data["provider"])
        self.assertEqual(2, data["candidate_count"])
        self.assertGreater(data["result_count"], 0)
        self.assertIn("score", data["results"][0])
        self.assertIn("content_preview", data["results"][0])
        self.assertIn("summary_type", data["results"][0])
        self.assertNotIn("embedding_json", json.dumps(data, ensure_ascii=False))

    def test_api_filters_memory_space_and_clamps_top_k(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            semantic_db = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(semantic_db)

            with local_server(semantic_db) as base_url:
                data = fetch_json(
                    f"{base_url}/api/semantic-summary-search-preview?query={quote('鲸鱼')}&memory_space=whale&top_k=99"
                )

        self.assertEqual("whale", data["memory_space"])
        self.assertEqual(20, data["top_k"])
        self.assertEqual(1, data["candidate_count"])

    def test_api_min_score_filters_results(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            semantic_db = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(semantic_db)

            with local_server(semantic_db) as base_url:
                data = fetch_json(f"{base_url}/api/semantic-summary-search-preview?q={quote('鲸鱼')}&min_score=1")

        self.assertEqual(0, data["result_count"])
        self.assertEqual(1.0, data["min_score"])

    def test_api_ignores_client_db_path_and_does_not_expose_paths(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            semantic_db = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(semantic_db)

            with local_server(semantic_db) as base_url:
                data = fetch_json(
                    f"{base_url}/api/semantic-summary-search-preview?q={quote('鲸鱼')}&db=C:/private/other.db"
                )

        encoded = json.dumps(data, ensure_ascii=False)
        self.assertEqual(2, data["candidate_count"])
        self.assertNotIn(str(semantic_db), encoded)
        self.assertNotIn("C:/private/other.db", encoded)

    def test_api_missing_semantic_db_or_table_returns_clear_status(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            semantic_db = Path(tmp) / "missing_test.db"

            with local_server(semantic_db) as base_url:
                data = fetch_json(f"{base_url}/api/semantic-summary-search-preview?q=hello")

        self.assertTrue(data["readonly"])
        self.assertEqual(0, data["candidate_count"])
        self.assertEqual(0, data["result_count"])
        self.assertIn("readonly_error", data)

    def test_api_rejects_missing_query_with_json_400(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            semantic_db = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(semantic_db)

            with local_server(semantic_db) as base_url:
                with self.assertRaises(HTTPError) as ctx:
                    fetch_json(f"{base_url}/api/semantic-summary-search-preview")
                body = json.loads(ctx.exception.read().decode("utf-8"))

        self.assertEqual(400, ctx.exception.code)
        self.assertEqual("Missing q or query", body["error"])

    def test_api_does_not_accept_post(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            semantic_db = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(semantic_db)

            with local_server(semantic_db) as base_url:
                request = Request(
                    f"{base_url}/api/semantic-summary-search-preview",
                    data=b'{"q":"hello"}',
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(request, timeout=5)

        self.assertEqual(403, ctx.exception.code)

    def test_readonly_post_closes_connection_without_consuming_body(self):
        import tools.local_web as local_web

        body = b'{"q":"hello"}'
        handler = local_web.AyunOSHandler.__new__(local_web.AyunOSHandler)
        handler.path = "/api/semantic-summary-search-preview"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler.app = SimpleNamespace(
            readonly=True,
            runtime_review=False,
            profile_admin=False,
            runtime_admin=False,
        )
        body_positions = []
        handler.send_error_json = Mock(
            side_effect=lambda *_args, **_kwargs: body_positions.append(
                handler.rfile.tell()
            )
        )

        handler.do_POST()

        self.assertEqual([0], body_positions)
        self.assertTrue(handler.close_connection)
        handler.send_error_json.assert_called_once_with(
            "Readonly mode: writes are disabled",
            local_web.HTTPStatus.FORBIDDEN,
        )

    def test_local_web_does_not_add_semantic_preview_post_or_page_wiring(self):
        text = (PROJECT_ROOT / "tools" / "local_web.py").read_text(encoding="utf-8")
        post_block = text[text.index("def handle_api_post") : text.index("def api_recent")]
        self.assertNotIn("semantic-summary-search-preview", post_block)
        self.assertIn("semantic-summary-search-preview", text)
        self.assertNotIn("semantic-summary-search-preview", (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8"))


class local_server:
    def __init__(self, semantic_db_path: Path):
        import tools.local_web as local_web

        self.previous = getattr(local_web, "SEMANTIC_SUMMARY_DB_PATH", None)
        local_web.SEMANTIC_SUMMARY_DB_PATH = semantic_db_path.resolve()
        self.local_web = local_web
        self.handler = local_web.AyunOSHandler
        self.handler.app = local_web.App((PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db").resolve(), real=False, readonly=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.previous is not None:
            self.local_web.SEMANTIC_SUMMARY_DB_PATH = self.previous


def fetch_json(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
