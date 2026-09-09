# 灵梦御所下载器 (Reimu Downloader)

一个浏览器访问的 Web 下载工具：输入灵梦御所的资源代码（如 `R4191`、`S5271`），
自动在 Telegram 频道搜索对应发布，提取 MEGA / 磁力 / 百度网盘等下载链接，并把 MEGA 资源直接下载到本机。

> 灵感来自 MegaDownloader。这是其 Web 化 + 自动化搜源版本。

## 功能特性

- 🌐 **纯浏览器操作**：本机 `http://127.0.0.1:8765` 访问，电脑/手机/平板都能用
- 🔍 **自动搜源**：输入代码 → Telegram 频道搜索 → 解析链接 + 提取码 + 变体（WinCrypt/Encrypto 等）
- ⬇️ **MEGA 直连下载**：服务端流式下载 + AES 解密，带断点续传（.part）、进度、子目录还原
- 📊 **实时进度**：每个文件的速度 / 剩余时间 / 进度条
- 📁 **文件级操作**：右键暂停 / 继续 / 取消 / 查看连接 / 打开目录 / 优先级
- 🗂️ **目录浏览器**：网页内浏览服务器磁盘，自选保存位置，支持新建文件夹
- 🔁 **任务持久化**：刷新 / 重启服务任务不丢，.part 可续传

## 工作原理

灵梦御所的投稿分在两个公开 Telegram 频道，代码规则如下：

| 代码前缀 | 内容 | 说明 |
|---|---|---|
| `Rxxxx` | MEGA 资源 | 动画/游戏/同人，通常单文件或文件夹，MEGA 直链 |
| `Sxxxx` | 多网盘资源 | 磁力 / 百度盘 / TeraBox 等多源（也可能含 MEGA） |

- `R` 开头：WebUI 直接流式下载到本机
- `S` 开头 / 磁力 / 百度盘：返回链接，在你当前设备点开，系统唤起对应 App

## 环境要求

- Python 3.9+
- 能访问 Telegram（国内网络可能需要代理，见下）

## 安装

```bash
git clone <你的仓库地址>
cd reimu-downloader
pip install -r requirements.txt
```

## 配置 Telegram 凭据（必做）

本工具用你自己的 Telegram 账号去搜频道（公开频道，任何人都能搜）。**别人无法共享你的账号**，
所以每个人需要用自己的凭据，三选一：

### 方式 1：本目录 `telegram_config.json`（推荐）

1. 去 <https://my.telegram.org> → API development tools → 申请 `api_id` 和 `api_hash`
2. 生成 session string（一次性）：

```bash
python -c "from telethon.sync import TelegramClient; from telethon.sessions import StringSession; \
c=TelegramClient(StringSession(), API_ID, 'API_HASH'); c.start(); print(c.session.save()); c.disconnect()"
```

（会提示输入手机号 + 验证码，把打印出的 session 字符串保存）

3. 复制 `telegram_config.example` 为 `telegram_config.json` 并填入：

```json
{
  "api_id": 1234567,
  "api_hash": "你的api_hash",
  "session_string": "你的session字符串"
}
```

### 方式 2：环境变量

```bash
export TG_API_ID=1234567
export TG_API_HASH=你的api_hash
export TG_SESSION_STRING=你的session
```

（如需代理再加 `TG_PROXY_HOST` / `TG_PROXY_PORT` / `TG_PROXY_TYPE`）

### 方式 3：已有 WorkBuddy 的 mcp.json（仅开发者本机）

如果你的 `~/.workbuddy/mcp.json` 里有 telegram-mcp 配置，程序会自动识别用作回退。

## 启动

```bash
python lmys_web.py
# 指定端口：python lmys_web.py --port 8765
# 局域网访问：python lmys_web.py --host 0.0.0.0
```

浏览器打开 http://127.0.0.1:8765 （局域网设备访问时用电脑的局域网 IP）。

## 使用

1. 在顶部输入框输入代码，如 `R4191` → 点「解析」
2. 会显示下载链接 + 提取码 + 简介；点「下载到本机」进入下载
3. 下载区选择保存目录（网页内目录浏览器）→ 确定
4. 下方任务区实时显示进度

下载目录默认在你用户主目录下的 `VRGAME/`，可用环境变量 `REIMU_SAVE_DIR` 改。

## MEGA 下载代理

MEGA API 默认直连。如果你的网络访问 MEGA 需要代理，设环境变量 `MEGA_PROXY`：

```bash
export MEGA_PROXY=http://127.0.0.1:7892   # 你的代理地址
```

## 常见问题

**Q: Telegram 凭据报错？**
确认 `telegram_config.json` 存在且填对了 api_id/session，或设置了环境变量。

**Q: 搜不到代码？**
确认你的 Telegram 账号加入了「灵梦御所 仓库」和「文章」两个公开频道（加入后才能搜到其历史消息）。

**Q: MEGA 下载慢 / 报 509？**
MEGA 匿名下载有配额（约 5-10GB/6小时，按 IP）。等配额恢复或设代理换出口 IP。

## 许可

仅供学习交流。请遵守 Telegram / MEGA 服务条款及当地法律。
