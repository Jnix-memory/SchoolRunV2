#!/usr/bin/env python3
"""
校园跑步数据下载服务器 - 后端主程序
基于 Python http.server 的多线程 HTTP 服务
"""

import http.server
import socketserver
import json
import os
import sys
import sqlite3
import threading

PORT = 5005
DB_PATH = 'data/activities.db'
db_lock = threading.Lock()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                duration TEXT NOT NULL,
                distance REAL,
                visitor_ip TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 兼容旧库：已有表补 distance 列
        try:
            conn.execute('ALTER TABLE activities ADD COLUMN distance REAL')
        except sqlite3.OperationalError:
            pass
        conn.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                visitor_ip TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()


def insert_activity(user_id, date, start_time, duration, visitor_ip, distance=None):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "INSERT INTO activities (user_id, date, start_time, duration, distance, visitor_ip) VALUES (?,?,?,?,?,?)",
            (user_id, date, start_time, duration, distance, visitor_ip)
        )
        aid = cur.lastrowid
        conn.commit()
        conn.close()
        return aid


def get_activities():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, user_id, date, start_time, duration, distance, visitor_ip, created_at FROM activities ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [
            {'id': r[0], 'user_id': r[1], 'date': r[2], 'start_time': r[3],
             'duration': r[4], 'distance': r[5], 'visitor_ip': r[6], 'created_at': r[7]}
            for r in rows
        ]


def insert_download(user_id, visitor_ip):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO downloads (user_id, visitor_ip) VALUES (?,?)",
            (user_id, visitor_ip)
        )
        conn.commit()
        conn.close()


class Handler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        pages = {
            '/': 'frontend/form.html',
            '/index.html': 'frontend/form.html',
            '/form.html': 'frontend/form.html',
            '/download.html': 'frontend/form.html',
        }
        if self.path in pages:
            self._file(pages[self.path], 'text/html')
        elif self.path == '/api/activities':
            self._json(get_activities())
        elif self.path == '/api/announcement':
            self._announcement()
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            data = self._read_body()
            if data is None:
                return
            if self.path == '/api/submit':
                self._submit(data)
            elif self.path == '/api/generate':
                self._generate(data)
            else:
                self.send_error(404)
        except Exception as e:
            print(f"Error: {e}")
            self._json({'error': '服务器内部错误'}, 500)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def end_headers(self):
        self._cors()
        super().end_headers()

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _read_body(self):
        try:
            n = int(self.headers.get('Content-Length', 0))
            return json.loads(self.rfile.read(n)) if n else {}
        except (json.JSONDecodeError, ValueError):
            self._json({'error': '无效的 JSON'}, 400)
            return None

    def _json(self, obj, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

    def _file(self, path, ct):
        try:
            with open(path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', f'{ct}; charset=utf-8')
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _announcement(self):
        path = os.path.join('data', 'Call.txt')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.read().split('\n', 1)
            version = lines[0].strip() if lines else ''
            content = lines[1].strip() if len(lines) > 1 else ''
            self._json({'version': version, 'content': content})
        except FileNotFoundError:
            self._json({'version': '', 'content': ''})

    def _submit(self, d):
        uid, date, st, dur = d.get('user_id'), d.get('date'), d.get('start_time'), d.get('duration')
        if not all([uid, date, st, dur]):
            self._json({'error': '缺少必要参数'}, 400)
            return
        try:
            km = self._opt_distance(d)
        except ValueError as e:
            self._json({'error': str(e)}, 400)
            return
        ip = self.client_address[0]
        aid = insert_activity(uid, date, st, dur, ip, km)
        self._json({'success': True, 'id': aid, 'distance': km})

    def _opt_distance(self, d):
        """可选距离(公里)：空值->None；否则 0<km<=100。"""
        raw = d.get('distance')
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        try:
            km = float(raw)
        except (TypeError, ValueError):
            raise ValueError('距离格式不正确')
        if not (0 < km <= 100):
            raise ValueError('距离应在0~100公里之间')
        return km

    def _generate(self, d):
        uid, date, st, dur = d.get('user_id'), d.get('date'), d.get('start_time'), d.get('duration')
        if not all([uid, date, st, dur]):
            self._json({'error': '缺少必要参数'}, 400)
            return
        try:
            km = self._opt_distance(d)
        except ValueError as e:
            self._json({'error': str(e)}, 400)
            return
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from tools.generate_fit import generate_fit
        out = f'data/{uid}.fit'
        ok = generate_fit(uid, date, st, dur, out, distance=km)
        if ok and os.path.exists(out):
            ip = self.client_address[0]
            insert_download(uid, ip)
            with open(out, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', f'attachment; filename={uid}.fit')
            self.end_headers()
            self.wfile.write(content)
            os.remove(out)
        else:
            self._json({'error': '文件生成失败'}, 500)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    init_db()
    with ThreadedHTTPServer(('', PORT), Handler) as srv:
        print(f"服务已启动: http://localhost:{PORT}")
        print(f"数据填写: http://localhost:{PORT}/form.html")
        print(f"数据下载: http://localhost:{PORT}/download.html")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止")


if __name__ == '__main__':
    main()
