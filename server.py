"""局域网多房间网页聊天室：HTTP 页面 + WebSocket 实时消息，共用同一端口。

运行方式：
    cd D:\chat
    uv run python server.py

功能：
- 房间1 / 房间2 完全隔离在线列表、历史记录和消息广播。
- 每条消息携带发送者 IP 和唯一 msg_id。
- 同 IP 只能在 2 分钟内撤回自己发送、且仍在本房间历史中的消息。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from local_dns import install_dns_compat
from storage import ChatDatabase
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

# 处理当前电脑上 Smart App Control 导致的本地地址解析问题。
install_dns_compat()

HOST = "0.0.0.0"
PORT = 8765
MAX_NAME_LENGTH = 16
MAX_TEXT_LENGTH = 200
HISTORY_SIZE = 50
RECALL_WINDOW_MS = 2 * 60 * 1000
MAX_FRAME_SIZE = 64 * 1024

DEFAULT_ROOM = "room1"
ROOM_NAMES = {
    "room1": "房间1",
    "room2": "房间2",
}

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
DB_PATH = BASE_DIR / "chat.db"
DB: ChatDatabase | None = None

STATIC_FILES = {
    "/manifest.json": (
        BASE_DIR / "manifest.json",
        "application/manifest+json; charset=utf-8",
    ),
    "/service-worker.js": (
        BASE_DIR / "service-worker.js",
        "application/javascript; charset=utf-8",
    ),
    "/icons/icon-192.png": (BASE_DIR / "icons" / "icon-192.png", "image/png"),
    "/icons/icon-512.png": (BASE_DIR / "icons" / "icon-512.png", "image/png"),
    "/icons/maskable-512.png": (
        BASE_DIR / "icons" / "maskable-512.png",
        "image/png",
    ),
    "/icons/apple-touch-icon.png": (
        BASE_DIR / "icons" / "apple-touch-icon.png",
        "image/png",
    ),
}

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
    room: str
    name: str
    joined: bool = False


# 所有在线连接；每个连接通过 state.room 归属一个房间。
CLIENTS: dict[ServerConnection, ClientState] = {}

# 按房间隔离的 {IP: [连接列表]}。
IP_CLIENTS: dict[str, dict[str, list[ServerConnection]]] = {
    room: {} for room in ROOM_NAMES
}

# 串行广播，避免多人同时发言时消息交叉写入。
BROADCAST_LOCK = asyncio.Lock()


def database() -> ChatDatabase:
    if DB is None:
        raise RuntimeError("数据库尚未初始化")
    return DB


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


def room_from_websocket(websocket: ServerConnection) -> str | None:
    """从 WebSocket 握手路径的 ?room= 参数读取并校验房间。"""
    request = getattr(websocket, "request", None)
    if request is None:
        return DEFAULT_ROOM

    query = parse_qs(urlsplit(request.path).query)
    room = (query.get("room") or [DEFAULT_ROOM])[0].strip()
    if not room:
        return DEFAULT_ROOM
    return room if room in ROOM_NAMES else None


def get_lan_ip() -> str:
    """获取当前默认局域网出口 IP；失败时返回本机回环地址。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def online_snapshot(room: str) -> tuple[list[dict], int]:
    """返回指定房间按 IP 聚合的在线用户，以及房间内去重人数。"""
    groups: list[dict] = []

    for ip, connections in IP_CLIENTS[room].items():
        names: list[str] = []
        active_connections = 0

        for websocket in connections:
            state = CLIENTS.get(websocket)
            if state is None or not state.joined or state.room != room:
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


def users_event(room: str) -> dict:
    groups, online_count = online_snapshot(room)
    return {
        "type": "users",
        "room": room,
        "users": groups,
        "onlineCount": online_count,
    }


async def send_json(websocket: ServerConnection, payload: dict) -> None:
    await websocket.send(json.dumps(payload, ensure_ascii=False))


async def broadcast(
    room: str,
    payload: dict,
    *,
    exclude: ServerConnection | None = None,
) -> None:
    """只向指定房间的已加入连接发送消息。"""
    message = json.dumps(payload, ensure_ascii=False)

    async with BROADCAST_LOCK:
        targets = [
            websocket
            for websocket, state in list(CLIENTS.items())
            if state.joined and state.room == room and websocket is not exclude
        ]

        for websocket in targets:
            try:
                await websocket.send(message)
            except ConnectionClosed:
                # 对方已断开，交给它自己的 finally 做统一清理。
                continue
            except Exception:
                logger.exception("向 %s 客户端广播失败", room)


