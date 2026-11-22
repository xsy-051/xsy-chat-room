# 局域网实时聊天室

基于 **uv + Python + WebSocket + SQLite + 原生 HTML/JS/CSS** 的局域网多房间聊天室。支持持久化历史、消息编辑、按 IP 校验的撤回、基于 `msg_id` 的断线补发，并已配置 PWA（可添加到主屏幕）。

## 核心功能

- **SQLite 持久化**：所有房间消息写入 `chat.db`，服务重启后历史仍然存在，不再依赖内存队列。
- **双房间隔离**：登录时选择“房间1 / 房间2”，WebSocket 通过 `?room=room1|room2` 归属房间；在线列表、历史、编辑、撤回和广播完全隔离。
- **按 IP 判断气泡方向**：消息包含发送者 IP；当前 IP 的消息显示在右侧绿色气泡，其他 IP 显示在左侧白色气泡，刷新后依然正确。
- **消息编辑**：发送 IP 可以编辑自己的消息；服务端校验房间和 IP，更新 SQLite 后向同房间广播 `edited` 事件。
- **两分钟内撤回**：发送 IP 可在 2 分钟内撤回；服务端校验房间、IP、`msg_id` 和时间窗口。撤回后从可见历史中移除，并广播撤回提示。
- **断线重连补发**：浏览器记录每个房间最后收到的 `msg_id`，重连时发送 `last_msg_id`；服务端从 SQLite 补发该消息之后的离线消息，离线消息超过最近 50 条也能补回。
- **单端口服务**：HTTP 页面、PWA 资源和 WebSocket 共用 `8765` 端口。
- **IP 去重在线人数**：每个房间独立维护 `{IP: [连接列表]}`，同一 IP 多个窗口只计 1 人；该 IP 在当前房间的最后一个窗口断开时才广播离开。
- **实时在线列表**：显示当前房间成员的昵称、IP 和窗口数。
- **输入校验**：前后端双重校验空白消息和超过 200 字的消息。
- **连接状态与重连**：顶部显示绿色“已连接”和红色“连接已断开，正在重连...”，断线后自动重连。
- **交互细节**：Enter 发送、Shift + Enter 换行、新消息自动滚动到底部。
- **PWA**：包含 `manifest.json`、Service Worker、192/512/maskable 图标；支持安装到主屏幕后独立窗口运行。

## 技术栈

| 类别 | 技术 |
| --- | --- |
| 包管理与环境 | uv |
| 后端 | Python 3.11+、`asyncio`、`websockets` |
| 数据库 | Python `sqlite3`（WAL 模式） |
| 前端 | HTML5、CSS3、原生 JavaScript |
| 实时通信 | WebSocket（JSON） |
| 离线与安装 | PWA Manifest、Service Worker、PNG 图标 |

## 项目结构

```text
.
├── server.py              # HTTP + WebSocket 服务、房间状态、编辑/撤回协议
├── storage.py             # SQLite 数据访问层（历史、编辑、撤回、补发）
├── index.html             # 响应式聊天界面
├── manifest.json          # PWA 配置
├── service-worker.js      # PWA 离线缓存
├── icons/                 # PWA / Apple Touch 图标
├── client.py              # 可选终端客户端（默认房间1）
├── local_dns.py           # Windows 本地地址解析兼容层
├── allow_lan_access.bat   # Windows 防火墙放行脚本（按需）
├── allow_lan_access.ps1   # 防火墙放行逻辑
├── pyproject.toml
├── uv.lock
├── .python-version
└── README.md
```

`chat.db`、WAL 文件、日志和 `.venv` 都会在运行时生成并被 `.gitignore` 排除。

## 快速开始

### 1. 安装 uv

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. 安装依赖并启动

```bash
git clone https://github.com/xsy-051/xsy-chat-room.git
cd xsy-chat-room
uv sync
uv run python server.py
```

启动输出示例：

```text
局域网多房间聊天室已启动
SQLite 数据库：D:\chat\chat.db
本机访问：http://127.0.0.1:8765
局域网访问：http://192.168.1.100:8765
房间：房间1 / 房间2
```

### 3. 使用

- 本机打开 `http://127.0.0.1:8765`。
- 手机/其他电脑连接同一 Wi-Fi 后打开 `http://<电脑局域网 IP>:8765`。
- 先选择房间，再输入昵称进入。
- 自己 IP 的消息在右侧绿色气泡，其他 IP 的消息在左侧白色气泡。
- 自己的消息可点击“编辑”；2 分钟内还可点击“撤回”。
- 断线重连后会自动补发服务端保存的离线消息。

