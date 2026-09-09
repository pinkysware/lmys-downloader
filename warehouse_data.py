# -*- coding: utf-8 -*-
"""
warehouse_data.py — 下载器的「云端数据源」层

下载器用 reimu-warehouse 云端预抓的 data.json 当查询源。
- 启动/刷新时从 reimu-warehouse.pages.dev/data.json 拉取并缓存到本地
- 查询走本地缓存（秒回）

默认数据源：
  https://reimu-warehouse.pages.dev/data.json   （Cloudflare 公开站）
可用环境变量 WAREHOUSE_DATA_URL 覆盖。
"""

import os
import sys
import io
import json
import time
import threading

import urllib.request

def _data_dir():
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
