# -*- coding: utf-8 -*-
"""
lmys_core.py — 灵梦御所资源解析核心

职责：资源代码（如 R4191）→ Telegram 频道搜索 → 提取链接 → 分类 → 配对提取码

Telegram 凭据（api_id / api_hash / session_string）按以下优先级获取：
  1. 环境变量 TG_API_ID / TG_API_HASH / TG_SESSION_STRING [ / TG_PROXY_HOST / TG_PROXY_PORT / TG_PROXY_TYPE ]
  2. 本文件同目录下的 telegram_config.json
  3. 本机 WorkBuddy 的 ~/.workbuddy/mcp.json（仅开发者本机兼容场景）

频道：📦 灵梦御所 仓库 📦 (@lmys886, chat_id=-1003648092850)
"""

import os
import io
import re
import json
import asyncio
import threading

from telethon import TelegramClient
from telethon.sessions import StringSession

CHANNEL_ID = -1003648092850      # 📦 灵梦御所 仓库 📦
CHANNEL_NAME = "灵梦御所 仓库 (@lmys886)"

ARTICLE_CHANNEL_ID = -1003245444362   # 📄 灵梦御所 文章 📄 (@lmys8181)
ARTICLE_CHANNEL_NAME = "灵梦御所 文章 (@lmys8181)"

# 封面图缓存：code -> bytes
THUMB_CACHE = {}

# 凭据配置来源
_HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_TG_CFG = os.path.join(_HERE, 'telegram_config.json')
MCP_JSON = os.path.expanduser(r'~\.workbuddy\mcp.json')


# ============ Telegram 客户端（常驻后台线程）============
_client = None
_loop = None
_ready = threading.Event()
_err = None


def _proxy_from(host, port, ptype):
    """构造 telethon 代理元组；缺任一返回 None（直连）"""
    if not host or not port:
        return None
    import socks
    kind = socks.HTTP if 'http' in str(ptype).lower() else socks.SOCKS5
    return (kind, host, int(port))


def _load_cfg():
    """读取 Telegram 凭据。优先级：环境变量 > 本目录 telegram_config.json > 本机 mcp.json"""
    # 1. 环境变量
    if os.environ.get('TG_API_ID') and os.environ.get('TG_SESSION_STRING'):
        return {
            'api_id': int(os.environ['TG_API_ID']),
            'api_hash': os.environ.get('TG_API_HASH', ''),
            'session': os.environ['TG_SESSION_STRING'],
            'proxy': _proxy_from(os.environ.get('TG_PROXY_HOST'),
                                 os.environ.get('TG_PROXY_PORT'),
                                 os.environ.get('TG_PROXY_TYPE')),
        }

    # 2. 本目录 telegram_config.json（分发部署推荐）
    if os.path.exists(LOCAL_TG_CFG):
        with io.open(LOCAL_TG_CFG, encoding='utf-8') as f:
            c = json.load(f)
        return {
            'api_id': int(c['api_id']),
            'api_hash': c.get('api_hash', ''),
            'session': c['session_string'],
            'proxy': _proxy_from(c.get('proxy_host'), c.get('proxy_port'), c.get('proxy_type')),
        }

    # 3. 本机 WorkBuddy 场景（开发者本地兼容）
    if os.path.exists(MCP_JSON):
        with io.open(MCP_JSON, encoding='utf-8') as f:
            d = json.load(f)
        env = d.get('mcpServers', d)['telegram-mcp']['env']
        return {
            'api_id': int(env['TELEGRAM_API_ID']),
            'api_hash': env['TELEGRAM_API_HASH'],
            'session': env['TELEGRAM_SESSION_STRING'],
            'proxy': _proxy_from(env.get('TELEGRAM_PROXY_HOST'),
                                 env.get('TELEGRAM_PROXY_PORT'),
                                 env.get('TELEGRAM_PROXY_TYPE')),
        }

    raise RuntimeError(
        '未找到 Telegram 凭据。请设置环境变量 TG_API_ID/TG_SESSION_STRING，'
        '或在本目录创建 telegram_config.json（参见 telegram_config.example）。')


def _bg_loop():
    global _loop, _client, _err
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _loop = loop
        cfg = _load_cfg()
        _client = TelegramClient(StringSession(cfg['session']),
                                 cfg['api_id'], cfg['api_hash'],
                                 proxy=cfg['proxy'])
        loop.run_until_complete(_client.connect())
    except Exception as e:
        _err = str(e)
    finally:
        _ready.set()
    if _loop:
        _loop.run_forever()


def ensure_client(timeout=60):
    """启动后台 telethon 客户端（只启动一次）"""
    global _client
    if _ready.is_set():
        if _err:
            raise RuntimeError(f"Telegram 连接失败: {_err}")
        return _client
    t = threading.Thread(target=_bg_loop, daemon=True)
    t.start()
    _ready.wait(timeout)
    if _err:
        raise RuntimeError(f"Telegram 连接失败: {_err}")
    if _client is None:
        raise RuntimeError("Telegram 客户端未初始化")
    return _client


