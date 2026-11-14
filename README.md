# 局域网实时聊天室

一个基于 **uv + Python + WebSocket + 原生 HTML/JS/CSS** 的局域网网页聊天室。网页和 WebSocket 共用同一个端口，打开浏览器即可实时聊天，适合课堂演示、局域网协作和 WebSocket 入门学习。

## 核心功能

- **微信/QQ 风格气泡 UI**：自己的消息靠右，其他人的消息靠左。
- **单端口服务**：HTTP 页面和 WebSocket 共用 `8765` 端口，只需要一个访问地址。
- **IP 去重在线人数**：服务端维护 `{IP: [连接列表]}`，同一 IP 打开多个窗口只计 1 个在线用户；该 IP 的最后一个窗口断开时才广播离开。
- **实时在线列表**：展示在线成员的昵称、IP 和窗口数。
- **历史消息**：服务端在内存中保留最近 50 条消息，新用户加入时自动推送。
- **输入校验**：前后端双重校验空白消息和超过 200 字的消息，并弹出提示。
- **连接状态与重连**：顶部状态栏显示绿色“已连接”和红色“连接已断开，正在重连...”，断线后自动重连。
- **交互细节**：Enter 发送、Shift + Enter 换行、新消息自动滚动到底部。
- **响应式布局**：桌面端侧边栏展示在线列表，移动端使用抽屉式在线成员面板。

## 技术栈

| 类别 | 技术 |
| --- | --- |
| 包管理与环境 | [uv](https://docs.astral.sh/uv/) |
| 后端 | Python 3.11+、`asyncio`、[`websockets`](https://websockets.readthedocs.io/) |
| 前端 | HTML5、CSS3、原生 JavaScript |
| 实时通信 | WebSocket（JSON 消息） |
| HTTP 静态页面 | 由 `websockets` 的握手前请求处理统一提供 |

服务端使用 `websockets` 的异步接口；普通 HTTP 请求返回 `index.html`，`/ws` 请求进入 WebSocket 握手，因此无需额外的 HTTP 服务或反向代理。

## 项目结构

```text
.
├── server.py              # HTTP + WebSocket 服务、广播、IP 去重、历史记录
├── index.html             # 响应式聊天界面（内联 CSS / JavaScript）
├── client.py              # 可选的旧终端客户端
├── local_dns.py           # Windows Smart App Control 环境下的本地地址解析兼容层
├── allow_lan_access.bat   # Windows 防火墙放行脚本（按需使用）
├── allow_lan_access.ps1   # 防火墙放行逻辑
├── pyproject.toml         # Python 项目与依赖声明
├── uv.lock                # uv 锁定依赖版本
├── .python-version        # Python 版本约束
└── README.md
```

## 快速开始

### 1. 安装 uv

如果尚未安装 uv，请参考官方文档安装：

```bash
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS / Linux：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 克隆并启动

```bash
git clone https://github.com/xsy-051/xsy-chat-room.git
cd xsy-chat-room

# 根据 uv.lock 创建虚拟环境并安装依赖
uv sync

# 启动聊天室
uv run python server.py
```

启动成功后会打印类似内容：

```text
局域网聊天室已启动
本机访问：http://127.0.0.1:8765
局域网访问：http://192.168.1.100:8765
WebSocket：ws://192.168.1.100:8765/ws
```

无需手动激活虚拟环境，`uv run` 会自动使用项目环境。

### 3. 开始聊天

- 在本机浏览器打开：`http://127.0.0.1:8765`
- 同一 Wi-Fi / 局域网内其他设备打开：`http://<电脑局域网 IP>:8765`
- 输入昵称后即可进入房间；打开多个浏览器标签或不同设备即可测试多人聊天。

## 局域网访问与防火墙

如果其他设备无法访问，请检查：

1. 设备与服务器在同一局域网，且没有被路由器的“AP 隔离 / 设备隔离”限制。
2. 系统防火墙允许入站 TCP `8765`。
3. 使用当前网卡的局域网 IPv4 地址，而不是 `127.0.0.1`。

Windows 下可参考项目内的 `allow_lan_access.bat`。它会请求管理员权限，为本机子网放行 TCP 8765。运行前请阅读 `allow_lan_access.ps1`，了解它所做的具体修改；脚本仅适用于本项目的演示环境。

## 可选配置

以下常量位于 `server.py` 顶部，可按需修改：

| 常量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8765` | HTTP 与 WebSocket 监听端口 |
| `HISTORY_SIZE` | `50` | 内存中保留的历史消息条数 |
| `MAX_TEXT_LENGTH` | `200` | 单条消息最大字数 |
| `MAX_NAME_LENGTH` | `16` | 昵称最大长度 |

注意：历史记录保存在服务端内存中，重启服务后会清空。

## 通信协议

浏览器与服务器之间使用 JSON 文本消息。

客户端发送：

```json
{ "type": "join", "name": "小明", "clientId": "browser-tab-id" }
{ "type": "message", "text": "大家好" }
```

服务端推送：

```json
{ "type": "init", "history": [], "users": [], "onlineCount": 1, "selfIp": "192.168.1.20" }
{ "type": "message", "message": { "id": "...", "name": "小明", "text": "大家好", "time": "20:30:00" } }
{ "type": "users", "users": [], "onlineCount": 1 }
{ "type": "system", "text": "小红 加入了聊天室", "time": "20:30:05" }
```

## 验收清单

- [x] 自己发送的消息显示在右侧，别人发送的消息显示在左侧。
- [x] 按 Enter 发送，Shift + Enter 换行。
- [x] 新消息出现后聊天区域自动滚动到底部。
- [x] 新用户加入时收到最近 50 条历史消息。
- [x] 同一 IP 的多个窗口只在在线列表中计 1 人。
- [x] 只有该 IP 的最后一个窗口断开时才广播离开。
- [x] 在线成员列表实时显示昵称、IP 和窗口数。
- [x] 空消息与超过 200 字的消息被前端和后端拒绝。
- [x] 连接断开显示红色状态并自动重连。
- [x] WebSocket 异常断开不会导致服务端崩溃。

## 安全说明

本项目面向局域网演示与学习，**没有账号、密码或消息加密**。请勿将端口直接暴露到公网。若需要公网部署，应增加 TLS、身份认证、限流和持久化存储。

## 可选终端客户端

保留的 `client.py` 仅用于基础的终端调试：

```bash
uv run python client.py 用户A
```

输入 `/quit` 退出。网页端是完整功能入口。
