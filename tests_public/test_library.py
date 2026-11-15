import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from public_app.server import Library, Server, decode_text


def file(name, text, encoding='utf-8'):
    return {'name': name, 'data': base64.b64encode(text.encode(encoding)).decode()}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.library = Library(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, **payload):
        preview = self.library.preview(payload)
        return self.library.commit(preview['preview_id'])

    def test_first_run_paste_chinese_search_and_restart(self):
        result = self.save(text='微信复制来的文字\n下周一起讨论项目。', source='微信')
        self.assertEqual(result['added'], 1)
        new = Library(self.temp.name)
        self.assertEqual(new.search('讨论')['total'], 1)
        self.assertEqual(new.get(result['ids'][0])['sources'][0]['label'], '微信')
        self.assertEqual(new.search('不存在')['total'], 0)

    def test_same_filename_different_body_is_not_overwritten(self):
        self.save(files=[file('记录.txt', '第一份内容'), file('记录.txt', '第二份内容')])
        self.assertEqual(self.library.count(), 2)

    def test_duplicate_within_batch_across_encodings_and_newline(self):
        result = self.save(files=[file('a.txt', '中文\r\n记录'), file('b.md', '中文\n记录', 'gb18030')])
        self.assertEqual((result['added'], result['duplicates']), (1, 1))
        self.assertEqual(len(self.library.get(result['ids'][0])['sources']), 2)

    def test_duplicate_across_sources_preserves_provenance(self):
        self.save(text='同一份文字', source='微信')
        result = self.save(text='同一份文字', source='网页')
        self.assertEqual((result['added'], result['duplicates']), (0, 1))
        self.assertEqual(len(self.library.get(result['ids'][0])['sources']), 2)

    def test_repeated_commit_and_concurrent_previews(self):
        keys = [self.library.preview({'text': '并发重复'})['preview_id'] for _ in range(4)]
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(self.library.commit, keys))
        self.assertEqual(sum(r['added'] for r in results), 1)
        self.assertEqual(self.library.commit(keys[0]), results[0])
        self.assertEqual(self.library.count(), 1)

    def test_preview_never_writes_and_expiry(self):
        p = self.library.preview({'text': '尚未确认'})
        self.assertEqual(self.library.count(), 0)
        self.library.previews[p['preview_id']]['time'] = time.monotonic() - 601
        with self.assertRaises(ValueError):
            self.library.commit(p['preview_id'])

    def test_invalid_files_reported_without_losing_valid_files(self):
        p = self.library.preview({'files': [file('ok.txt', '有效文字'), file('empty.txt', ''), file('image.png', 'not supported'), file('binary.txt', '\x00bad')]})
        self.assertEqual([r['status'] for r in p['rows']], ['ready', 'error', 'error', 'error'])
        result = self.library.commit(p['preview_id'])
        self.assertEqual((result['added'], result['failed']), (1, 3))

    def test_bom_gb18030_and_invalid_utf8(self):
        for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
            self.assertEqual(decode_text('中文测试'.encode(encoding))[0], '中文测试')
        with self.assertRaises(ValueError):
            decode_text(b'\xff', 'utf-8')

    def test_size_limits_and_bad_payload(self):
        with self.assertRaises(ValueError):
            decode_text(b'a' * (5 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.library.preview({'files': []})
        with self.assertRaises(ValueError):
            self.library.preview({'files': ['bad']})

    def test_transaction_rolls_back_on_failure(self):
        with self.library.connect() as db:
            db.execute("CREATE TRIGGER fail_source BEFORE INSERT ON sources BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        key = self.library.preview({'text': '不能部分写入'})['preview_id']
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.commit(key)
        self.assertEqual(self.library.count(), 0)

    def test_search_literal_wildcards_and_pagination(self):
        self.save(text='计划完成 100% _check_')
        self.save(text='其他资料')
        self.assertEqual(self.library.search('%')['total'], 1)
        self.assertEqual(self.library.search('_')['total'], 1)
        self.assertEqual(self.library.search("' OR 1=1 --")['total'], 0)
        self.assertEqual(len(self.library.search(offset=1)['items']), 1)

    def test_backup_contains_current_committed_data(self):
        self.save(text='备份原文', source='微信')
        backup = Path(self.temp.name) / 'backup.sqlite'
        backup.write_bytes(self.library.backup())
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT body FROM documents').fetchone()[0], '备份原文')
        db.close()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(Library(self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def request(self, path, payload=None, extra=None):
        headers = {'Content-Type': 'application/json', 'X-YunOS-Token': self.server.token}
        headers.update(extra or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request('POST' if payload is not None else 'GET', path,
                     json.dumps(payload).encode() if payload is not None else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_http_end_to_end(self):
        status, headers, _ = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        status, _, body = self.request('/api/preview', {'text': 'HTTP 导入测试'})
        self.assertEqual(status, 200)
        key = json.loads(body)['preview_id']
        status, _, body = self.request('/api/import', {'preview_id': key})
        self.assertEqual(json.loads(body)['added'], 1)
        status, _, body = self.request('/api/items?q=HTTP')
        self.assertEqual(json.loads(body)['total'], 1)

    def test_cross_origin_host_and_csrf_are_rejected(self):
        self.assertEqual(self.request('/api/preview', {'text': 'x'}, {'Origin': 'https://example.org'})[0], 403)
        self.assertEqual(self.request('/api/preview', {'text': 'x'}, {'X-YunOS-Token': 'bad'})[0], 403)
        self.assertEqual(self.request('/api/session', extra={'Host': 'example.org'})[0], 403)
        self.assertEqual(self.request('/api/session', extra={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.server.library.count(), 0)

    def test_invalid_json_shape_and_path(self):
        self.assertEqual(self.request('/api/preview', ['bad'])[0], 400)
        self.assertEqual(self.request('/../server.py')[0], 404)
        self.assertEqual(self.request('/api/items/not-a-number')[0], 400)


if __name__ == '__main__':
    unittest.main()
