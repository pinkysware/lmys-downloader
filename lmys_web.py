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

import mega_core
import warehouse_data

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, 'lmys_web.html')
STATE_FILE = os.path.join(HERE, '.lmys_jobs.json')
CONFIG_FILE = os.path.join(HERE, '.lmys_config.json')
# 默认下载目录。可设环境变量 REIMU_SAVE_DIR 覆盖；默认用户主目录下的 reimu-downloads
DEFAULT_SAVE = os.environ.get('REIMU_SAVE_DIR', '') or os.path.join(os.path.expanduser('~'), 'reimu-downloads')

app = Flask(__name__)

jobs = {}
jobs_lock = threading.Lock()

# 下载并发槽位（Semaphore）。惰性初始化，配置保存后动态调整。
_dl_slots = None


def _get_dl_slots():
    global _dl_slots
    if _dl_slots is None:
        try:
            n = load_config().get('max_concurrent', 3)
        except Exception:
            n = 3
        _dl_slots = threading.Semaphore(max(1, int(n)))
    return _dl_slots


def _set_dl_slots(n):
    """配置保存后调整并发槽位数（重建 Semaphore；进行中的下载不受影响）。"""
    global _dl_slots
    _dl_slots = threading.Semaphore(max(1, int(n)))


# ============ 配置（默认下载目录）============
def load_config():
    cfg = {
        'save_dir': DEFAULT_SAVE,
        'proxy': '',                        # MEGA 代理；空字符串=直连（用户在设置面板填自己的代理）
        'max_concurrent': 3,                 # 同时下载的文件数
        'rate_limit': 0,                     # 全局限速 KB/s，0=不限
    }
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, encoding='utf-8') as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                if d.get('save_dir'):
                    cfg['save_dir'] = d['save_dir']
                if 'proxy' in d:
                    cfg['proxy'] = d['proxy']
                if d.get('max_concurrent'):
                    cfg['max_concurrent'] = int(d['max_concurrent'])
                if 'rate_limit' in d:
                    cfg['rate_limit'] = int(d['rate_limit'] or 0)
    except Exception:
        pass
    # 同步到 mega_core 全局代理（运行时动态生效）
    try:
        mega_core.PROXY = cfg['proxy']
    except Exception:
        pass
    return cfg


def save_config(cfg):
    """合并保存配置（读旧文件，覆盖传入字段，写回）。"""
    try:
        merged = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, encoding='utf-8') as fh:
                    merged = json.load(fh) or {}
            except Exception:
                merged = {}
        merged.update(cfg)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as fh:
            json.dump(merged, fh, ensure_ascii=False)
        # 若更新了代理，同步到 mega_core 全局
        if 'proxy' in cfg:
            try:
                mega_core.PROXY = cfg['proxy']
            except Exception:
                pass
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
        # 全局限速（字节/秒，0=不限）。配置存 KB/s，这里 ×1024 转字节/秒。
        try:
            self.rate_limit = int(load_config().get('rate_limit', 0) or 0) * 1024
        except Exception:
            self.rate_limit = 0
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
        self._order = sorted(range(len(self.files)),
                             key=lambda i: self.files[i].get('priority', 0), reverse=True)
        self._pump()
        save_state()

    def _pump(self):
        """给剩余 waiting 文件抢并发槽位并启动，直到槽位满或无 waiting。
        注意：只拉 waiting，不碰 paused（暂停是用户主动操作，不自动恢复）。"""
        order = getattr(self, '_order', None)
        if order is None:
            order = sorted(range(len(self.files)),
                           key=lambda i: self.files[i].get('priority', 0), reverse=True)
            self._order = order
        for i in order:
            f = self.files[i]
            if f['state'] != 'waiting':
                continue
            if not _get_dl_slots().acquire(blocking=False):
                break
            try:
                self._spawn(i)
            except Exception:
                _get_dl_slots().release()
                break

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

        _logs = []
        try:
            st, n = mega_core.download_file(
                f['handle'], f['file_key'], f['size'], dest,
                progress_cb=cb,
                folder_id=self.folder_id,
                is_public=f['is_public'],
                pause_ev=ev['pause'], cancel_ev=ev['cancel'],
                rate_limit=self.rate_limit,
                log=lambda m: _logs.append(str(m)))
        except Exception as e:
            st, n = 'error', str(e)

        with self.lock:
            f['state'] = {'done': 'done', 'paused': 'paused',
                          'cancel': 'cancelled'}.get(st, 'error')
            if st == 'done':
                f['downloaded'] = f['size']
            elif st == 'error':
                # 保留详情：优先异常信息，否则取下载器日志末尾
                detail = str(n)
                if (not detail or detail == '0') and _logs:
                    detail = ' / '.join(_logs[-3:])
                f['error'] = detail[:300]
        save_state()
        # 释放并发槽位，并拉起下一个等待中的文件
        try:
            _get_dl_slots().release()
        except Exception:
            pass
        self._pump()

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
    from_official = False
    if not item:
        # 云端未命中 → 官网兜底（老代码：官网收录但 Telegram 仓库没有），
        # 实时搜 blog.reimu.net（经 Cloudflare 代理），返回简介 + 简介图（无下载链接）。
        try:
            item = warehouse_data.search_official(code)
            from_official = bool(item)
        except Exception:
            item = None

    # MEGA 存档下载链接（独立于官网：有些老代码官网没有简介，但存档里有下载）
    try:
        alinks = warehouse_data.archive_links(code)
    except Exception:
        alinks = []

    if not item and not alinks:
        return jsonify({
            'code': code.upper(),
            'found': False,
            'messages': [],
            'intro': None,
            'error': f'未找到 {code.upper()}（云端已收录 {warehouse_data.get_stats()["count"]} 条，官网与存档均无结果）',
            'source': '云端+官网+存档',
        })

    if not item:
        # 官网也没简介，但存档有下载链接 —— 只给链接
        item = {'code': code.upper(), 'date': '', 'links': [], 'intro': None}

    result = warehouse_data.to_resolve_result(code, item)
    if from_official:
        result['source'] = '官网'
        result['official'] = True
    if alinks:
        for m in result.get('messages', []):
            m['links'] = list(m.get('links', [])) + alinks
        result['archive'] = True
        result['found'] = True
    # 无简介时置 None（避免前端渲染空简介块）
    if not result.get('intro'):
        result['intro'] = None

    # 秒回：不在这里做 MEGA 预检（预检是网络请求，会拖慢首屏）。
    # 文件数/总大小由前端拿到简介后，异步调 /api/precheck 逐个填充。
    return jsonify(result)


