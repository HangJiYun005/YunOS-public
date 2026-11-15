import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import urlopen

from tests.test_whale_context_optimizer import create_optimizer_db, database_counts


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ContextOptimizerApiTest(unittest.TestCase):
    def test_api_requires_input_and_returns_json_400(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            with local_server(db_path) as base_url:
                with self.assertRaises(HTTPError) as ctx:
                    fetch_json(f"{base_url}/api/whale-context-optimizer")
                body = json.loads(ctx.exception.read().decode("utf-8"))

        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(body["error"], "Missing q or input")

    def test_api_returns_optimizer_summary_without_prompt(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/whale-context-optimizer?q=alpha")

        self.assertEqual(data["candidate_count"], 16)
        self.assertIn("recommended_config", data)
        self.assertEqual(len(data["top_configs"]), 5)
        self.assertIn("reason", data)
        self.assertNotIn("prompt", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("[System]", json.dumps(data, ensure_ascii=False))

    def test_api_defaults_test_general_false(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/whale-context-optimizer?q=alpha&limit=10")

        self.assertEqual({item["include_general"] for item in data["top_configs"]}, {False})
        self.assertEqual(data["recommended_config"]["include_general"], False)

    def test_api_test_general_true_can_return_general_candidates(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/whale-context-optimizer?q=alpha&test_general=true&limit=10")

        self.assertGreater(data["candidate_count"], 16)
        self.assertIn(True, {item["include_general"] for item in data["candidates"]})

    def test_api_returns_readonly_non_applied_model_flags(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/whale-context-optimizer?input=alpha")

        self.assertEqual(data["readonly"], True)
        self.assertEqual(data["applied"], False)
        self.assertEqual(data["sent_to_model"], False)

    def test_api_does_not_write_database_or_expose_paths(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)
            before = database_counts(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/whale-context-optimizer?q=alpha")

            after = database_counts(db_path)

        self.assertEqual(after, before)
        self.assertNotIn(str(db_path), json.dumps(data, ensure_ascii=False))
        self.assertNotIn(str(PROJECT_ROOT), json.dumps(data, ensure_ascii=False))

    def test_api_rejects_invalid_parameters_with_json_400(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            with local_server(db_path) as base_url:
                with self.assertRaises(HTTPError) as ctx:
                    fetch_json(f"{base_url}/api/whale-context-optimizer?q=alpha&limit=11")
                body = json.loads(ctx.exception.read().decode("utf-8"))

        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("limit", body["error"])

    def test_frontend_has_optimizer_entry_without_optimizer_post(self):
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        app = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn(
            '{ key: "contextOptimizer", labelKey: "context.optimizer", fallback: "上下文优化器", view: "contextOptimizer" }',
            app,
        )
        self.assertIn("whale-context-optimizer", app)
        self.assertIn("function loadContextOptimizer", app)
        optimizer_slice = app[app.index("function loadContextOptimizer") : app.index("const views")]
        self.assertNotIn("POST", optimizer_slice)
        self.assertNotIn("应用配置", optimizer_slice)
        self.assertNotIn("保存默认配置", optimizer_slice)
        self.assertNotIn("发送给模型", optimizer_slice)


class local_server:
    def __init__(self, db_path: Path):
        from tools.local_web import App, AyunOSHandler

        self.handler = AyunOSHandler
        self.handler.app = App(db_path.resolve(), real=False, readonly=True)
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


def fetch_json(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