async def remove_client(state: ClientState) -> None:
    """清理连接；只在该房间内此 IP 的最后一个窗口离开时广播离开。"""
    websocket = state.websocket
    if CLIENTS.pop(websocket, None) is None:
        return

    room = state.room
    connections = IP_CLIENTS[room].get(state.ip)
    group_removed = False

    if connections is not None:
        try:
            connections.remove(websocket)
        except ValueError:
            pass
        if not connections:
            IP_CLIENTS[room].pop(state.ip, None)
            group_removed = True

    remaining_windows = len(IP_CLIENTS[room].get(state.ip, []))
    logger.info(
        "[%s] %s (%s) 断开，该 IP 剩余 %s 个窗口，房间在线 IP 数 %s",
        ROOM_NAMES[room],
        state.name,
        state.ip,
        remaining_windows,
        online_snapshot(room)[1],
    )

    if not state.joined:
        return

    if group_removed:
        await broadcast(
            room,
            {
                "type": "system",
                "time": now(),
                "text": f"{state.name} 离开了聊天室",
            },
        )

    await broadcast(room, users_event(room))


async def handle_client(websocket: ServerConnection) -> None:
    """处理一条 WebSocket 连接的完整生命周期。"""
    remote = websocket.remote_address
    ip = normalize_ip(remote[0] if remote else None)
    room = room_from_websocket(websocket)
    state: ClientState | None = None

    if room is None:
        try:
            await send_json(
                websocket,
                {
                    "type": "error",
                    "text": "房间不存在，请选择房间1或房间2。",
                },
            )
            await websocket.close(code=1008, reason="invalid room")
        except ConnectionClosed:
            pass
        return

    room_name = ROOM_NAMES[room]

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
                state = ClientState(
                    websocket=websocket,
                    ip=ip,
                    room=room,
                    name=name,
                    joined=True,
                )
                CLIENTS[websocket] = state

                connections = IP_CLIENTS[room].setdefault(ip, [])
                is_new_ip = not connections
                connections.append(websocket)

                raw_last_id = data.get("last_msg_id", "")
                last_msg_id = (
                    raw_last_id.strip()[:64]
                    if isinstance(raw_last_id, str)
                    else None
                )
                history = database().get_history(
                    room,
                    last_msg_id or None,
                    recent_limit=HISTORY_SIZE,
                )

                users, online_count = online_snapshot(room)
                await send_json(
                    websocket,
                    {
                        "type": "init",
                        "room": room,
                        "roomName": room_name,
                        "selfIp": ip,
                        "name": name,
                        "history": history,
                        "users": users,
                        "onlineCount": online_count,
                        "recallWindowMs": RECALL_WINDOW_MS,
                        "serverTime": now(),
                    },
                )

                logger.info(
                    "[%s] %s (%s) 加入，补发后历史 %s 条；该 IP 共 %s 个窗口，房间在线 IP 数 %s",
                    room_name,
                    name,
                    ip,
                    len(history),
                    len(connections),
                    online_count,
                )

                # 同一 IP 的第二个窗口不重复播报加入，只刷新列表。
                if is_new_ip:
                    await broadcast(
                        room,
                        {
                            "type": "system",
                            "time": now(),
                            "text": f"{name} 加入了聊天室",
                        },
                        exclude=websocket,
                    )
                await broadcast(room, users_event(room), exclude=websocket)
                continue

            if state is None or not state.joined:
                await send_json(
                    websocket, {"type": "error", "text": "请先加入聊天室。"}
                )
                continue

            if message_type == "message":
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

                message = database().add_message(
                    msg_id=uuid.uuid4().hex,
                    room=room,
                    name=state.name,
                    ip=state.ip,
                    text=text,
                    created_at=int(time.time() * 1000),
                    time_text=now(),
                )

                logger.info("[%s] %s (%s): %s", room_name, state.name, ip, text[:80])
                await broadcast(room, {"type": "message", "message": message})
                continue

            if message_type == "edit":
                msg_id = str(data.get("msg_id", "")).strip()
                if not msg_id:
                    await send_json(
                        websocket, {"type": "error", "text": "缺少要编辑的消息 ID。"}
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

                target = database().get_message(room, msg_id)
                if target is None or target.get("recalled"):
                    await send_json(
                        websocket,
                        {"type": "error", "text": "消息不存在或已被撤回。"},
                    )
                    continue
                if target.get("ip") != state.ip:
                    await send_json(
                        websocket,
                        {"type": "error", "text": "只能编辑自己发送的消息。"},
                    )
                    continue

                edited_at = int(time.time() * 1000)
                if not database().edit_message(
                    room, msg_id, text, edited_at
                ):
                    await send_json(
                        websocket,
                        {"type": "error", "text": "消息不存在或已被撤回。"},
                    )
                    continue

                logger.info(
                    "[%s] %s (%s) 编辑消息 %s",
                    room_name,
                    state.name,
                    ip,
                    msg_id,
                )
                await broadcast(
                    room,
                    {
                        "type": "edited",
                        "room": room,
                        "msg_id": msg_id,
                        "text": text,
                        "name": target.get("name", state.name),
                        "time": now(),
                    },
                )
                continue

            if message_type == "recall":
                msg_id = str(data.get("msg_id", "")).strip()
                if not msg_id:
                    await send_json(
                        websocket, {"type": "error", "text": "缺少要撤回的消息 ID。"}
                    )
                    continue

                target = database().get_message(room, msg_id)

                if target is None or target.get("recalled"):
                    await send_json(
                        websocket,
                        {"type": "error", "text": "消息不存在或已被撤回。"},
                    )
                    continue

                if target.get("ip") != state.ip:
                    await send_json(
                        websocket,
                        {"type": "error", "text": "只能撤回自己发送的消息。"},
                    )
                    continue

                age_ms = int(time.time() * 1000) - int(
                    target.get("created_at", 0)
                )
                if age_ms > RECALL_WINDOW_MS:
                    await send_json(
                        websocket,
                        {"type": "error", "text": "已超过 2 分钟，无法撤回。"},
                    )
                    continue

                if not database().recall_message(
                    room, msg_id, int(time.time() * 1000)
                ):
                    await send_json(
                        websocket,
                        {"type": "error", "text": "消息不存在或已被撤回。"},
                    )
                    continue

                logger.info(
                    "[%s] %s (%s) 撤回消息 %s",
                    room_name,
                    state.name,
                    ip,
                    msg_id,
                )
                await broadcast(
                    room,
                    {
                        "type": "recall",
                        "room": room,
                        "msg_id": msg_id,
                        "name": target.get("name", state.name),
                        "ip": target.get("ip", state.ip),
                        "time": now(),
                    },
                )
                continue

            await send_json(
                websocket, {"type": "error", "text": "未知的消息类型。"}
            )

    except ConnectionClosed:
        pass
    except Exception:
        # 任一客户端的数据异常都不能让整个聊天室服务崩溃。
        logger.exception("[%s] 客户端 %s 的处理协程异常", room_name, ip)
    finally:
        if state is not None:
            try:
                await remove_client(state)
            except Exception:
                logger.exception("[%s] 清理客户端 %s 时异常", room_name, ip)


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


def binary_response(
    websocket: ServerConnection,
    body: bytes,
    content_type: str,
    cache_control: str = "no-store",
):
    """构造带二进制正文的普通 HTTP 响应。"""
    response = websocket.respond(200, "")
    del response.headers["Content-Type"]
    del response.headers["Content-Length"]
    response.headers["Content-Type"] = content_type
    response.headers["Content-Length"] = str(len(body))
    response.headers["Cache-Control"] = cache_control
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.body = body
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

    if path in STATIC_FILES:
        file_path, content_type = STATIC_FILES[path]
        try:
            data = file_path.read_bytes()
        except FileNotFoundError:
            return http_response(websocket, 500, f"缺少资源：{file_path.name}\n")
        except OSError:
            logger.exception("读取静态资源 %s 失败", file_path)
            return http_response(websocket, 500, "读取静态资源失败。\n")
        return binary_response(websocket, data, content_type)

    if path == "/health":
        rooms_info: dict[str, dict] = {}
        total_online = 0
        total_history = 0

        for room_id, room_name in ROOM_NAMES.items():
            groups, online_count = online_snapshot(room_id)
            connections = sum(group["connections"] for group in groups)
            history_count = database().visible_count(room_id)
            total_online += online_count
            total_history += history_count
            rooms_info[room_id] = {
                "name": room_name,
                "onlineCount": online_count,
                "connections": connections,
                "historyCount": history_count,
            }

        payload = {
            "status": "ok",
            "onlineCount": total_online,
            "connections": len(CLIENTS),
            "historyCount": total_history,
            "rooms": rooms_info,
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
    global DB

    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"缺少网页文件：{INDEX_FILE}")

    DB = ChatDatabase(DB_PATH)
    lan_ip = get_lan_ip()

    try:
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
            print("局域网多房间聊天室已启动", flush=True)
            print(f"SQLite 数据库：{DB_PATH}", flush=True)
            print(f"本机访问：http://127.0.0.1:{PORT}", flush=True)
            print(f"局域网访问：http://{lan_ip}:{PORT}", flush=True)
            print(f"WebSocket：ws://{lan_ip}:{PORT}/ws?room=room1", flush=True)
            print("房间：房间1 / 房间2", flush=True)
            print("按 Ctrl+C 停止服务。", flush=True)
            print("=" * 58, flush=True)
            await asyncio.get_running_loop().create_future()
    finally:
        DB.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n服务端已停止。")
    except OSError as exc:
        print(f"\n服务启动失败：{exc}", file=sys.stderr)
        print("请确认 8765 端口没有被其他程序占用。", file=sys.stderr)
        sys.exit(1)
