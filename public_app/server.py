"""Standalone local text library. Python standard library only."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser

VERSION = '0.2.2'
MAX_FILE = 5 * 1024 * 1024
MAX_BATCH = 10 * 1024 * 1024
MAX_REQUEST = 16 * 1024 * 1024
STATIC = Path(__file__).resolve().parent / 'web'


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def open_window(url, directory):
    """Use an installed browser's app window; no Electron or Node installation."""
    if os.name == 'nt':
        candidates = []
        for variable in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
            root = os.environ.get(variable)
            if root:
                candidates.extend([Path(root) / 'Microsoft/Edge/Application/msedge.exe',
                                   Path(root) / 'Google/Chrome/Application/chrome.exe'])
        for browser in candidates:
            if browser.is_file():
                try:
                    subprocess.Popen([str(browser), '--app=' + url, '--new-window',
                                      '--user-data-dir=' + str(Path(directory).parent / 'browser-profile'),
                                      '--no-first-run'], creationflags=subprocess.CREATE_NO_WINDOW)
                    return
                except OSError:
                    continue
    webbrowser.open(url)


def canonical(text):
    return unicodedata.normalize('NFC', text.replace('\r\n', '\n').replace('\r', '\n')).lstrip('\ufeff')


def decode_text(raw, encoding='auto'):
    if len(raw) > MAX_FILE:
        raise ValueError('单个文件不能超过 5 MB')
    if encoding not in ('auto', 'utf-8', 'gb18030', 'utf-16'):
        raise ValueError('不支持的编码')
    if encoding == 'auto':
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            encoding = 'utf-16'
        else:
            try:
                raw.decode('utf-8-sig')
                encoding = 'utf-8-sig'
            except UnicodeDecodeError:
                encoding = 'gb18030'
    try:
        text = canonical(raw.decode(encoding))
    except (UnicodeError, LookupError):
        raise ValueError('无法读取文字，请选择正确编码或另存为 UTF-8') from None
    if any(ord(c) < 32 and c not in '\n\t' for c in text):
        raise ValueError('文件含非文本内容，请使用 TXT 或 Markdown')
    if not text.strip():
        raise ValueError('内容为空，未导入')
    return text, encoding