@app.post('/api/precheck')
def api_precheck():
    """快速获取单个 MEGA 链接的文件数/总大小（前端异步调用，不阻塞简介加载）。
    body: {url}  ->  {count, total_size} 或 {error}"""
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': '缺少链接'}), 400
    try:
        cnt, total = mega_core.quick_stat(url)
        return jsonify({'count': cnt, 'total_size': total})
    except Exception as e:
        return jsonify({'error': str(e)[:120]}), 502


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
        # 仅当显式传了 rate_limit 才覆盖；否则保持 Job.__init__ 从配置读的默认限速
        if data.get('rate_limit') is not None:
            # data.rate_limit 单位 KB/s，转字节/秒
            job.rate_limit = max(0, int(data.get('rate_limit') or 0)) * 1024
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
    if action == 'resume':
        # 清掉暂停/取消标志，把可恢复文件置回 waiting，再走 _pump 抢并发槽位
        for i in range(len(job.files)):
            ev = job._events(i)
            ev['pause'].clear()
            ev['cancel'].clear()
            if job.files[i]['state'] in ('paused', 'cancelled', 'error'):
                job.files[i]['state'] = 'waiting'
        job._pump()
        return
    for i in range(len(job.files)):
        ev = job._events(i)
        if action == 'pause':
            ev['pause'].set()
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
            f['state'] = 'waiting'
            job._pump()
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


# ---- 配置（默认下载目录）----
@app.get('/api/config')
def api_config():
    return jsonify(load_config())


@app.post('/api/config')
def api_config_set():
    """保存配置（部分字段也可）。可保存：save_dir / proxy / max_concurrent / rate_limit"""
    data = request.get_json(force=True) or {}
    cfg = {}
    if 'save_dir' in data:
        d = (data.get('save_dir') or '').strip()
        if not d:
            return jsonify({'error': '目录不能为空'}), 400
        cfg['save_dir'] = d
    if 'proxy' in data:
        cfg['proxy'] = (data.get('proxy') or '').strip()
    if 'max_concurrent' in data:
        try:
            cfg['max_concurrent'] = max(1, min(16, int(data.get('max_concurrent') or 3)))
        except Exception:
            return jsonify({'error': '并发数需为 1~16 的整数'}), 400
    if 'rate_limit' in data:
        try:
            cfg['rate_limit'] = max(0, int(data.get('rate_limit') or 0))
        except Exception:
            return jsonify({'error': '限速需为整数 KB/s'}), 400
    save_config(cfg)
    # 若改了并发数，重建并发槽位（进行中的下载不受影响，后续新起的文件用新值）
    if 'max_concurrent' in cfg:
        try:
            _set_dl_slots(cfg['max_concurrent'])
        except Exception:
            pass
    return jsonify({'ok': True, **load_config()})


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


