"""最基础的多人在线聊天室客户端。

运行方式：
    cd D:\chat
    uv run python client.py 用户A

不写昵称时，客户端会提示输入。进入聊天室后输入 /quit 退出。
"""

import asyncio
import json
import sys

from local_dns import install_dns_compat
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

# 处理当前电脑上 Smart App Control 导致的 localhost 解析问题。
install_dns_compat()

SERVER_URL = "ws://localhost:8765"


def get_name() -> str:
    """优先使用命令行参数作为昵称，否则在终端中询问。"""
    if len(sys.argv) > 1:
        return sys.argv[1].strip()[:20] or "匿名用户"

    try:
        name = input("请输入你的昵称：").strip()
    except (EOFError, KeyboardInterrupt):
        return "匿名用户"
    return name[:20] or "匿名用户"


async def receive_messages(websocket, stop_event: asyncio.Event) -> None:
    """持续接收服务端广播，并打印到终端。"""
    try:
        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)
            except json.JSONDecodeError:
                print(f"\n[收到无法解析的消息] {raw_message}")
                continue

            if not isinstance(data, dict):
                continue

            message_type = data.get("type")
            if message_type == "message":
                print(
                    f"\n[{data.get('time', '')}] "
                    f"{data.get('name', '?')}: {data.get('text', '')}"
                )
            elif message_type == "system":
                print(f"\n[系统] {data.get('text', '')}")
            elif message_type == "error":
                print(f"\n[错误] {data.get('text', '')}")
    except ConnectionClosed:
        print("\n[系统] 与服务器的连接已断开。")
    else:
        print("\n[系统] 服务器已关闭连接。")
    finally:
        # 通知发送端不要再等待输入，便于服务器断开后正常退出。
        stop_event.set()


async def send_messages(websocket, stop_event: asyncio.Event) -> None:
    """从终端读取输入并发送。

    input() 是阻塞函数，交给 asyncio.to_thread 在线程中执行，
    这样事件循环仍能同时接收并显示其他人的消息。
    """
    while not stop_event.is_set():
        try:
            text = await asyncio.to_thread(input, "我: ")
        except (EOFError, KeyboardInterrupt):
            return

        if stop_event.is_set():
            return

        text = text.strip()
        if not text:
            continue

        if text.lower() in {"/quit", "/exit", "quit", "exit"}:
            return

        try:
            await websocket.send(
                json.dumps(
                    {"type": "message", "text": text},
                    ensure_ascii=False,
                )
            )
        except ConnectionClosed:
            return


async def main() -> None:
    name = get_name()

    try:
        # async with 退出时会自动关闭 WebSocket 连接。
        async with connect(SERVER_URL) as websocket:
            # 连接成功后先登记昵称，后续消息就能显示发送者名字。
            await websocket.send(
                json.dumps({"type": "join", "name": name}, ensure_ascii=False)
            )

            print(f"已连接到 {SERVER_URL}")
            print(f"当前昵称：{name}")
            print("输入内容后按回车发送；输入 /quit 退出。")

            # 两个 asyncio 任务并发运行：一边等键盘输入，一边接收广播。
            stop_event = asyncio.Event()
            receiver = asyncio.create_task(
                receive_messages(websocket, stop_event)
            )
            sender = asyncio.create_task(send_messages(websocket, stop_event))

            # 任意一边结束就处理另一边：用户主动退出时取消接收；
            # 服务器先断开时则等待用户按一次回车，让 input() 所在线程自然结束。
            done, pending = await asyncio.wait(
                {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
            )

            if sender in done:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                finished_tasks = done
            else:
                print("\n[系统] 连接已关闭，按回车退出。")
                await asyncio.gather(sender, return_exceptions=True)
                finished_tasks = done | {sender}

            # 显示任务中没有被内部捕获的异常。
            for task in finished_tasks:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    print(f"\n[错误] {error}")

    except OSError as exc:
        print("[错误] 无法连接到服务器。")
        print("       请先在另一个终端运行：uv run python server.py")
        print(f"       详细信息：{exc}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出聊天室。")
