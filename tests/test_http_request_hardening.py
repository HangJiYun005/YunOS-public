from contextlib import closing, redirect_stdout
from email.message import Message
from http import HTTPStatus
from io import BytesIO, StringIO
from pathlib import Path
import sqlite3
import socket
import threading
from http.server import ThreadingHTTPServer
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tools.local_web import AyunOSHandler, MARKS_SQL


class HttpRequestHardeningTest(unittest.TestCase):
    def handler(self, body=b"{}", length="2", readonly=False):
        handler = object.__new__(AyunOSHandler)
        handler.path = "/api/conversation/tag"
        handler.app = SimpleNamespace(
            readonly=readonly, runtime_review=False,
            profile_admin=False, runtime_admin=False,
        )
        handler.headers = Message()
        handler.headers["Content-Length"] = length
        handler.rfile = BytesIO(body)
        handler.send_error_json = Mock()
        handler.handle_api_post = Mock()
        return handler

    def test_invalid_lengths_are_rejected_before_read(self):
        for value in ("abc", "-1", "+2", "2.0", "２", "9" * 5000):
            with self.subTest(value=value[:20]):
                handler = self.handler(length=value)
                handler.rfile = Mock()
                handler.do_POST()
                handler.send_error_json.assert_called_once_with("Invalid Content-Length")
                handler.rfile.read.assert_not_called()
                handler.handle_api_post.assert_not_called()
                self.assertTrue(handler.close_connection)

    def test_duplicate_length_and_transfer_encoding_are_rejected(self):
        for name, value in (("Content-Length", "2"), ("Transfer-Encoding", "chunked")):
            handler = self.handler()
            handler.headers[name] = value
            handler.rfile = Mock()
            handler.do_POST()
            handler.send_error_json.assert_called_once()
            handler.rfile.read.assert_not_called()
            handler.handle_api_post.assert_not_called()

    def test_oversized_body_is_rejected_before_read(self):
        handler = self.handler(length=str(AyunOSHandler.MAX_REQUEST_BODY_BYTES + 1))
        handler.rfile = Mock()
        handler.do_POST()
        handler.send_error_json.assert_called_once_with(
            "Request body too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        )
        handler.rfile.read.assert_not_called()

    def test_bad_encoding_json_and_incomplete_body_never_dispatch(self):
        for body, length, error in (
            (b"\xff", "1", "Invalid JSON"),
            (b"{", "1", "Invalid JSON"),
            (b"{}", "3", "Incomplete request body"),
        ):
            handler = self.handler(body, length)
            handler.do_POST()
            handler.send_error_json.assert_called_once_with(error)
            handler.handle_api_post.assert_not_called()

    def test_readonly_rejects_without_waiting_for_body(self):
        handler = self.handler(readonly=True)
        handler.rfile = Mock()
        handler.do_POST()
        handler.rfile.read.assert_not_called()
        handler.send_error_json.assert_called_once_with(
            "Readonly mode: writes are disabled", HTTPStatus.FORBIDDEN,
        )
        self.assertTrue(handler.close_connection)

    def test_valid_body_dispatches(self):
        handler = self.handler()
        handler.do_POST()
        handler.send_error_json.assert_not_called()
        handler.handle_api_post.assert_called_once_with(handler.path, {})

    def test_live_readonly_rejection_does_not_wait_for_missing_body(self):
        class ReadonlyHandler(AyunOSHandler):
            app = SimpleNamespace(
                readonly=True, runtime_review=False,
                profile_admin=False, runtime_admin=False,
            )

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), ReadonlyHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(
                    b"POST /api/conversation/tag HTTP/1.1\r\n"
                    b"Host: localhost\r\nContent-Length: 1000\r\n\r\n"
                )
                response = bytearray()
                while chunk := client.recv(4096):
                    response.extend(chunk)
                self.assertIn(b"403 Forbidden", response)
                self.assertIn(b"Readonly mode", response)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_access_log_excludes_query_and_fragment(self):
        handler = self.handler()
        handler.path = "/api/search?q=private-search&token=private-token#private-fragment"
        handler.command = "GET"
        handler.client_address = ("127.0.0.1", 12345)
        output = StringIO()
        with redirect_stdout(output):
            handler.log_request(200, 42)
        self.assertIn("GET /api/search", output.getvalue())
        self.assertIn("200 42", output.getvalue())
        self.assertNotIn("private-", output.getvalue())

    def test_tag_retries_are_idempotent_without_schema_migration(self):
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "tags.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    "CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY);"
                    "INSERT INTO conversations VALUES ('one');" + MARKS_SQL
                )
            opened = []

            def connect():
                connection = sqlite3.connect(database)
                opened.append(connection)
                return connection

            handler = self.handler()
            handler.app = SimpleNamespace(connect=connect, db_mode="test")
            handler.send_json = Mock()
            try:
                for tags in (["alpha", "alpha"], ["alpha", "beta"], ["beta"]):
                    handler.api_add_tag({"conversation_id": "one", "tags": tags})
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(
                        [("alpha", 1), ("beta", 1)],
                        connection.execute(
                            "SELECT tag, count(*) FROM conversation_tags GROUP BY tag ORDER BY tag"
                        ).fetchall(),
                    )
                handler.send_error_json.assert_not_called()
            finally:
                for connection in opened:
                    connection.close()


if __name__ == "__main__":
    unittest.main()
