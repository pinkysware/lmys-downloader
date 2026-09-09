# -*- coding: utf-8 -*-
"""
app_main.py — PyInstaller 打包入口（一键启动，无窗口版）

双击 exe：
  1. 若 8765 已被占用（服务已在跑）→ 直接打开浏览器
  2. 否则 → 后台启动 Web 服务 + 后台预载云端数据 → 自动打开浏览器 → 服务常驻

运行信息写入 exe 同目录 reimu.log（无窗口时便于排查）。
"""
import os
import sys
import time
import socket
import threading
import webbrowser

PORT = 8765
URL = f'http://127.0.0.1:{PORT}'

# 清除可能干扰 urllib 直连的系统/沙箱代理（exe 分发更干净，避免误走代理被拦）
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

# 无窗口(-w)时没有可用控制台 stdout；统一写 exe 同目录日志
_LOG = os.path.join(
    os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)),
    'reimu.log',
)


def log(msg):
    line = time.strftime('%H:%M:%S ') + msg
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(_LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def port_in_use(port):
    try:
        s = socket.create_connection(('127.0.0.1', port), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


def run_server():
    import lmys_web
    try:
        lmys_web.app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
    except Exception as e:
        log(f'服务启动失败: {e}')


def preload_data():
    """后台预载云端资源数据。本地有缓存则秒用；否则尝试从云端拉取，失败静默。"""
    try:
        import warehouse_data
        st = warehouse_data.init(timeout=25)
        log(f'云端资源就绪: {st.get("count")} 条 (更新于 {st.get("updated_at", "?")})')
    except Exception as e:
        log(f'云端资源加载失败(不影响启动): {e}')


def open_browser_later():
    for _ in range(20):
        if port_in_use(PORT):
            break
        time.sleep(0.5)
    try:
        webbrowser.open(URL)
    except Exception:
        pass
    log('已在浏览器打开 ' + URL)


def main():
    log('=' * 40)
    log('灵梦御所下载器 启动…')

    # 若已运行：只开浏览器，不重复起服务
    if port_in_use(PORT):
        log(f'服务已在运行 {URL}，直接打开浏览器')
        webbrowser.open(URL)
        return

    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=preload_data, daemon=True).start()
    threading.Thread(target=open_browser_later, daemon=True).start()
    log(f'服务已启动: {URL}  (常驻后台，关闭本进程即停止)')

    # 无窗口模式下主进程必须存活，服务线程才不退出
    try:
        while True:
            time.sleep(3)
    except KeyboardInterrupt:
        log('已退出')


if __name__ == '__main__':
    main()
