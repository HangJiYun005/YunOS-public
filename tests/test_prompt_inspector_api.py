import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.test_whale_prompt_preview import create_preview_db


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PromptInspectorApiTest(unittest.TestCase):
    def test_api_requires_input_and_returns_json_400(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            with local_server(db_path) as base_url:
                with self.assertRaises(HTTPError) as ctx:
                    fetch_json(f"{base_url}/api/whale-prompt-preview")
                body = json.loads(ctx.exception.read().decode("utf-8"))

        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(body["error"], "Missing q or input")

    def test_api_rejects_invalid_numeric_parameter_with_json_400(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            with local_server(db_path) as base_url:
                with self.assertRaises(HTTPError) as ctx:
                    fetch_json(f"{base_url}/api/whale-prompt-preview?q=alpha&summary_limit=11")
                body = json.loads(ctx.exception.read().decode("utf-8"))

        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("summary_limit", body["error"])

    def test_api_returns_readonly_prompt_diagnostics_without_full_prompt_by_default(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/whale-prompt-preview?q=alpha")

        self.assertTrue(data["readonly"])
        self.assertFalse(data["sent_to_model"])
        self.assertIn("sections", data)
        self.assertIn("totals", data)
        self.assertIn("ordering", data)
        self.assertNotIn("prompt", data)
        self.assertEqual(data["totals"]["summary_count"], 2)
        self.assertGreater(data["totals"]["raw_memory_count"], 0)
        self.assertEqual(data["totals"]["general_context_count"], 0)
        self.assertEqual(data["ordering"]["summary_before_raw_memory"], True)
        self.assertGreater(data["totals"]["total_chars"], 0)
        self.assertGreater(data["totals"]["estimated_total_tokens"], 0)
        self.assertIn("High-level Memory Summaries - whale", data["ordering"]["section_order"])
        self.assertTrue(all("preview" in section for section in data["sections"]))
        self.assertNotIn(str(db_path), json.dumps(data, ensure_ascii=False))

    def test_api_show_full_prompt_is_required_to_return_prompt(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/whale-prompt-preview?input=alpha&show_full_prompt=true")

        self.assertIn("prompt", data)
        self.assertIn("[User Input]\nalpha", data["prompt"])

    def test_api_include_general_defaults_false_and_can_be_enabled(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            with local_server(db_path) as base_url:
                default_data = fetch_json(f"{base_url}/api/whale-prompt-preview?q=alpha")
                general_data = fetch_json(f"{base_url}/api/whale-prompt-preview?q=alpha&include_general=true")

        self.assertEqual(default_data["totals"]["general_context_count"], 0)
        self.assertEqual(general_data["totals"]["general_context_count"], 1)

    def test_api_endpoint_does_not_accept_post(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            with local_server(db_path) as base_url:
                request = Request(
                    f"{base_url}/api/whale-prompt-preview",
                    method="POST",
                )
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(request, timeout=5)

        self.assertEqual(ctx.exception.code, 403)

    def test_frontend_has_prompt_inspector_entry_without_post_or_send_actions(self):
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        app = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn(
            '{ key: "promptInspector", labelKey: "context.inspector", fallback: "上下文检查器", view: "promptInspector" }',
            app,
        )
        self.assertIn("whale-prompt-preview", app)
        self.assertIn("show_full_prompt", app)
        inspector_slice = app[app.index("function loadPromptInspector") : app.index("function loadContextOptimizer")]
        self.assertNotIn('/api/whale-prompt-preview", { method: "POST"', app)
        self.assertNotIn("发送", inspector_slice)
        self.assertNotIn("生成", inspector_slice)
        self.assertNotIn("保存", inspector_slice)


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