# ---- Windows 原生目录选择框（IFileDialog, ctypes 标准库实现）----
def pick_folder_native(title='选择下载目录'):
    """弹出 Windows 现代目录选择对话框（资源管理器风格：左侧快速访问/此电脑）。
    返回所选路径字符串；用户取消返回 None；不支持/失败抛异常。
    注意：对话框出现在运行本服务的机器屏幕上（exe 场景=用户本机）。"""
    if os.name != 'nt':
        raise RuntimeError('仅支持 Windows')

    import ctypes
    from ctypes import wintypes, byref, POINTER, c_void_p, c_wchar_p, c_uint

    class _GUID(ctypes.Structure):
        _fields_ = [('D1', ctypes.c_ulong), ('D2', ctypes.c_ushort),
                    ('D3', ctypes.c_ushort), ('D4', ctypes.c_ubyte * 8)]

    def _guid(s):
        # s: '{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}'
        parts = s.strip('{}').split('-')
        g = _GUID()
        g.D1 = int(parts[0], 16)
        g.D2 = int(parts[1], 16)
        g.D3 = int(parts[2], 16)
        b = bytes.fromhex(parts[3] + parts[4])
        for i in range(8):
            g.D4[i] = b[i]
        return g

    CLSID_FileOpenDialog = _guid('{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}')
    IID_IFileDialog      = _guid('{D57C7288-D4AD-4768-BE02-9D969532D960}')
    IID_IShellItem       = _guid('{43826D1E-E718-42EE-BC55-A1E261C37BFE}')

    FOS_PICKFOLDERS     = 0x20
    FOS_FORCEFILESYSTEM = 0x40
    FOS_PATHMUSTEXIST   = 0x800
    SIGDN_FILESYSPATH   = 0x80058000
    ERROR_CANCELLED_HR  = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED=1223=0x4C7)

    HRESULT = ctypes.c_long
    ole32 = ctypes.OleDLL('ole32')   # OleDLL 自动检查 HRESULT

    # 每线程初始化 COM（STA）。已初始化过会报 RPC_E_CHANGED_MODE，容错。
    try:
        ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    except OSError:
        pass

    def _slot(obj, idx, *argtypes):
        """取 COM 接口 vtable 第 idx 个函数，签名为 (HRESULT, this, *argtypes)"""
        vtbl = ctypes.cast(obj, POINTER(c_void_p)).contents.value
        fn = ctypes.cast(vtbl, POINTER(c_void_p))[idx]
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)
        return proto(fn), obj

    # CoCreateInstance(CLSID, None, CLSCTX_INPROC_SERVER=1, IID, &pfd)
    pfd = c_void_p()
    ole32.CoCreateInstance(byref(CLSID_FileOpenDialog), None, 1,
                           byref(IID_IFileDialog), byref(pfd))
    try:
        # IFileDialog vtable (IUnknown 0-2 之后):
        # Show=3, SetFileTypes=4, SetFileTypeIndex=5, GetFileTypeIndex=6,
        # Advise=7, Unadvise=8, SetOptions=9, GetOptions=10,
        # SetDefaultFolder=11, SetFolder=12, GetFolder=13,
        # GetCurrentSelection=14, SetFileName=15, SetFileNameLabel=16,
        # SetOkButtonLabel=17, SetTitle=18, Close=19, AddPlace=20,
        # SetDefaultExtension=21, SetClientGuid=22, ClearClientData=23,
        # SetFilter=24, GetResult=25
        opts = FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
        fn, this = _slot(pfd, 9, c_uint)   # SetOptions 按值传 DWORD
        hr = fn(this, opts)
        if hr != 0:
            raise OSError(f'SetOptions failed: {hr:#x}')

        # SetOkButtonLabel -> slot 17
        try:
            fn, this = _slot(pfd, 17, c_wchar_p)
            fn(this, '\u9009\u62e9\u6b64\u6587\u4ef6\u5939')
        except Exception:
            pass

        # SetTitle -> slot 18
        try:
            fn, this = _slot(pfd, 18, c_wchar_p)
            fn(this, title)
        except Exception:
            pass

        # 取前台窗口作父窗口（避免全屏浏览器盖住对话框）
        hwnd_owner = None
        try:
            hwnd_owner = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            pass

        # Show -> slot 3（阻塞直到用户关闭对话框）
        fn, this = _slot(pfd, 3, wintypes.HWND)
        hr = fn(this, hwnd_owner)
        if hr != 0:
            # 用户点取消/关闭（X）时返回 ERROR_CANCELLED，视为正常取消
            if (hr & 0xFFFFFFFF) == ERROR_CANCELLED_HR:
                return None
            raise OSError(f'Show failed: {hr:#x}')

        # GetResult -> slot 25
        psi = c_void_p()
        fn, this = _slot(pfd, 25, POINTER(c_void_p))
        hr = fn(this, byref(psi))
        if hr != 0 or not psi.value:
            return None
        try:
            # IShellItem::GetDisplayName -> slot 5
            pwsz = c_wchar_p()
            fn2, this2 = _slot(psi, 5, c_uint, POINTER(c_wchar_p))
            hr = fn2(this2, SIGDN_FILESYSPATH, byref(pwsz))
            if hr != 0 or not pwsz.value:
                return None
            path = pwsz.value
            try:
                ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(pwsz, c_void_p))
            except Exception:
                pass
            return os.path.normpath(path)
        finally:
            rel, _ = _slot(psi, 2)
            rel(psi)
    finally:
        rel, _ = _slot(pfd, 2)
        rel(pfd)


@app.post('/api/pick-dir')
def api_pick_dir():
    """弹 Windows 原生目录选择框（现代资源管理器风格）。阻塞直到用户选完。"""
    try:
        d = pick_folder_native()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'无法弹出选择框: {e}'}), 500
    if d:
        return jsonify({'ok': True, 'path': d})
    return jsonify({'ok': False, 'error': '已取消选择'})


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