def search_channel(code, limit=5):
    """在频道里搜索代码，返回消息列表 [{id, date, text}]"""
    ensure_client()

    async def _c():
        out = []
        async for m in _client.iter_messages(CHANNEL_ID, search=code, limit=limit):
            out.append({
                'id': m.id,
                'date': m.date.strftime('%Y-%m-%d %H:%M') if m.date else '',
                'text': m.text or '',
            })
        return out

    fut = asyncio.run_coroutine_threadsafe(_c(), _loop)
    return fut.result(timeout=90)


# ============ 链接提取与分类 ============
URL_RE = re.compile(
    r'https?://[^\s<>"\'\u4e00-\u9fff]+'
    r'|magnet:\?[^\s<>"\'\u4e00-\u9fff]+'
    r'|thunder://[^\s<>"\'\u4e00-\u9fff]+',
    re.I)

# 就近配对用的提取码（比全局版宽松）
CODE_NEAR_RE = re.compile(
    r'(?:提取码|提取密码|解压码|解压密码|密码|pwd)\s*[:：]?\s*([A-Za-z0-9]{4,8})',
    re.I)

# [xxx] / 【xxx】 里的变体标签（如 WinCrypt / Encrypto）
VARIANT_RE = re.compile(r'[\[【]([^\]】]{1,24})[\]】]')

# 这些是「分节标题」，不是变体名，配对时要跳过
SECTION_WORDS = {
    '磁力链接', '磁力', '百度云', '百度网盘', '百度盘', 'MEGA', 'mega',
    'TeraBox', 'terabox', '阿里云盘', '阿里', '夸克', '夸克网盘',
    '迅雷', '蓝奏', '115', '直链', '备用', '分流', '主链',
}

# 形如 R4191 / S5271 的资源代码，也不是变体名
CODE_WORD_RE = re.compile(r'^[RSrs]\d{3,6}$')

CODE_PATTERNS = [
    (r'提取码[:：\s]*([A-Za-z0-9]{4,8})', '提取码'),
    (r'提取密码[:：\s]*([A-Za-z0-9]{4,8})', '提取码'),
    (r'解压码[:：\s]*([A-Za-z0-9]{4,8})', '解压码'),
    (r'解压密码[:：\s]*([A-Za-z0-9]{4,8})', '解压码'),
    (r'密码[:：\s]*([A-Za-z0-9]{4,8})', '密码'),
    (r'\bpwd[:：\s]*([A-Za-z0-9]{4,8})', '密码'),
]

NETDISK_LABELS = {
    'mega': 'MEGA',
    'magnet': '磁力链接',
    'baidu': '百度网盘',
    'thunder': '迅雷',
    'aliyun': '阿里云盘',
    'quark': '夸克网盘',
    'lanzou': '蓝奏云',
    'terabox': 'TeraBox',
    '115': '115网盘',
    'other': '其他链接',
}


def classify(url):
    u = (url or '').lower()
    if 'mega.nz' in u:
        return 'mega'
    if u.startswith('magnet:'):
        return 'magnet'
    if u.startswith('thunder://'):
        return 'thunder'
    if 'pan.baidu.com' in u:
        return 'baidu'
    if '1024terabox' in u or 'terabox' in u:
        return 'terabox'
    if 'aliyundrive' in u or 'alipan' in u:
        return 'aliyun'
    if 'quark.cn' in u:
        return 'quark'
    if 'lanzou' in u:
        return 'lanzou'
    if '115.com' in u or '115cdn' in u:
        return '115'
    return 'other'


def _variant_before(text, pos, window=140):
    """往前找最近的 [变体名]，跳过分节标题和资源代码"""
    before = text[max(0, pos - window):pos]
    cands = VARIANT_RE.findall(before)
    for v in reversed(cands):
        v = v.strip()
        if not v or v in SECTION_WORDS or CODE_WORD_RE.match(v):
            continue
        if re.fullmatch(r'[^\u4e00-\u9fff]{0,24}', v) and not re.search(r'[\u4e00-\u9fff]', v):
            # 纯英文/数字组合（如 WinCrypt / Win / Encrypto）优先采用
            return v
        return v
    return ''


