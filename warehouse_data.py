# -*- coding: utf-8 -*-
"""
warehouse_data.py — 下载器的「云端数据源」层

下载器不再实时连 Telegram 搜频道，而是用 reimu-warehouse 云端预抓的 data.json 当查询源。
- 启动/刷新时从 reimu-warehouse.pages.dev/data.json 拉取并缓存到本地 data_cache.json
- /api/resolve 查本地缓存（秒回，不依赖 Telegram 凭据）

默认数据源：
  https://reimu-warehouse.pages.dev/data.json   （Cloudflare 公开站）
可用环境变量 WAREHOUSE_DATA_URL 覆盖。
"""

import os
import sys
import io
import re
import json
import time
import threading

import urllib.request
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """运行目录。PyInstaller 打包后取 exe 所在目录（避免缓存写进 _MEIPASS 临时目录）。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


LOCAL_CACHE = os.path.join(_data_dir(), '.warehouse_cache.json')

# 云端数据源（公开站，无需鉴权）。可环境变量覆盖指向其他源。
DEFAULT_SOURCE = 'https://reimu-warehouse.pages.dev/data.json'
SOURCE_URL = os.environ.get('WAREHOUSE_DATA_URL', DEFAULT_SOURCE)

_lock = threading.Lock()
_data = []          # 原始 items 列表（保持 data.json 顺序）
_by_code = {}       # code -> item
_updated_at = ''
_last_fetch = 0


def load_local():
    """从本地缓存加载（若存在）"""
    global _data, _by_code, _updated_at
    try:
        if os.path.exists(LOCAL_CACHE):
            with io.open(LOCAL_CACHE, encoding='utf-8') as f:
                d = json.load(f)
            _set(d)
            return True
    except Exception:
        pass
    return False


def fetch_remote(timeout=60):
    """从云端拉取最新 data.json 并覆盖缓存"""
    global _data, _by_code, _updated_at, _last_fetch
    req = urllib.request.Request(SOURCE_URL,
                                 headers={'User-Agent': 'ReimuDownloader/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    d = json.loads(raw.decode('utf-8'))
    with _lock:
        _set(d)
        # 写本地缓存
        try:
            with io.open(LOCAL_CACHE, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False)
        except Exception:
            pass
    return True


def _set(d):
    global _data, _by_code, _updated_at
    _data = d.get('items', []) or []
    _updated_at = d.get('updated_at', '')
    _by_code = {}
    for it in _data:
        if it.get('code'):
            _by_code[it['code'].upper()] = it


def init(force_refresh=False, timeout=60):
    """初始化：先本地缓存；可选强制拉云端刷新"""
    if not load_local():
        try:
            fetch_remote(timeout=timeout)
        except Exception:
            pass  # 拉不到也静默，查询会返回未命中
    elif force_refresh:
        try:
            fetch_remote(timeout=timeout)
        except Exception:
            pass
    return get_stats()


def refresh(timeout=60):
    """强制从云端刷新"""
    try:
        fetch_remote(timeout=timeout)
        return {'ok': True, **get_stats()}
    except Exception as e:
        return {'ok': False, 'error': str(e), **get_stats()}


def get_stats():
    with _lock:
        return {
            'count': len(_data),
            'updated_at': _updated_at,
            'source': SOURCE_URL,
            'last_fetch': _last_fetch,
        }


def lookup(code):
    """按代码查资源。返回 item 或 None。兼容输入 R4191 / 4191"""
    code = (code or '').strip().upper()
    if not code:
        return None
    if code in _by_code:
        return _by_code[code]
    # 容错：纯数字自动补 R / S
    if code.isdigit():
        for pre in ('R', 'S'):
            if pre + code in _by_code:
                return _by_code[pre + code]
    return None


def _cover_base():
    """根据数据源 URL 推导封面图基址（data.json 同站点的根路径）"""
    try:
        from urllib.parse import urlparse
        u = urlparse(SOURCE_URL)
        # data.json 可能在根或子路径，去掉末尾文件名得目录基址
        path = u.path.rsplit('/', 1)[0] if '/' in u.path else ''
        return f"{u.scheme}://{u.netloc}{path}/"
    except Exception:
        return DEFAULT_SOURCE.rsplit('/', 1)[0] + '/'


def to_resolve_result(code, item):
    """把 data.json 的 item 转成旧 /api/resolve 前端兼容的 messages+intro 结构
    并把 intro.cover 的相对路径转成完整云端 URL（前端能直接显示）"""
    links = item.get('links', []) or []
    intro = dict(item.get('intro') or {})
    # 封面相对路径 → 完整 URL
    if intro.get('cover') and not str(intro['cover']).startswith('http'):
        intro['cover'] = _cover_base() + str(intro['cover']).lstrip('/')
    return {
        'code': item.get('code') or code,
        'found': bool(links or intro),
        'date': item.get('date', ''),
        'messages': [{
            'id': item.get('code') or code,
            'date': item.get('date', ''),
            'text': '',
            'links': links,
            'codes': [],
            'from_cloud': True,
        }],
        'intro': intro,
        'source': '云端',
    }


# ============ 老代码官网兜底 ============
# 云端 data.json 覆盖 R2182~R4411；更早的代码（R0099~R2181）官网收录但云端没有。
# 通过 Cloudflare Pages Function(/api/search) 实时搜官网，拿「简介 + 简介图」（无下载链接）。
# 官网代码格式为 4 位补零（R0099 / R0500 / R4195），故查询前先规范化。

OFFICIAL_API = 'https://reimu-warehouse.pages.dev/api/search'
_official_cache = {}          # code -> item 或 None
_official_lock = threading.Lock()


def normalize_code(raw):
    """官网代码规范化：纯数字默认补 R（2999 -> R2999），再补零到 4 位（R100 -> R0100）。"""
    s = (raw or '').strip().upper()
    if s.isdigit():
        s = 'R' + s
    m = re.match(r'^([RS])(\d{1,6})$', s)
    if not m:
        return s
    return m[1] + m[2].zfill(4)


def search_official(code, timeout=15):
    """官网兜底查询。返回 item 结构（links 为空 + intro）或 None。

    直连 pages.dev（Cloudflare CDN 境内可达），显式禁用系统代理避免走错。
    带内存缓存，同一代码不重复请求。
    """
    code = normalize_code(code)
    with _official_lock:
        if code in _official_cache:
            return _official_cache[code]

    item = None
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = OFFICIAL_API + '?code=' + urllib.parse.quote(code)
        req = urllib.request.Request(url, headers={'User-Agent': 'ReimuDownloader/1.0'})
        with op.open(req, timeout=timeout) as r:
            d = json.loads(r.read().decode('utf-8', 'ignore'))
        if d.get('found'):
            item = {
                'code': d.get('code') or code,
                'date': d.get('date') or '',
                'links': [],
                'intro': {
                    'title': d.get('title') or '',
                    'raw_title': d.get('title') or '',
                    'body': d.get('summary') or '',
                    'cover': d.get('cover') or '',
                    'platforms': [],
                    'categories': [],
                    'tags': [],
                    'detail_url': d.get('detail_url') or '',
                    'date': d.get('date') or '',
                    'from_official': True,
                },
                'source': '官网',
            }
    except Exception:
        item = None

    with _official_lock:
        _official_cache[code] = item
    return item


# ============ MEGA 老代码存档（R0008~R4364，已冻结）============
# 存档不再更新，映射从云端 archive.json 拉取（本地缓存）。
# 格式: {root:{handle,key}, items:{ "R1725":[{"sub":"BhZwQAoJ","files":1,"size":263704854}] }}

ARCHIVE_URL = 'https://reimu-warehouse.pages.dev/archive.json'
_archive = None
_archive_lock = threading.Lock()


def load_archive():
    """加载存档映射（本地缓存优先，否则云端拉取）。失败返回空结构。"""
    global _archive
    with _archive_lock:
        if _archive is not None:
            return _archive
        local = os.path.join(_data_dir(), '.archive_cache.json')
        try:
            if os.path.exists(local):
                with io.open(local, encoding='utf-8') as f:
                    _archive = json.load(f)
                return _archive
        except Exception:
            pass
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(ARCHIVE_URL,
                                         headers={'User-Agent': 'ReimuDownloader/1.0'})
            with op.open(req, timeout=15) as r:
                _archive = json.loads(r.read().decode('utf-8', 'ignore'))
            try:
                with io.open(local, 'w', encoding='utf-8') as f:
                    json.dump(_archive, f, ensure_ascii=False)
            except Exception:
                pass
        except Exception:
            # 失败不缓存空结果，下次调用会重试
            return {'root': {}, 'items': {}}
        return _archive


def archive_links(code):
    """返回该代码的 MEGA 存档下载链接列表（可能多个子目录）。无则空列表。"""
    try:
        a = load_archive()
        root = a.get('root') or {}
        h, k = root.get('handle'), root.get('key')
        if not h or not k:
            return []
        entry = (a.get('items') or {}).get(normalize_code(code))
        if not entry:
            return []
        out = []
        for e in entry:
            sub = e.get('sub')
            if not sub:
                continue
            out.append({
                'url': 'https://mega.nz/folder/%s#%s/folder/%s' % (h, k, sub),
                'type': 'mega',
                'label': 'MEGA 存档',
                'variant': '',
                'code': '',
                'files': e.get('files', 0),
                'total_size': e.get('size', 0),
            })
        return out
    except Exception:
        return []
