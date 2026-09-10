# -*- coding: utf-8 -*-
"""
mega_core.py — MEGA 下载核心（无 UI，可独立测试）

功能：
  1. enum_tree(folder链接) —— 枚举 folder，返回带子目录层级的文件清单
  2. download_file(...) —— 流式下载单文件，支持：
       - 断点续传（.part + Range + AES-CTR 计数器复位）
       - 真暂停 / 取消（在 chunk 边界停下，已下数据不丢）
       - 失败自动重试（指数退避）
       - HTTP 509 配额等待（下满额度 → 等 → 接着下）
       - 完成后大小校验 + 原子改名

加密算法移植自 mega.py（已验证）+ MegaDownloader-Revival。

注意：本脚本用系统 Python 3.14 运行（有 tkinter + pycryptodome + requests）。
"""

import sys
import json
import base64
import struct
import urllib.request
import re
import time
import os

import requests

from Crypto.Cipher import AES
from Crypto.Util import Counter

MEGA_API = "https://g.api.mega.co.nz/cs"
import os as _os
# MEGA API 代理。初始值取自环境变量 MEGA_PROXY，默认空字符串=直连。
# 注意：这是模块级可变全局，WebUI 保存配置时会由 lmys_web 动态修改（mega_core.PROXY = ...）。
# 设为空字符串则强制直连（ProxyHandler({}) 绕过系统代理）。
PROXY = _os.environ.get('MEGA_PROXY', '')


def _cur_proxy(proxy):
    """解析代理：显式传 None 时使用当前模块全局 PROXY（支持运行时动态改）。"""
    return PROXY if proxy is None else proxy


# ============ 加密基础（对照 mega.py crypto.py）============
def b64url_decode(s):
    s += '=='[(2 - len(s) * 3) % 4:]
    for a, b in (('-', '+'), ('_', '/'), (',', '')):
        s = s.replace(a, b)
    return base64.b64decode(s)


def b64url_encode(b):
    return base64.b64encode(b).decode().replace('+', '-').replace('/', '_').replace('=', '')


def a32_to_bytes(a):
    return b''.join(struct.pack('>I', x & 0xFFFFFFFF) for x in a)


