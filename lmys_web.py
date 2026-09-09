# -*- coding: utf-8 -*-
"""
lmys_web.py — 灵梦御所下载器 WebUI

浏览器访问（Windows / 安卓通用），输入资源代码即可：
  代码 → Telegram 搜频道 → 识别链接类型 →
    MEGA   : 服务端直接下载到默认下载目录\\<代码>\\（带续传/进度/子目录）
    磁力/百度等 : 交给当前设备（手机/电脑）点开，系统唤起对应 App

任务会持久化到 .lmys_jobs.json，刷新页面 / 重启服务都不丢（配合 .part 可续传）。

启动：python lmys_web.py [--host 0.0.0.0] [--port 8765]
"""

import os
import sys
import time
import uuid
import json
import socket
import subprocess
import threading
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, Response

import lmys_core
import mega_core
import warehouse_data

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, 'lmys_web.html')
STATE_FILE = os.path.join(HERE, '.lmys_jobs.json')
CONFIG_FILE = os.path.join(HERE, '.lmys_config.json')
# 默认下载目录。可设环境变量 REIMU_SAVE_DIR 覆盖；默认放到用户主目录下的 VRGAME
DEFAULT_SAVE = os.environ.get('REIMU_SAVE_DIR', '') or os.path.join(os.path.expanduser('~'), 'VRGAME')

app = Flask(__name__)

jobs = {}
jobs_lock = threading.Lock()


# ============ 配置（默认下载目录）============
def load_config():
    cfg = {'save_dir': DEFAULT_SAVE}
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, encoding='utf-8') as fh:
                d = json.load(fh)
            if isinstance(d, dict) and d.get('save_dir'):
                cfg['save_dir'] = d['save_dir']
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as fh:
            json.dump(cfg, fh, ensure_ascii=False)
    except Exception:
        pass


