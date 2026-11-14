"""当前 Windows 环境下的 localhost 解析兼容处理。

本机开启了 Smart App Control，uv 自带 Python 的 unicodedata.pyd 被系统拦截。
Windows 的 socket.getaddrinfo() 对字符串主机名会触发 IDNA 编码，而 IDNA 依赖
unicodedata，因此连接 localhost 时会报 LookupError: unknown encoding: idna。

把纯 ASCII 主机名以 bytes 形式传给系统解析器，可以跳过 IDNA。聊天室只访问
本机地址，这个处理不影响功能；在正常 Python 环境中不会启用。
"""

import socket
import sys


def install_dns_compat() -> None:
    """仅在 IDNA 不可用时安装兼容的 getaddrinfo。"""
    if sys.platform != "win32":
        return

    try:
        "localhost".encode("idna")
    except (ImportError, LookupError):
        # IDNA 不可用，需要下面的兼容处理。
        pass
    else:
        # 正常环境什么也不做。
        return

    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if isinstance(host, str) and host.isascii():
            host = host.encode("ascii")
        return original_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo
