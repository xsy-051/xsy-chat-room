"""局域网网页聊天室：HTTP 静态页面 + WebSocket 实时消息，共用 8765 端口。

运行方式：
    cd D:\chat
    uv run python server.py

浏览器打开启动时打印的局域网地址即可。网页请求由 / 返回，WebSocket
握手使用 /ws（旧终端客户端连接根路径也仍然兼容）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import sys
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from local_dns import install_dns_compat
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

# 处理当前电脑上 Smart App Control 导致的本地地址解析问题。
install_dns_compat()

HOST = "0.0.0.0"
PORT = 8765
MAX_NAME_LENGTH = 16
MAX_TEXT_LENGTH = 200
HISTORY_SIZE = 50
MAX_FRAME_SIZE = 64 * 1024

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("lan-chat")


@dataclass
class ClientState:
    websocket: ServerConnection
    ip: str
    name: str
    client_id: str
    joined: bool = False


# 每个 TCP/WebSocket 连接对应一个 ClientState。
CLIENTS: dict[ServerConnection, ClientState] = {}

# 核心去重结构：{IP: [连接列表]}。同一 IP 的所有窗口都放在同一个列表中。
IP_CLIENTS: dict[str, list[ServerConnection]] = {}

# 服务端内存中的最近 50 条聊天消息。
HISTORY: deque[dict] = deque(maxlen=HISTORY_SIZE)

# 串行广播，避免多人同时发言时消息交叉写入。
BROADCAST_LOCK = asyncio.Lock()


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def normalize_ip(value: str | None) -> str:
    """把 IPv4-mapped IPv6 地址统一成 IPv4，并按 IP 字符串分组。"""
    if not value:
        return "未知 IP"
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def clean_name(value: object) -> str:
    """去除控制字符、首尾空格，并限制昵称长度。"""
    text = "".join(ch for ch in str(value) if ch.isprintable()).strip()
    return text[:MAX_NAME_LENGTH] or "匿名用户"


def clean_message_text(value: object) -> str:
    """保留换行和制表符，去掉其它控制字符，并去掉首尾空白。"""
    text = str(value)
    text = "".join(
        ch for ch in text if ch in {"\n", "\t"} or ch.isprintable()
    ).strip()
    return text


def get_lan_ip() -> str:
    """获取当前默认局域网出口 IP；失败时返回本机回环地址。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # UDP connect 不会真正发包，只用于让系统选择默认出口网卡。
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def online_snapshot() -> tuple[list[dict], int]:
    """返回按 IP 聚合的在线用户列表，以及 IP 去重后的在线人数。"""
    groups: list[dict] = []

    for ip, connections in IP_CLIENTS.items():
        names: list[str] = []
        active_connections = 0

        for websocket in connections:
            state = CLIENTS.get(websocket)
            if state is None or not state.joined:
                continue
            active_connections += 1
            if state.name not in names:
                names.append(state.name)

        if active_connections:
            groups.append(
                {
                    "ip": ip,
                    "names": names,
                    "connections": active_connections,
                }
            )

    groups.sort(
        key=lambda item: (
            item["names"][0].casefold() if item["names"] else "",
            item["ip"],
        )
    )
    return groups, len(groups)


def users_event() -> dict:
    groups, online_count = online_snapshot()
    return {
        "type": "users",
        "users": groups,
        "onlineCount": online_count,
    }


async def send_json(websocket: ServerConnection, payload: dict) -> None:
    await websocket.send(json.dumps(payload, ensure_ascii=False))


async def broadcast(payload: dict, *, exclude: ServerConnection | None = None) -> None:
    """向所有已加入的连接发送消息；单个连接异常不会影响其他人。"""
    message = json.dumps(payload, ensure_ascii=False)

    async with BROADCAST_LOCK:
        targets = [
            websocket
            for websocket, state in list(CLIENTS.items())
            if state.joined and websocket is not exclude
        ]

        for websocket in targets:
            try:
                await websocket.send(message)
            except ConnectionClosed:
                # 对方已断开，交给它自己的 finally 做统一清理。
                continue
            except Exception:
                logger.exception("向客户端广播失败，连接将稍后清理")


async def remove_client(state: ClientState) -> None:
    """清理连接；只有某个 IP 的最后一个窗口离开时才广播离开事件。"""
    websocket = state.websocket
    if CLIENTS.pop(websocket, None) is None:
        return

    connections = IP_CLIENTS.get(state.ip)
    group_removed = False

    if connections is not None:
        try:
            connections.remove(websocket)
        except ValueError:
            pass
        if not connections:
            IP_CLIENTS.pop(state.ip, None)
            group_removed = True

    remaining_windows = len(IP_CLIENTS.get(state.ip, []))
    logger.info(
        "%s (%s) 断开，该 IP 剩余 %s 个窗口，在线 IP 数 %s",
        state.name,
        state.ip,
        remaining_windows,
        len(online_snapshot()[0]),
    )

    if not state.joined:
        return

    if group_removed:
        await broadcast(
            {
                "type": "system",
                "time": now(),
                "text": f"{state.name} 离开了聊天室",
            }
        )

    # 同一 IP 还有别的窗口时也刷新列表，但在线人数不会减少。
    await broadcast(users_event())


