# -*- coding: utf-8 -*-
# PXE FTP 服务器 - aioftp 匿名共享 isos 目录（TOOL.ZIP / PE ISO）
# 用法: python ftp_server.py [端口]
import asyncio
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PY_DIR = os.path.join(BASE, "runtime", "python")
if os.path.isdir(PY_DIR) and PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

import aioftp

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 21
ISO_DIR = os.path.join(BASE, "isos")
if not os.path.isdir(ISO_DIR):
    print("isos 目录不存在: %s" % ISO_DIR, flush=True)
    sys.exit(1)


def _pasv_address():
    """从 config.json 读取 server_ip 作为 FTP 被动模式回应地址；无配置时自动。"""
    try:
        import json
        with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
            ip = json.load(f).get("server_ip")
        if ip:
            return ip
    except Exception:
        pass
    return None


async def main():
    user = aioftp.User(
        login=None,
        password=None,
        base_path=ISO_DIR,
        home_path="/",
        permissions=[aioftp.Permission(path="/", readable=True, writable=False)],
    )
    manager = aioftp.MemoryUserManager([user])
    # 优化：固定被动端口范围 + 加大超时（大文件传输稳定）+ 强制 PASV 地址
    server = aioftp.Server(
        manager,
        socket_timeout=600,
        idle_timeout=600,
        data_ports=range(50000, 50101),
        ipv4_pasv_forced_response_address=_pasv_address(),
    )
    await server.start("0.0.0.0", PORT)
    print("FTP 服务器已启动: 0.0.0.0:%d  根目录: %s  被动端口: 50000-50100" % (PORT, ISO_DIR), flush=True)
    await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
