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
import time
import uuid
from itertools import count

PORT = 5005
DB_PATH = 'data/activities.db'
db_lock = threading.Lock()
# 并发上限：4核/2G 小服务器建议 4~8；超出时请求排队而不是无限开线程
MAX_CONCURRENCY = 8
_sem = threading.BoundedSemaphore(MAX_CONCURRENCY)

# ---- 可感知的生成作业队列（供前端进度条/排队人数） ----
# 下载=三步：POST /api/jobs 注册 -> GET /api/jobs/status?t= 轮询 -> GET /api/jobs/result?t= 取文件
# 生成泵线程按 FIFO 串行出队（GIL 下单进程本就是串行），_processing 表示正在生成中的作业数
_job_cond = threading.Condition()
_job_tickets = count(1)
_jobs = {}                 # ticket -> dict(status/path/params)
_queue = []                # 等待中的 ticket（FIFO）
_processing = 0


def _cleanup_old_temp():
    """清理未被取走的旧 .g_ 临时文件与过期作业（超过 10 分钟）。"""
    now = time.time()
    try:
        for name in os.listdir('data'):
            if name.startswith('.g_') and name.endswith('.fit'):
                p = os.path.join('data', name)
                if now - os.path.getmtime(p) > 600:
                    os.remove(p)
    except OSError:
        pass
    with _job_cond:
        stale = [t for t, j in _jobs.items()
                 if j.get('status') == 'done' and now - (j.get('done_at') or now) > 600]
        for t in stale:
            _jobs.pop(t, None)


def _job_pump():
    """后台泵线程：FIFO 出队 -> 生成 FIT -> 标记完成。"""
    global _processing, _jobs, _queue
    while True:
        with _job_cond:
            while not _queue:
                _job_cond.wait(timeout=1.0)
                if not _queue:
                    continue
                break
            ticket = _queue.pop(0)
            _processing += 1
        job = _jobs.get(ticket)
        if job:
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
                from tools.generate_fit import generate_fit
                out = f'data/.g_{ticket}_{uuid.uuid4().hex}.fit'
                uid, date, st, dur, km = job['params']
                job['ok'] = generate_fit(uid, date, st, dur, out, distance=km)
                job['path'] = out if job.get('ok') else None
            except Exception as e:
                print(f"生成作业异常: {e}")
                job['ok'] = False
                job['path'] = None
            finally:
                job['status'] = 'done'
                job['done_at'] = time.time()
        with _job_cond:
            _processing -= 1
            _job_cond.notify_all()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # 提高连接等待队列(默认只有5)：突发并发时排队而不是立刻拒绝
    request_queue_size = 64
    block_on_close = True

    def process_request(self, request, client_address):
        """先取并发许可再开线程：线程数被钳制在 MAX_CONCURRENCY 内，
        排队请求留在内核连接队列，不会变成一堆空等线程（省内存）。"""
        _sem.acquire()
        try:
            t = threading.Thread(target=self._serve_guarded,
                                 args=(request, client_address))
            t.daemon = True
            t.start()
        except Exception:
            _sem.release()
            self.shutdown_request(request)

    def _serve_guarded(self, request, client_address):
        try:
            self.finish_request(request, client_address)
            self.shutdown_request(request)
        except Exception:
            self.handle_error(request, client_address)
            self.shutdown_request(request)
        finally:
            _sem.release()


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
        self._do_get_core()

    def _do_get_core(self):
        if self.path.startswith('/api/jobs'):
            self._jobs_get()
            return
        pages = {
            '/': 'frontend/form.html',
            '/index.html': 'frontend/form.html',
            '/form.html': 'frontend/form.html',
            '/download.html': 'frontend/form.html',
        }
        if self.path in pages:
            self._file(pages[self.path], 'text/html')
        elif self.path == '/Keep.apk':
            # 前端“下载 Keep 安卓版”按钮指向的安装包（文件放网站根目录）
            self._file('Keep.apk', 'application/vnd.android.package-archive')
        elif self.path == '/api/activities':
            self._json(get_activities())
        elif self.path == '/api/announcement':
            self._announcement()
        else:
            self.send_error(404)

    def do_POST(self):
        self._do_post_core()

    def _do_post_core(self):
        try:
            data = self._read_body()
            if data is None:
                return
            if self.path == '/api/submit':
                self._submit(data)
            elif self.path == '/api/jobs':
                self._job_create(data)
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
                self.send_response(200)
                self.send_header('Content-Type', f'{ct}; charset=utf-8')
                self.send_header('Content-Length', str(os.path.getsize(path)))
                self.end_headers()
                while True:                # 流式发送：大文件(APK)不全量读入内存
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
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

    # ============ 生成作业队列（进度条/排队人数） ============
    def _job_create(self, d):
        """POST /api/jobs {user_id,date,start_time,duration,distance?} -> {ticket, ahead}"""
        uid, date, st, dur = d.get('user_id'), d.get('date'), d.get('start_time'), d.get('duration')
        if not all([uid, date, st, dur]):
            self._json({'error': '缺少必要参数'}, 400)
            return
        try:
            km = self._opt_distance(d)
        except ValueError as e:
            self._json({'error': str(e)}, 400)
            return
        _cleanup_old_temp()
        with _job_cond:
            ticket = next(_job_tickets)
            _jobs[ticket] = {'ticket': ticket, 'status': 'waiting',
                             'params': (uid, date, st, dur, km), 'ok': None, 'path': None}
            _queue.append(ticket)
            # 前面还有：正在生成中的作业 + 排在我之前的等待作业
            ahead = _processing + (len(_queue) - 1)
        self._json({'success': True, 'ticket': ticket, 'ahead': ahead})

    def _jobs_get(self):
        """GET /api/jobs/status?t=N 或 /api/jobs/result?t=N"""
        query = ''
        if '?' in self.path:
            self.path, query = self.path.split('?', 1)
        params = {}
        for kv in query.split('&'):
            if not kv:
                continue
            if '=' in kv:
                k, v = kv.split('=', 1)
                params[k] = v
        try:
            t = int(params.get('t', ''))
        except ValueError:
            self.send_error(400)
            return
        if self.path == '/api/jobs/status':
            with _job_cond:
                job = _jobs.get(t)
                if job is None:
                    self._json({'error': '任务不存在'}, 404)
                    return
                if job.get('status') == 'done':
                    self._json({'phase': 'done', 'ahead': 0, 'ok': bool(job.get('ok'))})
                elif t in _queue:
                    idx = _queue.index(t)
                    self._json({'phase': 'waiting', 'ahead': _processing + idx})
                else:
                    self._json({'phase': 'running', 'ahead': 0})
            return
        if self.path == '/api/jobs/result':
            # 等待完成（最多约 3 分钟）后返回 FIT 文件
            deadline = time.time() + 180
            while time.time() < deadline:
                with _job_cond:
                    job = _jobs.get(t)
                    if job and job.get('status') == 'done':
                        ok, path, uid = job.get('ok'), job.get('path'), job['params'][0]
                        _jobs.pop(t, None)
                        break
                    job = None
                    ok = path = uid = None
                time.sleep(0.2)
            else:
                with _job_cond:
                    job = _jobs.get(t)
                if job is None:
                    self._json({'error': '任务不存在或超时'}, 404)
                else:
                    self._json({'error': '生成超时'}, 500)
                return
            if not ok or not path or not os.path.exists(path):
                self._json({'error': '文件生成失败'}, 500)
                return
            ip = self.client_address[0]
            insert_download(uid, ip)
            try:
                with open(path, 'rb') as f:
                    content = f.read()
            finally:
                os.remove(path)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', f'attachment; filename={uid}.fit')
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_error(404)

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
        out = f'data/.g_{uuid.uuid4().hex}.fit'   # 唯一临时名：同 uid 并发安全
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
    _cleanup_old_temp()
    threading.Thread(target=_job_pump, daemon=True).start()
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
