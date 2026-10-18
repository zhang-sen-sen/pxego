# -*- coding: utf-8 -*-
"""
PXE 网络引导服务器 —— 启动入口。

用法：
    python main.py                  # 以默认配置启动全部服务
    python main.py --http-port 8080 # 修改 HTTP 端口（避免权限问题）
    python main.py --no-dhcp        # 仅启动 TFTP+HTTP（配合已有 DHCP 使用）
    python main.py --web-only       # 仅启动 Web 管理界面（预览/管理配置）

提示：
- 在 Windows 上，DHCP(67) / TFTP(69) / HTTP(80) 端口需要管理员权限；
- 首次运行前请先执行 tools\\fetch_bootloader.py 下载引导文件；
- 把 ISO 镜像放入 isos\\ 目录（或子目录），客户端 PXE 启动即可看到引导菜单。
"""
import argparse
import os
import sys
import threading
import time
import webbrowser


def main():
    parser = argparse.ArgumentParser(description="PXE 网络引导服务器")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 ./config.json）")
    parser.add_argument("--http-port", type=int, default=None, help="HTTP 端口（默认 80）")
    parser.add_argument("--tftp-port", type=int, default=None, help="TFTP 端口（默认 69）")
    parser.add_argument("--dhcp-port", type=int, default=None, help="DHCP 端口（默认 67）")
    parser.add_argument("--no-dhcp", action="store_true", help="禁用 DHCP")
    parser.add_argument("--iso-dir", action="append", default=None,
                        help="额外 ISO 目录（可多次指定）")
    parser.add_argument("--web-only", action="store_true",
                        help="仅启动 Web 管理界面（不启动 DHCP/TFTP）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    args = parser.parse_args()

    from pxe.config import Config
    from pxe.server import ServerContext

    cfg = Config(args.config)
    if not os.path.isfile(cfg.path):
        cfg.save()
        print(f"[init] 已生成默认配置文件: {cfg.path}")

    if args.http_port:
        cfg.data["http_port"] = args.http_port
    if args.tftp_port:
        cfg.data["tftp_port"] = args.tftp_port
    if args.dhcp_port:
        cfg.data["dhcp_port"] = args.dhcp_port
    if args.no_dhcp:
        cfg.data["dhcp_enable"] = False
    if args.iso_dir:
        for d in args.iso_dir:
            if d not in cfg.data["iso_dirs"]:
                cfg.data["iso_dirs"].append(d)
    if args.web_only:
        cfg.data["dhcp_enable"] = False
        cfg.data["tftp_port"] = 0

    os.makedirs(cfg.boot_dir, exist_ok=True)
    for d in cfg.iso_dirs:
        os.makedirs(d, exist_ok=True)

    ctx = ServerContext(cfg, quiet=args.quiet)
    ctx.start()

    print()
    print("=" * 60)
    print(" PXE 网络引导服务器已启动")
    print(f"  管理界面   : http://{cfg.server_ip}:{cfg.http_port}/")
    print(f"  服务器 IP  : {cfg.server_ip}")
    print(f"  DHCP 模式  : {cfg.dhcp_mode if cfg.dhcp_enable else 'disabled'}")
    print(f"  TFTP 端口  : {cfg.tftp_port or 'disabled'}")
    print(f"  ISO 目录   : {', '.join(cfg.iso_dirs)}")
    print(" 客户端机器设置为 PXE 网络启动即可看到引导菜单")
    print("=" * 60)
    print()

    if not args.no_browser and ctx.httpd:
        threading.Timer(0.8, lambda: webbrowser.open(
            f"http://{cfg.server_ip}:{cfg.http_port}/")).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n正在停止...")
        ctx.stop()


if __name__ == "__main__":
    main()