# ============ 下载任务 ============
class Job:
    def __init__(self, code, url, save_dir, jid=None):
        self.jid = jid or uuid.uuid4().hex[:8]
        self.code = code
        self.url = url
        self.save_dir = os.path.join(save_dir, code)
        self.files = []          # [{name,relpath,size,downloaded,state,handle,file_key,is_public,priority}]
        self.events = {}         # idx -> {'pause':Event,'cancel':Event}
        self.threads = {}        # idx -> Thread
        self.folder_id = None
        self.error = None
        self.rate_limit = 0       # 字节/秒，0=不限
        self.created = time.time()
        self.lock = threading.RLock()   # 可重入：snapshot/to_dict 在锁内又调 cur_state()

    # ---- 解析链接，列出文件 ----
    def prepare(self):
        typ, handle, key = mega_core.parse_mega_link(self.url)
        if typ is None:
            raise RuntimeError('不是有效的 MEGA 链接')
        if typ == 'folder':
            for f in mega_core.enum_tree(handle, key):
                self.files.append({
                    'name': f['name'], 'relpath': f['relpath'],
                    'size': f['size'], 'downloaded': 0, 'state': 'waiting',
                    'handle': f['handle'], 'file_key': f['file_key'],
                    'is_public': False, 'priority': 0,
                })
            self.folder_id = handle
        else:
            from mega_core import api_request, b64_to_a32, _decrypt_attr_name
            data = api_request({"a": "g", "g": 1, "p": handle})
            d0 = data[0]
            fkey = b64_to_a32(key)
            name = _decrypt_attr_name({'t': 0, 'a': d0.get('at', '')}, fkey) or handle
            self.files.append({
                'name': name, 'relpath': name,
                'size': d0.get('s', 0), 'downloaded': 0, 'state': 'waiting',
                'handle': handle, 'file_key': fkey, 'is_public': True,
                'priority': 0,
            })
            self.folder_id = None
        return self

    # ---- 事件对象（服务重启后恢复的任务可能还没有）----
    def _mk_events(self, i):
        ev = {'pause': threading.Event(), 'cancel': threading.Event()}
        self.events[i] = ev
        return ev

    def _events(self, i):
        return self.events.get(i) or self._mk_events(i)

    # ---- 目标目录 ----
    def _dest(self, f):
        return os.path.join(self.save_dir, f['relpath'].replace('/', os.sep))

    def start(self):
        os.makedirs(self.save_dir, exist_ok=True)
        for i in range(len(self.files)):
            self._mk_events(i)
        # 按优先级（降序）决定启动顺序；优先级高的先起
        order = sorted(range(len(self.files)),
                       key=lambda i: self.files[i].get('priority', 0), reverse=True)
        for i in order:
            f = self.files[i]
            if f['state'] in ('waiting', 'paused'):
                self._spawn(i)
        save_state()

    def _spawn(self, i):
        f = self.files[i]
        with self.lock:
            if f['state'] in ('done', 'downloading', 'cancelled'):
                return
        t = threading.Thread(target=self._worker, args=(i, f), daemon=True)
        self.threads[i] = t
        t.start()

    def _worker(self, i, f):
        ev = self._events(i)
        # 注意：这里【不】clear cancel —— cancel 一旦设置，必须保留到显式 resume/retry 才清。
        ev['pause'].clear()
        dest = self._dest(f)
        os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)

        with self.lock:
            f['state'] = 'downloading'

        def cb(dl):
            with self.lock:
                f['downloaded'] = dl
                f['state'] = 'downloading'

        try:
            st, n = mega_core.download_file(
                f['handle'], f['file_key'], f['size'], dest,
                progress_cb=cb,
                folder_id=self.folder_id,
                is_public=f['is_public'],
                pause_ev=ev['pause'], cancel_ev=ev['cancel'],
                rate_limit=self.rate_limit)
        except Exception as e:
            st, n = 'error', str(e)

        with self.lock:
            f['state'] = {'done': 'done', 'paused': 'paused',
                          'cancel': 'cancelled'}.get(st, 'error')
            if st == 'done':
                f['downloaded'] = f['size']
            elif st == 'error':
                f['error'] = str(n)[:200]
        save_state()

    # ---- 状态由各文件实时推导 ----
    def cur_state(self):
        with self.lock:
            sts = {f['state'] for f in self.files}
        if not sts:
            return 'pending'
        if 'downloading' in sts or 'waiting' in sts:
            return 'working'
        if sts == {'done'}:
            return 'done'
        if sts <= {'done', 'cancelled'}:
            return 'done' if 'done' in sts else 'cancelled'
        if 'error' in sts:
            return 'error'
        if 'paused' in sts:
            return 'paused'
        if 'cancelled' in sts:
            return 'cancelled'
        return 'working'

    def snapshot(self):
        """返回任务快照，含每个文件的速度与剩余时间（MegaDownloader 风格表格用）"""
        now = time.time()
        with self.lock:
            files = []
            for f in self.files:
                lb = f.get('_lb', 0)
                lt = f.get('_lt')
                sp = 0.0
                if f['state'] == 'downloading':
                    if lt is None:
                        f['_lb'] = f['downloaded']; f['_lt'] = now
                    else:
                        dt = now - lt
                        if dt >= 0.6:
                            sp = max(0.0, (f['downloaded'] - lb) / dt)
                            f['speed'] = sp
                            f['_lb'] = f['downloaded']; f['_lt'] = now
                        else:
                            sp = f.get('speed', 0.0)
                else:
                    f['speed'] = 0.0
                    f['_lb'] = f['downloaded']; f['_lt'] = now

                d = f.get('downloaded', 0)
                left = max(0, f['size'] - d)
                eta = int(left / sp) if sp > 0 and f['state'] == 'downloading' else None
                files.append({
                    'name': f['name'], 'relpath': f['relpath'], 'size': f['size'],
                    'downloaded': d, 'state': f['state'], 'error': f.get('error'),
                    'speed': sp, 'eta': eta, 'priority': f.get('priority', 0),
                    'idx': len(files),
                })
        total = sum(x['size'] for x in files) or 1
        got = sum(x['downloaded'] for x in files)
        spd = sum(x['speed'] for x in files)
        eta = int((total - got) / spd) if spd > 0 and got < total else None
        return {
            'jid': self.jid, 'code': self.code, 'url': self.url,
            'save_dir': self.save_dir, 'state': self.cur_state(),
            'error': self.error,
            'total': sum(x['size'] for x in files),
            'downloaded': got,
            'pct': round(got / total * 100, 1),
            'speed': spd, 'eta': eta,
            'files': files,
        }

    def to_dict(self):
        with self.lock:
            return {
                'jid': self.jid, 'code': self.code, 'url': self.url,
                'save_dir': self.save_dir, 'folder_id': self.folder_id,
                'created': self.created, 'rate_limit': self.rate_limit,
                'was_active': self.cur_state() == 'working',
                'files': [{
                    'name': f['name'], 'relpath': f['relpath'], 'size': f['size'],
                    'downloaded': f.get('downloaded', 0), 'state': f['state'],
                    'handle': f['handle'], 'file_key': f['file_key'],
                    'is_public': f['is_public'], 'priority': f.get('priority', 0),
                } for f in self.files],
            }

    @staticmethod
    def from_dict(d):
        j = Job(d['code'], d['url'], os.path.dirname(d['save_dir']), jid=d['jid'])
        j.save_dir = d['save_dir']
        j.folder_id = d.get('folder_id')
        j.rate_limit = d.get('rate_limit', 0)
        j.created = d.get('created', time.time())
        for f in d['files']:
            f = dict(f)
            st = f.get('state', 'waiting')
            if st == 'downloading':
                st = 'paused'          # 进程已重启，不可能还在跑
            f['state'] = st
            dest = os.path.join(j.save_dir, f['relpath'].replace('/', os.sep))
            part = dest + '.part'
            if os.path.exists(part):
                f['downloaded'] = os.path.getsize(part)
            elif os.path.exists(dest) and os.path.getsize(dest) == f['size']:
                f['state'] = 'done'
                f['downloaded'] = f['size']
            f.setdefault('priority', 0)
            j.files.append(f)
        return j

    # ---- 查看连接（a:g 实时取，因为 g-URL 有时效）----
    def get_link(self, idx, with_desc=False):
        f = self.files[idx]
        url = mega_core._get_download_url(f['handle'], self.folder_id, f['is_public'])
        if with_desc:
            return {
                'name': f['name'],
                'size': f['size'],
                'url': url,
            }
        return {'url': url}

    # ---- 打开目录（Windows）----
    def open_dir(self):
        os.makedirs(self.save_dir, exist_ok=True)
        if os.name == 'nt':
            subprocess.Popen(['explorer', self.save_dir])
        else:
            subprocess.Popen(['xdg-open', self.save_dir])
        return self.save_dir


# ============ 持久化 ============
def save_state():
    try:
        with jobs_lock:
            data = [j.to_dict() for j in jobs.values()]
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def load_state():
    if not os.path.exists(STATE_FILE):
        return 0
    try:
        with open(STATE_FILE, encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        return 0
    n = 0
    for d in data:
        try:
            j = Job.from_dict(d)
            with jobs_lock:
                jobs[j.jid] = j
            if d.get('was_active'):
                j.start()      # 服务重启前正在下的，自动续传
            n += 1
        except Exception:
            pass
    return n


# ============ API ============
@app.route('/')
def index():
    try:
        with open(INDEX_HTML, encoding='utf-8') as f:
            return Response(f.read(), mimetype='text/html; charset=utf-8')
    except Exception as e:
        return f"缺少前端文件 {INDEX_HTML}: {e}", 500


@app.post('/api/resolve')
def api_resolve():
    """查代码。数据源 = reimu-warehouse 云端预抓缓存(warehouse_data)，不再实时连 Telegram。"""
    data = request.get_json(force=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'error': '请输入资源代码'}), 400

    # 未初始化则先初始化（本地缓存或云端拉取）
    if not warehouse_data.get_stats().get('count'):
        warehouse_data.init()

    item = warehouse_data.lookup(code)
    if not item:
        return jsonify({
            'code': code.upper(),
            'found': False,
            'messages': [],
            'intro': None,
            'error': f'未找到 {code.upper()}（云端已收录 {warehouse_data.get_stats()["count"]} 条，可能该代码较新尚未同步或不在收录范围）',
            'source': '云端',
        })

    result = warehouse_data.to_resolve_result(code, item)

    # 预检：对 MEGA 链接枚举文件清单与总大小（供 UI 下载前展示）
    precheck = bool(data.get('precheck', False))  # 默认不做 MEGA 预检(快)；需文件清单时前端传 precheck=true
    if precheck:
        for m in result.get('messages', []):
            for link in m.get('links', []):
                if link['type'] != 'mega':
                    continue
                try:
                    typ, handle, key = mega_core.parse_mega_link(link['url'])
                    files = []
                    if typ == 'folder':
                        files = mega_core.enum_tree(handle, key)
                        link['files'] = [{
                            'relpath': f['relpath'], 'name': f['name'],
                            'size': f['size'],
                        } for f in files]
                    else:
                        from mega_core import api_request, b64_to_a32, _decrypt_attr_name
                        d0 = api_request({"a": "g", "g": 1, "p": handle})[0]
                        fkey = b64_to_a32(key)
                        nm = _decrypt_attr_name({'t': 0, 'a': d0.get('at', '')}, fkey) or handle
                        link['files'] = [{'relpath': nm, 'name': nm,
                                          'size': d0.get('s', 0)}]
                    link['count'] = len(link['files'])
                    link['total_size'] = sum(f['size'] for f in link['files'])
                except Exception as e:
                    link['precheck_error'] = str(e)[:120]
    return jsonify(result)


@app.post('/api/refresh')
def api_refresh():
    """从云端刷新数据缓存"""
    res = warehouse_data.refresh(timeout=int((request.get_json(silent=True) or {}).get('timeout', 60)))
    return jsonify(res)


@app.post('/api/download')
def api_download():
    data = request.get_json(force=True) or {}
    code = (data.get('code') or '').strip() or 'MEGA'
    url = (data.get('url') or '').strip()
    save_dir = (data.get('save_dir') or DEFAULT_SAVE).strip()
    if not url:
        return jsonify({'error': '缺少链接'}), 400

    # 同一链接已在队列中则复用
    with jobs_lock:
        for j in jobs.values():
            if j.url == url and j.cur_state() in ('working', 'paused', 'pending'):
                return jsonify({'jid': j.jid, 'reused': True})

    try:
        job = Job(code, url, save_dir).prepare()
    except Exception as e:
        return jsonify({'error': f'解析失败: {e}'}), 500
    try:
        job.rate_limit = max(0, int(data.get('rate_limit') or 0))
    except Exception:
        pass
    with jobs_lock:
        jobs[job.jid] = job
    job.start()
    return jsonify({'jid': job.jid})


@app.get('/api/jobs')
def api_jobs():
    with jobs_lock:
        return jsonify([j.snapshot() for j in jobs.values()])


# ---- 任务级操作 ----
def _apply_action(job, action):
    for i in range(len(job.files)):
        ev = job._events(i)
        if action == 'pause':
            ev['pause'].set()
        elif action == 'resume':
            ev['pause'].clear()
            ev['cancel'].clear()
            if job.files[i]['state'] in ('paused', 'cancelled', 'error', 'waiting'):
                job._spawn(i)
        elif action in ('cancel', 'remove'):
            ev['cancel'].set()
            job.files[i]['state'] = 'cancelled'


@app.post('/api/job/<jid>/<action>')
def api_job_action(jid, action):
    with jobs_lock:
        job = jobs.get(jid)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    _apply_action(job, action)
    if action == 'remove':
        with jobs_lock:
            jobs.pop(jid, None)
    save_state()
    return jsonify({'ok': True})


@app.post('/api/jobs/<action>')
def api_jobs_action(action):
    """全局操作：对所有任务 暂停/继续/取消/清除已完成"""
    with jobs_lock:
        targets = list(jobs.values())
    for job in targets:
        if action == 'clear_done' and job.cur_state() in ('done', 'cancelled'):
            with jobs_lock:
                jobs.pop(job.jid, None)
            continue
        if action in ('pause', 'resume', 'cancel', 'remove'):
            _apply_action(job, action)
    save_state()
    return jsonify({'ok': True})


# ---- 文件级操作（右键菜单）----
@app.post('/api/job/<jid>/file/<int:idx>/<action>')
def api_file_action(jid, idx, action):
    with jobs_lock:
        job = jobs.get(jid)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if idx < 0 or idx >= len(job.files):
        return jsonify({'error': '文件序号无效'}), 400

    f = job.files[idx]
    ev = job._events(idx)

    if action == 'pause':
        ev['pause'].set()
    elif action == 'resume':
        ev['pause'].clear(); ev['cancel'].clear()
        if f['state'] in ('paused', 'cancelled', 'error', 'waiting'):
            job._spawn(idx)
    elif action == 'cancel':
        ev['cancel'].set()
        f['state'] = 'cancelled'
    elif action == 'priority_up':
        f['priority'] = f.get('priority', 0) + 1
    elif action == 'priority_down':
        f['priority'] = f.get('priority', 0) - 1
    elif action == 'remove_list':
        ev['cancel'].set()
        f['state'] = 'cancelled'
        job.files.pop(idx)
        job.events.pop(idx, None)
        job.threads.pop(idx, None)
    elif action == 'remove_disk':
        ev['cancel'].set()
        f['state'] = 'cancelled'
        for p in (job._dest(f), job._dest(f) + '.part'):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        job.files.pop(idx)
        job.events.pop(idx, None)
        job.threads.pop(idx, None)
    elif action == 'opendir':
        return jsonify({'ok': True, 'path': job.open_dir()})
    elif action == 'link':
        try:
            return jsonify({'ok': True, **job.get_link(idx, with_desc=False)})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)})
    elif action == 'linkdesc':
        try:
            return jsonify({'ok': True, **job.get_link(idx, with_desc=True)})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)})
    else:
        return jsonify({'error': '未知操作'}), 400

    # remove 后若文件列表空了，整个任务一起清掉（避免空壳显示）
    if not job.files:
        with jobs_lock:
            jobs.pop(job.jid, None)
        save_state()
        return jsonify({'ok': True, 'job_removed': True})

    save_state()
    return jsonify({'ok': True})