def extract_links(text):
    """提取链接并分类，同时就近配对「变体名」与「提取码」。

    频道里一条消息常含多个网盘，各自的提取码不同（例：WinCrypt 的百度盘 bur8、
    Encrypto 的 jcdw），所以必须按链接就近配对，不能全局共用一个码。

    注意：提取码的搜索范围必须截止到「下一个链接之前」，否则会把下一个网盘
    的码错配过来（实测踩坑：磁力链被配成了后面百度盘的 bur8）。
    """
    text = text or ''
    matches = list(URL_RE.finditer(text))
    seen = set()
    out = []
    for idx, m in enumerate(matches):
        u = m.group(0).rstrip('.,;）)]\'"')
        if u in seen:
            continue
        seen.add(u)
        t = classify(u)

        # 变体名：链接之前最近的 [xxx]
        variant = _variant_before(text, m.start())

        # 提取码：只在「本链接之后 ~ 下一个链接之前」的范围内找
        nxt = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        tail = text[m.end():min(nxt, m.end() + 200)]
        cm = CODE_NEAR_RE.search(tail)
        code = cm.group(1) if cm else ''

        out.append({
            'url': u,
            'type': t,
            'label': NETDISK_LABELS.get(t, '其他链接'),
            'variant': variant,
            'code': code,
        })
    return out


def extract_codes(text):
    """兜底：提取全文所有提取码/密码（当就近配对失败时用）"""
    out = []
    seen = set()
    for pat, label in CODE_PATTERNS:
        for m in re.finditer(pat, text or '', re.I):
            v = m.group(1)
            if (label, v) in seen:
                continue
            seen.add((label, v))
            out.append({'label': label, 'value': v})
    return out


# ============ 简介（来自「文章」频道）============
# 标题里形如 [安卓][电脑] 的平台标记（用于筛选版本，如"只要电脑版"）
PLATFORM_WORDS = ['安卓', 'android', '电脑', 'pc', '手机', 'ios', 'mac', 'macos']


def parse_intro(text):
    """解析文章频道正文 → 标题/平台/简介/分类/标签/详情链接"""
    lines = [l.rstrip() for l in (text or '').splitlines()]
    title = ''
    body, categories, tags = [], [], []
    detail = ''
    for l in lines:
        s = l.strip()
        if not s:
            continue
        if not title:
            title = s
            continue
        if s.startswith('分类'):
            categories = re.findall(r'#([^\s#]+)', s)
        elif s.startswith('标签'):
            tags = re.findall(r'#([^\s#]+)', s)
        elif s.startswith('详情'):
            m = re.search(r'https?://\S+', s)
            if m:
                detail = m.group(0)
        else:
            body.append(s)

    # 标题里的 [xxx] 平台标记（只认已知平台词，避免把社团名当平台）
    brackets = re.findall(r'\[([^\[\]]{1,10})\]', title)
    platforms = [b for b in brackets if b.lower() in PLATFORM_WORDS]

    # 去掉开头的【代码】
    title_clean = re.sub(r'^【[^】]*】\s*', '', title).strip()

    return {
        'title': title_clean or title,
        'raw_title': title,
        'platforms': platforms,
        'body': '\n\n'.join(body).strip(),
        'categories': categories,
        'tags': tags,
        'detail_url': detail,
    }


def fetch_intro(code, with_thumb=True):
    """从「文章」频道取该代码的简介（含封面图，存入 THUMB_CACHE）"""
    ensure_client()

    async def _c():
        async for m in _client.iter_messages(ARTICLE_CHANNEL_ID, search=code, limit=1):
            d = parse_intro(m.text or '')
            d['id'] = m.id
            d['date'] = m.date.strftime('%Y-%m-%d %H:%M') if m.date else ''
            d['channel'] = ARTICLE_CHANNEL_NAME
            thumb = None
            if with_thumb and m.media:
                try:
                    thumb = await m.download_media(bytes)
                except Exception:
                    thumb = None
            return d, thumb
        return None, None

    d, thumb = asyncio.run_coroutine_threadsafe(_c(), _loop).result(timeout=120)
    if thumb:
        THUMB_CACHE[(code or '').strip().upper()] = thumb
    if d:
        d['has_thumb'] = bool(thumb)
    return d


def resolve(code, limit=5, with_intro=True):
    """主入口：代码 → 解析结果（含仓库链接 + 文章简介）

    返回：
      {'code':..., 'found':bool, 'messages':[...], 'intro': {...} 或 None}
    """
    code = (code or '').strip()
    msgs = search_channel(code, limit=limit)
    result = []
    for m in msgs:
        result.append({
            'id': m['id'],
            'date': m['date'],
            'text': m['text'],
            'links': extract_links(m['text']),
            'codes': extract_codes(m['text']),
        })
    intro = None
    if with_intro:
        try:
            intro = fetch_intro(code)
        except Exception as e:
            intro = {'error': str(e)}
    return {'code': code, 'found': bool(result), 'messages': result,
            'intro': intro}


if __name__ == '__main__':
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else 'R4191'
    r = resolve(c)
    print(f"代码 {r['code']}  命中 {len(r['messages'])} 条")
    for m in r['messages']:
        print(f"\n[{m['id']}] {m['date']}")
        for l in m['links']:
            extra = f"  变体={l['variant']}" if l['variant'] else ''
            codex = f"  提取码={l['code']}" if l['code'] else ''
            print(f"   [{l['label']}] {l['url'][:70]}{extra}{codex}")