async def handle_client(websocket: ServerConnection) -> None:
    """处理一条 WebSocket 连接的完整生命周期。"""
    remote = websocket.remote_address
    ip = normalize_ip(remote[0] if remote else None)
    state: ClientState | None = None

    try:
        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)
            except (json.JSONDecodeError, TypeError):
                await send_json(
                    websocket, {"type": "error", "text": "消息必须是 JSON 格式。"}
                )
                continue

            if not isinstance(data, dict):
                await send_json(
                    websocket, {"type": "error", "text": "消息格式不正确。"}
                )
                continue

            message_type = data.get("type")

            if message_type == "join":
                if state is not None and state.joined:
                    await send_json(
                        websocket, {"type": "error", "text": "你已经加入聊天室。"}
                    )
                    continue

                name = clean_name(data.get("name", "匿名用户"))
                client_id = str(data.get("clientId", ""))[:64] or uuid.uuid4().hex
                state = ClientState(
                    websocket=websocket,
                    ip=ip,
                    name=name,
                    client_id=client_id,
                    joined=True,
                )
                CLIENTS[websocket] = state

                connections = IP_CLIENTS.setdefault(ip, [])
                is_new_ip = not connections
                connections.append(websocket)

                users, online_count = online_snapshot()
                await send_json(
                    websocket,
                    {
                        "type": "init",
                        "selfIp": ip,
                        "name": name,
                        "history": list(HISTORY),
                        "users": users,
                        "onlineCount": online_count,
                        "serverTime": now(),
                    },
                )

                logger.info(
                    "%s (%s) 加入；该 IP 共 %s 个窗口，在线 IP 数 %s",
                    name,
                    ip,
                    len(connections),
                    online_count,
                )

                # 同一 IP 的第二个窗口不重复播报“加入”，只刷新在线列表。
                if is_new_ip:
                    await broadcast(
                        {
                            "type": "system",
                            "time": now(),
                            "text": f"{name} 加入了聊天室",
                        },
                        exclude=websocket,
                    )
                await broadcast(users_event(), exclude=websocket)
                continue

            if state is None or not state.joined:
                await send_json(
                    websocket, {"type": "error", "text": "请先加入聊天室。"}
                )
                continue

            if message_type != "message":
                await send_json(
                    websocket, {"type": "error", "text": "未知的消息类型。"}
                )
                continue

            text = clean_message_text(data.get("text", ""))

            if not text:
                await send_json(
                    websocket, {"type": "error", "text": "消息不能为空。"}
                )
                continue

            if len(text) > MAX_TEXT_LENGTH:
                await send_json(
                    websocket,
                    {
                        "type": "error",
                        "text": f"消息不能超过 {MAX_TEXT_LENGTH} 字。",
                    },
                )
                continue

            message = {
                "id": uuid.uuid4().hex,
                "time": now(),
                "name": state.name,
                "text": text,
                "clientId": state.client_id,
            }
            HISTORY.append(message)

            logger.info("%s (%s): %s", state.name, state.ip, text[:80])
            await broadcast({"type": "message", "message": message})

    except ConnectionClosed:
        pass
    except Exception:
        # 任一客户端的数据异常都不能让整个聊天室服务崩溃。
        logger.exception("客户端 %s 的处理协程异常", ip)
    finally:
        if state is not None:
            try:
                await remove_client(state)
            except Exception:
                logger.exception("清理客户端 %s 时异常", ip)


def http_response(
    websocket: ServerConnection,
    status: int,
    text: str,
    content_type: str = "text/plain; charset=utf-8",
):
    """构造普通 HTTP 响应。"""
    response = websocket.respond(status, text)
    # Headers 的赋值是追加语义，先删除 respond() 默认的 text/plain。
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def route_request(websocket: ServerConnection, request):
    """在 WebSocket 握手前截获普通 HTTP 请求，实现单端口网页服务。"""
    path = urlsplit(request.path).path
    is_websocket = request.headers.get("Upgrade", "").lower() == "websocket"

    # WebSocket 握手继续走 websockets 的正常流程。
    if is_websocket or path == "/ws":
        return None

    if request.method != "GET":
        return http_response(websocket, 405, "Method Not Allowed\n")

    if path in {"/", "/index.html"}:
        try:
            html = INDEX_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return http_response(websocket, 500, "index.html 不存在。\n")
        except OSError:
            logger.exception("读取 index.html 失败")
            return http_response(websocket, 500, "读取网页文件失败。\n")
        return http_response(websocket, 200, html, "text/html; charset=utf-8")

    if path == "/health":
        _, online_count = online_snapshot()
        payload = {
            "status": "ok",
            "onlineCount": online_count,
            "connections": len(CLIENTS),
            "historyCount": len(HISTORY),
        }
        return http_response(
            websocket,
            200,
            json.dumps(payload, ensure_ascii=False),
            "application/json; charset=utf-8",
        )

    if path == "/favicon.ico":
        return http_response(websocket, 204, "")

    return http_response(websocket, 404, "Not Found\n")


async def main() -> None:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"缺少网页文件：{INDEX_FILE}")

    lan_ip = get_lan_ip()

    async with serve(
        handle_client,
        HOST,
        PORT,
        process_request=route_request,
        max_size=MAX_FRAME_SIZE,
        ping_interval=20,
        ping_timeout=20,
    ):
        print("=" * 58, flush=True)
        print("局域网聊天室已启动", flush=True)
        print(f"本机访问：http://127.0.0.1:{PORT}", flush=True)
        print(f"局域网访问：http://{lan_ip}:{PORT}", flush=True)
        print(f"WebSocket：ws://{lan_ip}:{PORT}/ws", flush=True)
        print("按 Ctrl+C 停止服务。", flush=True)
        print("=" * 58, flush=True)
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n服务端已停止。")
    except OSError as exc:
        print(f"\n服务启动失败：{exc}", file=sys.stderr)
        print("请确认 8765 端口没有被其他程序占用。", file=sys.stderr)
        sys.exit(1)