def bytes_to_a32(b):
    return [struct.unpack('>I', b[i*4:i*4+4])[0] for i in range(len(b)//4)]


def b64_to_a32(s):
    b = b64url_decode(s)
    if len(b) % 4:
        b += b'\0' * (4 - len(b) % 4)
    return bytes_to_a32(b)


def decrypt_key_blocks(data_a32, key_a32):
    """decrypt_key：逐 4 个 a32 块 AES-CBC 解密，key 用前 4 个 a32"""
    key = a32_to_bytes(key_a32[:4])
    out = b''
    for i in range(0, len(data_a32), 4):
        blk = a32_to_bytes(data_a32[i:i+4])
        out += AES.new(key, AES.MODE_CBC, b'\0'*16).decrypt(blk)
    return bytes_to_a32(out)


def get_chunks(size):
    p = 0
    s = 0x20000  # 128KB 起
    while p + s < size:
        yield (p, s)
        p += s
        if s < 0x100000:  # 递增到 1MB
            s += 0x20000
    yield (p, size - p)


def chunks_from(size, offset):
    """与 get_chunks 完全相同的切分，但从 offset 所在块开始产出。
    offset 必须是 chunk 边界（由 resume_offset 保证）。"""
    p = 0
    s = 0x20000
    while p + s < size:
        if p >= offset:
            yield (p, s)
        p += s
        if s < 0x100000:
            s += 0x20000
    if p >= offset:
        yield (p, size - p)


def resume_offset(size, offset):
    """返回 <= offset 的最大 chunk 边界，作为续传起点。

    为什么必须对齐到 chunk 边界：AES-CTR 的计数器在整个文件上连续递增，
    从任意字节续传都需要 counter = base + offset//16 精确复位；而 get_chunks
    的每个块长度都是 0x20000(131072) 的倍数，131072 % 16 == 0，所以
    chunk 边界一定是 16 的倍数，不会产生半块残留。
    """
    if offset >= size:
        return size
    last = 0
    p = 0
    s = 0x20000
    while p + s < size:
        if p + s > offset:
            break
        p += s
        last = p
        if s < 0x100000:
            s += 0x20000
    return last


def read_exact(raw, n):
    """从 urllib3 原始流精确读满 n 字节（raw.read 可能短读）。
    返回不足 n 字节表示流已结束。"""
    buf = b''
    while len(buf) < n:
        d = raw.read(n - len(buf))
        if not d:
            break
        buf += d
    return buf


def parse_mega_link_full(url):
    """解析 MEGA 链接，返回 (type, handle, key, sub_handle)。

    支持子文件夹分享链接：mega.nz/folder/<root>#<key>/folder/<sub>
      -> sub_handle 非 None，表示只关注该子文件夹（MEGA 存档链接用）。
    普通链接 / 老式 #F! 链接的 sub_handle 为 None。
    """
    url = (url or '').strip()
    m = re.search(r'mega\.nz/folder/([^#/]+)#([^/\s]+)(?:/folder/([^/\s]+))?', url)
    if m:
        return 'folder', m.group(1), m.group(2), m.group(3)
    m = re.search(r'mega\.nz/file/([^#/]+)#([^/\s]+)', url)
    if m:
        return 'file', m.group(1), m.group(2), None
    m = re.search(r'mega\.nz/#(F?)!([^!]+)!([^/\s]+)', url)
    if m:
        t = 'folder' if m.group(1) == 'F' else 'file'
        return t, m.group(2), m.group(3), None
    return None, None, None, None


def parse_mega_link(url):
    """解析 MEGA 链接，返回 (type, handle, key)（兼容旧调用）"""
    t, h, k, _ = parse_mega_link_full(url)
    return t, h, k


def _opener(proxy=None):
    """构造 opener。proxy 非空走指定代理；为空字符串时用 ProxyHandler({}) 显式直连，
    绕过系统 http_proxy 环境变量。proxy=None 时读当前全局 PROXY。"""
    proxy = _cur_proxy(proxy)
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    # 空字典 = 不使用任何代理，强制直连（覆盖系统代理环境变量）
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api_request(payload, proxy=None, n=None):
    """MEGA API 请求，带重试。n 参数放在 URL query（folder 枚举需要）。"""
    url = f"{MEGA_API}?id=0"
    if n:
        url += f"&n={n}"
    req = urllib.request.Request(url, data=json.dumps([payload]).encode(),
                                 headers={'Content-Type': 'application/json'})
    op = _opener(proxy)
    for _ in range(8):
        try:
            return json.loads(op.open(req, timeout=30).read().decode('utf-8', 'ignore'))
        except Exception:
            time.sleep(1.5)
    raise RuntimeError('MEGA API 请求失败')


# ============ folder 枚举（含子目录层级）============
def _decrypt_attr_name(node, key_a32):
    """解密节点属性得到名字。folder 用 key[:4]，file 用 8a32 XOR 前4后4"""
    if node.get('t') == 1:
        aes_key = a32_to_bytes(key_a32[:4])
    else:
        if len(key_a32) < 8:
            return None
        k = (key_a32[0] ^ key_a32[4], key_a32[1] ^ key_a32[5],
             key_a32[2] ^ key_a32[6], key_a32[3] ^ key_a32[7])
        aes_key = a32_to_bytes(list(k))
    attr_b = AES.new(aes_key, AES.MODE_CBC, b'\0'*16).decrypt(
        b64url_decode(node['a'])).rstrip(b'\0')
    if attr_b[:4] != b'MEGA':
        return None
    try:
        return json.loads(attr_b[4:].decode('utf-8', 'ignore')).get('n')
    except Exception:
        return None


def enum_tree(folder_id, folder_key, proxy=None, sub_handle=None):
    """枚举 folder，返回带子目录层级的文件清单。

    父文件夹归属改用节点自带的 p 字段（直接父句柄）向上回溯，
    比旧版「从 k 字段段数推断」可靠得多，且支持任意深度嵌套。
    （旧版在匿名分享下会把子目录里的文件错判到根目录）

    返回 list[dict]：
      { 'relpath': 'sub/inner.bin' 或 'big.bin',  # 相对路径（含子目录）
        'name': 文件名,
        'size': 字节数,
        'handle': 文件 handle,
        'file_key': 文件下载 key（8 a32 的 list）,
      }
    """
    data = api_request({"a": "f", "c": 1, "r": 1}, proxy, n=folder_id)
    if isinstance(data, list) and data and isinstance(data[0], int):
        raise Exception(f"MEGA 返回错误码 {data[0]}")
    nodes = data[0]['f']
    fk = b64_to_a32(folder_key)
    handles = {n['h'] for n in nodes}

    # 1. 解密所有节点名字（folder 用 4a32，file 用 8a32 XOR）
    names = {}
    for n in nodes:
        for seg in n['k'].split('/'):
            if ':' not in seg:
                continue
            _, kseg = seg.split(':', 1)
            try:
                key = decrypt_key_blocks(b64_to_a32(kseg), fk)
                nm = _decrypt_attr_name(n, key)
                if nm:
                    names[n['h']] = nm
                    break
            except Exception:
                continue
        if n['h'] not in names:
            names[n['h']] = n['h']

    # 2. 父映射；分享根 = 其 p 不在本次返回节点集合内的节点
    parent_of = {n['h']: n.get('p') for n in nodes}
    roots = {h for h, pp in parent_of.items() if pp not in handles}
    if not roots:
        roots = {n['h'] for n in nodes
                 if n.get('t') == 1 and n['k'].split('/')[0].split(':', 1)[0] == n['h']}

    def build_dir(h):
        """向上回溯拼出 h 所在目录（不含根）。指定 sub_handle 时回溯到该子文件夹即停。"""
        parts = []
        cur = parent_of.get(h)
        seen = set()
        while cur and cur in handles and cur not in roots and cur not in seen:
            if sub_handle and cur == sub_handle:
                break
            seen.add(cur)
            parts.append(names.get(cur, cur))
            cur = parent_of.get(cur)
        parts.reverse()
        return '/'.join(parts)

    # 3. 文件清单
    results = []
    for n in nodes:
        if n.get('t') != 0:
            continue
        # 指定 sub_handle 时，只保留该子文件夹子树下的文件
        if sub_handle:
            ok = False
            cur = n.get('p')
            seen2 = set()
            while cur and cur not in seen2:
                if cur == sub_handle:
                    ok = True
                    break
                seen2.add(cur)
                cur = parent_of.get(cur)
            if not ok:
                continue
        h = n['h']
        name = names.get(h, h)

        # 下载 key：能解出本节点名字的那个 8a32
        file_key = None
        for seg in n['k'].split('/'):
            if ':' not in seg:
                continue
            _, kseg = seg.split(':', 1)
            try:
                fkey = decrypt_key_blocks(b64_to_a32(kseg), fk)
                if len(fkey) < 8:
                    continue
                if file_key is None:
                    file_key = fkey
                if _decrypt_attr_name(n, fkey) == name:
                    file_key = fkey
                    break
            except Exception:
                continue

        rel = build_dir(h)
        relpath = (rel + '/' + name) if rel else name
        results.append({
            'relpath': relpath,
            'name': name,
            'size': n.get('s', 0),
            'handle': h,
            'file_key': file_key,
        })
    return results


# ============ 快速预检（单次请求、短超时、不重试）============
def quick_stat(url, timeout=12):
    """快速获取 MEGA 链接的文件数/总大小，供 UI 下载前预览。
    单次请求、短超时、不重试（慢/失败时快速返回，不阻塞主流程）。
    返回 (count, total_size)。失败抛异常（由调用方容错）。

    注意：folder 用 a:f 一次拿整棵树；file 用 a:g 拿单文件大小。
    不复用 api_request（那是 8 次重试×1.5s×30s，太重）。
    """
    typ, handle, key = parse_mega_link(url)
    if not typ:
        raise RuntimeError('无法解析链接')

    # 轻量单次请求
    payload = {"a": "f", "c": 1, "r": 1} if typ == 'folder' else {"a": "g", "g": 1, "p": handle}
    req_url = f"{MEGA_API}?id=0"
    if typ == 'folder':
        req_url += f"&n={handle}"
    req = urllib.request.Request(req_url, data=json.dumps([payload]).encode(),
                                 headers={'Content-Type': 'application/json'})
    op = _opener()
    try:
        data = json.loads(op.open(req, timeout=timeout).read().decode('utf-8', 'ignore'))
    except Exception as e:
        raise RuntimeError(f'MEGA 请求失败: {e}')

    if isinstance(data, list) and data and isinstance(data[0], int):
        raise RuntimeError(f'MEGA 错误码 {data[0]}')

    if typ == 'folder':
        nodes = data[0]['f']
        total = 0
        cnt = 0
        for n in nodes:
            if n.get('t') == 0:  # 只统计文件
                total += n.get('s', 0)
                cnt += 1
        return cnt, total
    else:
        d0 = data[0]
        return 1, d0.get('s', 0)


# ============ 下载 ============

class FatalError(Exception):
    """不可重试的致命错误（链接失效、文件被删等）"""


def _get_download_url(handle, folder_id, is_public, proxy=None):
    """a:g 取临时下载 URL。匿名 folder 文件必须带 enp。"""
    if is_public:
        payload = {"a": "g", "g": 1, "p": handle}
    else:
        payload = {"a": "g", "g": 1, "n": handle}
        if folder_id:
            payload["enp"] = folder_id
    data = api_request(payload, proxy)
    if isinstance(data, list) and data and isinstance(data[0], int):
        raise FatalError(f"a:g 错误码 {data[0]}")
    if not data or 'g' not in data[0]:
        raise FatalError("文件不可访问（可能已失效）")
    return data[0]['g']


def _sleep_interruptable(sec, pause_ev, cancel_ev, step=1.0):
    """可中断的 sleep。返回 'cancel' / 'paused' / None"""
    end = time.time() + sec
    while True:
        if cancel_ev is not None and cancel_ev.is_set():
            return 'cancel'
        if pause_ev is not None and pause_ev.is_set():
            return 'paused'
        left = end - time.time()
        if left <= 0:
            return None
        time.sleep(min(step, left))


def _sync_part(part_path, size):
    """把 .part 截断到最后一个完整 chunk 边界，返回续传起点 offset。"""
    if not os.path.exists(part_path):
        return 0
    have = os.path.getsize(part_path)
    off = resume_offset(size, have)
    if off != have:
        with open(part_path, 'r+b') as f:
            f.truncate(off)
    return off


def download_file(handle, file_key_a32, size, dest_path, progress_cb=None,
                  proxy=None, folder_id=None, is_public=False,
                  pause_ev=None, cancel_ev=None, max_retries=5, log=None,
                  rate_limit=0):
    """下载单个文件到 dest_path（支持续传/暂停/重试/配额等待/校验）。

    全程写 dest_path + '.part'，完成后 os.replace 原子改名为正式文件。
    file_key_a32 是 8 个 a32 的 list。
    progress_cb(downloaded_bytes) 每块调用一次（传的是绝对已下载量）。
    pause_ev / cancel_ev 是 threading.Event，在 chunk 边界被检查。
    rate_limit: 限速（字节/秒），0 = 不限。

    返回 (status, downloaded)，status ∈ {'done','paused','cancel','error'}
    """
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    d = os.path.dirname(dest_path)
    if d:
        os.makedirs(d, exist_ok=True)
    part_path = dest_path + '.part'

    # 已经完整下载过？
    if os.path.exists(dest_path) and os.path.getsize(dest_path) == size:
        if progress_cb:
            progress_cb(size)
        return ('done', size)

    offset = _sync_part(part_path, size)
    if progress_cb:
        progress_cb(offset)

    # 解密参数
    k = (file_key_a32[0] ^ file_key_a32[4], file_key_a32[1] ^ file_key_a32[5],
         file_key_a32[2] ^ file_key_a32[6], file_key_a32[3] ^ file_key_a32[7])
    iv = list(file_key_a32[4:6]) + [0, 0]
    base = ((iv[0] << 32) + iv[1]) << 64
    k_str = a32_to_bytes(list(k))

    proxy = _cur_proxy(proxy)
    # proxy 非空走代理；为空时 trust_env=False 强制 requests 直连
    # （否则 requests 会读系统 http_proxy 环境变量，走错代理导致 502/超时）
    proxies = {'http': proxy, 'https': proxy} if proxy else None
    _trust_env = bool(proxy)

    retry = 0
    quota_wait = 60          # 509 配额等待：60s 起，逐次加倍，上限 30min
    quota_round = 0

    while True:
        # --- 中断检查 ---
        if cancel_ev is not None and cancel_ev.is_set():
            return ('cancel', offset)
        if pause_ev is not None and pause_ev.is_set():
            return ('paused', offset)

        # --- 1. 取下载 URL（每次都重新取，g-URL 有时效）---
        try:
            file_url = _get_download_url(handle, folder_id, is_public, proxy)
        except FatalError as e:
            _log(f"致命错误: {e}")
            return ('error', offset)
        except Exception as e:
            retry += 1
            if retry > max_retries:
                _log(f"重试 {max_retries} 次后仍失败: {e}")
                return ('error', offset)
            wait = min(2 ** (retry - 1), 16)
            _log(f"取下载地址失败({retry}/{max_retries})，{wait}s 后重试")
            offset = _sync_part(part_path, size)
            if _sleep_interruptable(wait, pause_ev, cancel_ev) is not None:
                continue
            continue

        # --- 2. Range 请求 ---
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            if offset > 0:
                headers['Range'] = f'bytes={offset}-'
            resp = requests.get(file_url, headers=headers, stream=True,
                                proxies=proxies, timeout=(15, 60),
                                trust_env=_trust_env)
        except Exception as e:
            retry += 1
            if retry > max_retries:
                _log(f"连接失败 {max_retries} 次: {e}")
                return ('error', offset)
            wait = min(2 ** (retry - 1), 16)
            _log(f"连接失败({retry}/{max_retries})，{wait}s 后重试")
            offset = _sync_part(part_path, size)
            _sleep_interruptable(wait, pause_ev, cancel_ev)
            continue

        # --- 3. 配额 509：不重试，改为等待 ---
        if resp.status_code == 509:
            resp.close()
            quota_round += 1
            _log(f"MEGA 配额已用尽(509)，等待 {quota_wait}s 后自动续传"
                 f"（第 {quota_round} 轮）")
            r = _sleep_interruptable(quota_wait, pause_ev, cancel_ev)
            if r == 'cancel':
                return ('cancel', offset)
            if r == 'paused':
                return ('paused', offset)
            quota_wait = min(quota_wait * 2, 1800)
            offset = _sync_part(part_path, size)
            continue

        # --- 4. 服务器不支持 Range（要了 range 却返回 200）→ 全量重下 ---
        if resp.status_code == 200 and offset > 0:
            resp.close()
            _log("服务器不支持断点续传，改为从头下载")
            with open(part_path, 'r+b') as f:
                f.truncate(0)
            offset = 0
            if progress_cb:
                progress_cb(0)
            continue

        if resp.status_code not in (200, 206):
            resp.close()
            retry += 1
            if retry > max_retries:
                _log(f"下载失败，HTTP {resp.status_code}")
                return ('error', offset)
            wait = min(2 ** (retry - 1), 16)
            _log(f"HTTP {resp.status_code}({retry}/{max_retries})，{wait}s 后重试")
            offset = _sync_part(part_path, size)
            _sleep_interruptable(wait, pause_ev, cancel_ev)
            continue

        # --- 5. 流式解密写盘 ---
        try:
            counter = Counter.new(128, initial_value=base + offset // 16)
            aes = AES.new(k_str, AES.MODE_CTR, counter=counter)
            raw = resp.raw
            raw.decode_content = True

            mode = 'r+b' if offset > 0 else 'wb'
            with open(part_path, mode) as fh:
                if offset > 0:
                    fh.seek(offset)
                for _cs, csize in chunks_from(size, offset):
                    if cancel_ev is not None and cancel_ev.is_set():
                        return ('cancel', offset)
                    buf = read_exact(raw, csize)
                    if not buf:
                        break
                    fh.write(aes.decrypt(buf))
                    offset += len(buf)
                    if progress_cb:
                        progress_cb(offset)
                    if pause_ev is not None and pause_ev.is_set():
                        return ('paused', offset)
                    # 限速：让"慢速下载"场景可控，也便于测试暂停/继续
                    if rate_limit > 0:
                        expect = csize / rate_limit
                        time.sleep(min(expect, 0.5))
        except Exception as e:
            retry += 1
            if retry > max_retries:
                _log(f"传输中断 {max_retries} 次: {e}")
                return ('error', offset)
            wait = min(2 ** (retry - 1), 16)
            _log(f"传输中断({retry}/{max_retries})，{wait}s 后从断点续传")
            offset = _sync_part(part_path, size)
            _sleep_interruptable(wait, pause_ev, cancel_ev)
            continue
        finally:
            try:
                resp.close()
            except Exception:
                pass

        # --- 6. 完成校验 ---
        got = os.path.getsize(part_path)
        if got != size:
            retry += 1
            if retry > max_retries:
                _log(f"大小校验失败: 期望 {size}，实际 {got}")
                return ('error', got)
            _log(f"文件不完整({got}/{size})，从断点续传")
            offset = _sync_part(part_path, size)
            _sleep_interruptable(min(2 ** (retry - 1), 16), pause_ev, cancel_ev)
            continue

        os.replace(part_path, dest_path)
        return ('done', size)


if __name__ == '__main__':
    # 自测：枚举 folder
    if len(sys.argv) > 1:
        url = sys.argv[1]
        typ, handle, key = parse_mega_link(url)
        if typ == 'folder':
            print(f"枚举 folder {handle} ...")
            files = enum_tree(handle, key)
            print(f"共 {len(files)} 个文件:")
            for f in files:
                print(f"  {f['relpath']}  ({f['size']/1024/1024:.1f}MB)")
        else:
            print("请传 folder 链接")
    else:
        print("用法: python mega_core.py <folder链接>")