@app.get('/api/thumb/<code>')
def api_thumb(code):
    """简介封面图（来自文章频道，缓存在内存）"""
    data = lmys_core.THUMB_CACHE.get((code or '').strip().upper())
    if not data:
        return Response(status=404)
    return Response(data, mimetype='image/jpeg')


# ---- 配置（默认下载目录）----
@app.get('/api/config')
def api_config():
    return jsonify(load_config())


@app.post('/api/config')
def api_config_set():
    data = request.get_json(force=True) or {}
    d = (data.get('save_dir') or '').strip()
    if not d:
        return jsonify({'error': '目录不能为空'}), 400
    cfg = {'save_dir': d}
    save_config(cfg)
    return jsonify({'ok': True, 'save_dir': d})


@app.post('/api/opendir')
def api_opendir():
    """打开下载根目录（Windows 资源管理器）"""
    data = request.get_json(silent=True) or {}
    d = (data.get('path') or '').strip() or load_config()['save_dir']
    if not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    if os.name == 'nt':
        subprocess.Popen(['explorer', d])
    else:
        subprocess.Popen(['xdg-open', d])
    return jsonify({'ok': True, 'path': d})


# ---- 目录浏览（网页内目录选择器）----
def _list_drives():
    import string
    drives = []
    for c in string.ascii_uppercase:
        d = c + ':\\'
        if os.path.exists(d):
            drives.append(d)
    return drives


@app.get('/api/browse')
def api_browse():
    """列出目录内容。path 为空 → 返回盘符列表；否则返回该目录的子目录。
    返回 {path, parent, drives?, dirs:[...]}"""
    path = (request.args.get('path') or '').strip()

    # 根：返回盘符列表
    if not path:
        return jsonify({'path': '', 'parent': None, 'drives': _list_drives(),
                        'dirs': []})

    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return jsonify({'error': '目录不存在'}), 404

    try:
        entries = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir() and not e.name.startswith('.'):
                        entries.append(e.name)
                except Exception:
                    continue
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500

    entries.sort(key=str.lower)

    # 父目录：盘符根没有上级
    parent = os.path.dirname(path)
    if os.name == 'nt':
        # 盘符根（如 C:\）的 dirname 是自身
        if parent == path or not os.path.isdir(parent):
            parent = ''   # '' 表示回到盘符选择
    else:
        if parent == path:
            parent = ''

    return jsonify({
        'path': path,
        'parent': parent,
        'is_root': parent == '',
        'dirs': entries,
    })


@app.post('/api/mkdir')
def api_mkdir():
    """在指定目录下新建子文件夹"""
    data = request.get_json(silent=True) or {}
    parent = (data.get('parent') or '').strip()
    name = (data.get('name') or '').strip()
    if not parent or not name:
        return jsonify({'error': '参数不完整'}), 400
    # 安全：禁止路径分隔符/非法字符
    if any(ch in name for ch in '\\/:*?"<>|') or name in ('.', '..'):
        return jsonify({'error': '文件夹名含非法字符'}), 400
    target = os.path.join(parent, name)
    try:
        os.makedirs(target, exist_ok=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'path': target})


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8765)
    a = ap.parse_args()

    n = load_state()
    ip = lan_ip()

    # 初始化云端数据缓存（查代码用）。失败静默，不影响启动。
    wd = warehouse_data.init()
    print('=' * 52)
    print('  灵梦御所下载器 WebUI 已启动')
    print(f'  本机访问 : http://127.0.0.1:{a.port}')
    print(f'  手机/其他设备: http://{ip}:{a.port}')
    print(f'  下载保存到: {DEFAULT_SAVE}\\<代码>\\')
    if wd.get('count'):
        print(f'  云端资源: {wd["count"]} 条 (更新于 {wd.get("updated_at", "?")})')
    else:
        print('  [警告] 云端资源加载失败，代码查询暂不可用')
    if n:
        print(f'  已恢复 {n} 个历史任务')
    print('=' * 52)
    app.run(host=a.host, port=a.port, debug=False, threaded=True)