class Library:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'library.sqlite'
        self.lock = threading.RLock()
        self.previews = {}
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    document_id INTEGER NOT NULL REFERENCES documents(id),
                    label TEXT NOT NULL, filename TEXT NOT NULL,
                    UNIQUE(document_id, label, filename)
                );
                PRAGMA user_version=1;
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20, factory=Connection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def count(self):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]

    def preview(self, payload):
        entries = payload.get('files')
        source = str(payload.get('source', '其他'))[:80].strip() or '其他'
        encoding = payload.get('encoding', 'auto')
        if entries is None:
            text = payload.get('text', '')
            if not isinstance(text, str):
                raise ValueError('请输入文字')
            entries = [{'name': '', 'text': text, 'title': payload.get('title', '')}]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
            raise ValueError('每批请选择 1–50 个文件')
        rows, seen, total = [], set(), 0
        with self.lock, self.connect() as db:
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError('文件列表格式错误')
                name = str(entry.get('name', '')).replace('\\', '/').split('/')[-1][:240]
                row = {'name': name or '粘贴的文字', 'source': source, 'filename': name}
                try:
                    if 'text' in entry:
                        if not isinstance(entry['text'], str):
                            raise ValueError('文字格式错误')
                        raw = entry['text'].encode('utf-8')
                        body, used_encoding = decode_text(raw, 'utf-8')
                    else:
                        if Path(name).suffix.lower() not in ('.txt', '.md', '.markdown'):
                            raise ValueError('暂不支持此格式，请使用 TXT 或 Markdown')
                        try:
                            raw = base64.b64decode(entry.get('data', ''), validate=True)
                        except (ValueError, TypeError):
                            raise ValueError('文件读取失败，请重新选择') from None
                        body, used_encoding = decode_text(raw, encoding)
                    total += len(raw)
                    if total > MAX_BATCH:
                        raise ValueError('每批总大小不能超过 10 MB，请分批导入')
                    title = str(entry.get('title', '')).strip()[:200]
                    title = title or (Path(name).stem if name else body.strip().split('\n')[0][:80])
                    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
                    existing = db.execute('SELECT id FROM documents WHERE digest=?', (digest,)).fetchone()
                    status = 'duplicate' if existing or digest in seen else 'ready'
                    seen.add(digest)
                    row.update(title=title, body=body, digest=digest, status=status,
                               characters=len(body), encoding=used_encoding,
                               detail='正文已存在，将跳过正文并补充来源' if status == 'duplicate' else '可以导入')
                except ValueError as exc:
                    row.update(status='error', detail=str(exc))
                rows.append(row)
            if total > MAX_BATCH:
                raise ValueError('每批总大小不能超过 10 MB，请分批导入')
            now = time.monotonic()
            self.previews = {k: v for k, v in self.previews.items() if now - v['time'] < 600}
            if len(self.previews) >= 4:
                oldest = min(self.previews, key=lambda k: self.previews[k]['time'])
                del self.previews[oldest]
            key = secrets.token_urlsafe(24)
            self.previews[key] = {'rows': rows, 'time': now}
            return {'preview_id': key, 'rows': [
                {**{k: v for k, v in row.items() if k not in ('body', 'digest', 'filename')},
                 'excerpt': row.get('body', '')[:500]} for row in rows]}

    def commit(self, key):
        with self.lock:
            preview = self.previews.get(key)
            if not preview or time.monotonic() - preview['time'] > 600:
                raise ValueError('预览已过期，请重新检查')
            if 'result' in preview:
                return preview['result']
            added = duplicates = failed = 0
            ids = []
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for row in preview['rows']:
                    if row['status'] == 'error':
                        failed += 1
                        continue
                    cursor = db.execute(
                        'INSERT OR IGNORE INTO documents(title,body,digest,created_at) VALUES(?,?,?,?)',
                        (row['title'], row['body'], row['digest'], time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
                    added += cursor.rowcount
                    duplicates += 1 - cursor.rowcount
                    doc_id = db.execute('SELECT id FROM documents WHERE digest=?', (row['digest'],)).fetchone()[0]
                    ids.append(doc_id)
                    db.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?)',
                               (doc_id, row['source'], row['filename']))
            result = {'added': added, 'duplicates': duplicates, 'failed': failed, 'ids': list(dict.fromkeys(ids))}
            preview['result'] = result
            preview['rows'] = []
            return result

    def search(self, query='', offset=0):
        term = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        params = ('%' + term + '%',) * 2
        clause = "title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\'"
        with self.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM documents WHERE ' + clause, params).fetchone()[0]
            rows = db.execute('SELECT id,title,body,created_at FROM documents WHERE ' + clause +
                              ' ORDER BY id DESC LIMIT 40 OFFSET ?', (*params, offset)).fetchall()
            items = []
            for row in rows:
                doc = dict(row)
                body = doc.pop('body')
                start = max(0, body.casefold().find(query.casefold()) - 60) if query else 0
                doc['excerpt'] = body[start:start + 240]
                doc['characters'] = len(body)
                doc['sources'] = [dict(s) for s in db.execute('SELECT label,filename FROM sources WHERE document_id=?', (doc['id'],))]
                items.append(doc)
            return {'items': items, 'total': count}

    def get(self, doc_id):
        with self.connect() as db:
            row = db.execute('SELECT id,title,body,created_at FROM documents WHERE id=?', (doc_id,)).fetchone()
            if not row:
                raise ValueError('资料不存在')
            doc = dict(row)
            doc['sources'] = [dict(s) for s in db.execute('SELECT label,filename FROM sources WHERE document_id=?', (doc_id,))]
            return doc

    def backup(self):
        # SQLite backup API includes committed WAL contents and never copies a live file unsafely.
        with self.connect() as source, sqlite3.connect(':memory:', factory=Connection) as target:
            source.backup(target)
            return target.serialize()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, library, port=0):
        self.library = library
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def respond(self, body, status=200, content_type='application/json; charset=utf-8', attachment=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        if attachment:
            self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        return (self.headers.get('Host') == urlsplit(self.server.origin).netloc
                and self.headers.get('Origin', self.server.origin) == self.server.origin
                and self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('cross-site', 'same-site'))

    def do_GET(self):
        if not self.allowed():
            return self.respond({'error': '只允许本机应用访问'}, 403)
        parsed = urlsplit(self.path)
        try:
            if parsed.path == '/api/session':
                return self.respond({'token': self.server.token, 'version': VERSION,
                                     'count': self.server.library.count(), 'data_directory': str(self.server.library.directory)})
            if parsed.path == '/api/items':
                params = parse_qs(parsed.query)
                return self.respond(self.server.library.search(params.get('q', [''])[0][:500],
                                    max(0, int(params.get('offset', ['0'])[0]))))
            if parsed.path.startswith('/api/items/'):
                return self.respond(self.server.library.get(int(parsed.path.rsplit('/', 1)[1])))
            if parsed.path == '/api/backup':
                return self.respond(self.server.library.backup(), content_type='application/octet-stream',
                                    attachment=f'yunos-library-{time.strftime("%Y%m%d-%H%M%S")}.sqlite')
            names = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if parsed.path in names:
                name, content_type = names[parsed.path]
                return self.respond((STATIC / name).read_bytes(), content_type=content_type)
            return self.respond({'error': '未找到页面'}, 404)
        except (ValueError, OverflowError):
            return self.respond({'error': '查询参数无效或资料不存在'}, 400)
        except (OSError, sqlite3.Error):
            return self.respond({'error': '读取失败，请检查资料目录和磁盘空间'}, 500)

    def do_POST(self):
        if not self.allowed() or self.headers.get('X-YunOS-Token') != self.server.token:
            self.close_connection = True
            return self.respond({'error': '会话失效，请刷新页面'}, 403)
        try:
            sizes = self.headers.get_all('Content-Length', [])
            if len(sizes) != 1 or not sizes[0].isascii() or not sizes[0].isdecimal() or self.headers.get('Transfer-Encoding'):
                raise ValueError('请求长度无效')
            size = int(sizes[0])
            if size > MAX_REQUEST:
                raise ValueError('文件过大，请分批导入')
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('请求格式错误')
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('传输中断，请重试')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('请求格式错误')
            if self.path == '/api/preview':
                return self.respond(self.server.library.preview(payload))
            if self.path == '/api/import':
                return self.respond(self.server.library.commit(str(payload.get('preview_id', ''))))
            if self.path == '/api/shutdown':
                self.respond({'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.respond({'error': '未找到操作'}, 404)
        except (ValueError, TypeError, UnicodeError, TimeoutError) as exc:
            self.close_connection = True
            return self.respond({'error': str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else '数据格式错误或传输中断'}, 400)
        except sqlite3.Error:
            return self.respond({'error': '保存未完成，请检查磁盘空间后重试。已有资料不受影响。'}, 500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default=str(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'YunOS' / 'data'))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    directory = Path(args.data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / 'service.json'
    # Hold a process lock; the database also enforces uniqueness across transactions.
    lock = (directory / 'service.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        import urllib.request
        for _ in range(40):
            try:
                previous = json.loads(state.read_text(encoding='utf-8'))
                url = previous['url']
                parsed = urlsplit(url)
                if parsed.hostname != '127.0.0.1' or parsed.scheme != 'http':
                    raise ValueError('Invalid local service')
                with urllib.request.urlopen(url + '/api/session', timeout=1) as response:
                    active = json.load(response)
                if active.get('token') != previous['token']:
                    raise ValueError('Service identity mismatch')
                if not args.no_browser:
                    open_window(url, directory)
                return
            except (OSError, ValueError, KeyError):
                time.sleep(.25)
        raise SystemExit('YunOS is already starting. Please retry shortly.')
    server = Server(Library(directory), args.port)
    state.write_text(json.dumps({'url': server.origin, 'token': server.token}), encoding='utf-8')
    print(server.origin, flush=True)
    if not args.no_browser:
        open_window(server.origin, directory)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        state.unlink(missing_ok=True)
        lock.close()


if __name__ == '__main__':
    main()