## SQLite 数据

默认数据库为项目目录下的 `chat.db`，首次启动自动创建。主要表结构：

```sql
CREATE TABLE messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT NOT NULL UNIQUE,
    room TEXT NOT NULL,
    name TEXT NOT NULL,
    ip TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    time TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,
    recalled INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER,
    recalled_at INTEGER
);
```

- 新用户首次进入拿到该房间最近 50 条消息。
- 重连时携带 `last_msg_id`；服务端合并最近消息与该 `msg_id` 之后的所有可见消息。
- 撤回使用软删除，被撤回的消息不会出现在历史或补发结果中。
- 编辑直接更新数据库中的消息文本并标记 `edited=1`。

## PWA

已配置：

- `manifest.json`：`standalone` 显示模式、主题色、192/512/maskable 图标。
- `service-worker.js`：缓存应用外壳，页面导航优先走网络，离线时回退到缓存。
- `index.html`：注册 Service Worker，并配置 Apple Touch Icon 与移动端 Web App 元信息。

> 浏览器要求 **HTTPS 或 localhost** 才能完整启用 Service Worker 与 PWA 安装。通过普通 `http://局域网IP` 访问时可以正常聊天，但部分手机浏览器只会创建普通快捷方式，不会提供完整 PWA 安装。若要获得完整安装体验，请配置受信任的 HTTPS 证书。

当前环境没有 Java 和 Android SDK，因此未生成 APK。

## 配置

常量位于 `server.py`：

| 常量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8765` | HTTP 与 WebSocket 端口 |
| `HISTORY_SIZE` | `50` | 首次进入显示的最近历史条数 |
| `MAX_TEXT_LENGTH` | `200` | 单条消息最大字数 |
| `MAX_NAME_LENGTH` | `16` | 昵称最大长度 |
| `RECALL_WINDOW_MS` | `120000` | 同 IP 撤回窗口 |
| `DB_PATH` | `chat.db` | SQLite 数据库文件 |
| `ROOM_NAMES` | `room1 / room2` | 房间标识与显示名 |

## 通信协议

WebSocket：`ws://<host>:8765/ws?room=room1`（或 `room2`）。

客户端发送：

```json
{ "type": "join", "name": "小明", "last_msg_id": "上次收到的消息ID" }
{ "type": "message", "text": "大家好" }
{ "type": "edit", "msg_id": "消息ID", "text": "编辑后的内容" }
{ "type": "recall", "msg_id": "消息ID" }
```

服务端消息：

```json
{ "type": "init", "room": "room1", "history": [], "users": [], "onlineCount": 1, "selfIp": "192.168.1.20", "recallWindowMs": 120000 }
{ "type": "message", "message": { "msg_id": "...", "ip": "192.168.1.20", "name": "小明", "text": "大家好", "created_at": 1760000000000, "edited": false } }
{ "type": "edited", "room": "room1", "msg_id": "...", "text": "编辑后的内容" }
{ "type": "recall", "room": "room1", "msg_id": "...", "name": "小明" }
{ "type": "users", "room": "room1", "users": [], "onlineCount": 1 }
{ "type": "error", "text": "错误说明" }
```

## 验收清单

- [x] SQLite 持久化，服务重启后历史保留。
- [x] 房间1 / 房间2 的在线列表、历史和广播完全隔离。
- [x] 每个房间首次加载最近 50 条历史。
- [x] 消息带发送 IP 和唯一 `msg_id`。
- [x] 按 IP 判断右侧绿气泡 / 左侧白气泡，刷新后保持正确。
- [x] 发送 IP 可以编辑自己的消息，其他 IP 和房间不能编辑。
- [x] 发送 IP 可在 2 分钟内撤回，其他 IP / 房间 / 超时不能撤回。
- [x] 撤回后从可见历史中删除并广播撤回提示。
- [x] 断线重连按 `last_msg_id` 补发离线消息，超过最近 50 条仍有效。
- [x] 同一房间内同一 IP 多窗口只计 1 人。
- [x] 空消息和超过 200 字消息前后端均拒绝。
- [x] PWA manifest、Service Worker、图标可正常加载。
- [x] WebSocket 异常断开不会导致服务端崩溃。

## 安全说明

本项目面向局域网演示和学习，**没有账号、密码或传输加密**。请勿将端口直接映射到公网。生产部署需要 TLS、身份认证、限流和备份策略。

## 可选终端客户端

`client.py` 默认连接房间1，仅用于终端调试，不支持房间选择、编辑和撤回：

```bash
uv run python client.py 用户A
```

输入 `/quit` 退出。完整功能请使用网页端。
