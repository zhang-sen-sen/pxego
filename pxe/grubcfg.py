# -*- coding: utf-8 -*-
"""
引导菜单生成器：
- gen_grub_cfg()     生成 GRUB2 菜单（UEFI + BIOS 双分支，支持 Linux / Windows / memdisk）
- gen_pxelinux_cfg() 生成 PXELINUX 菜单（BIOS 回退路径：memdisk 引导）
- gen_ipxe_script()  生成 iPXE 链式脚本（可选）
"""
import html
import os
import urllib.parse


# 菜单显示名直接采用 isos 目录中的文件名（与磁盘上的 ISO 一一对应）。
# 如需自定义显示名，可在 boot_conf.json 的 overrides 里设 "label" 字段（优先级最高）。
_IPXE_LABEL_MAP = {}


def _ipxe_label(name):
    return _IPXE_LABEL_MAP.get(name, name)


def _esc_grub(s):
    """GRUB 配置内转义（菜单标题/路径）。"""
    return s.replace("\\", "/").replace(" ", "\\ ")


def _http_base(server, port):
    """HTTP 根：http://IP 或 http://IP:port（端口 80 时省略）。"""
    port = int(port or 80)
    return f"http://{server}:{port}" if port != 80 else f"http://{server}"


def _grub_http_host(server, port):
    """GRUB 设备中的主机串：(http,host) 的 host 部分，端口 80 时省略。"""
    port = int(port or 80)
    return f"{server}:{port}" if port != 80 else server


def _grub_linux_entry(iso, pxe_server, http_port):
    """Linux 内核条目：运行时按 grub_platform 分支（efi 用 linux/initrd，pc 用 linux16/initrd16）。"""
    conf = iso["conf"]
    kernel = _esc_grub(conf.get("kernel", ""))
    initrd = _esc_grub(conf.get("initrd", ""))
    iso_url = _http_base(pxe_server, http_port) + iso["url"]
    args = conf.get("args", "").replace("{iso_url}", iso_url)
    return (
        f"\tif [ \"$grub_platform\" = \"efi\" ]; then\n"
        f"\t\tlinux (loop)/{kernel} {args}\n"
        f"\t\tinitrd (loop)/{initrd}\n"
        f"\telse\n"
        f"\t\tlinux16 (loop)/{kernel} {args}\n"
        f"\t\tinitrd16 (loop)/{initrd}\n"
        f"\tfi\n"
    )


def _grub_windows_entry(iso, pxe_server, http_port):
    """Windows 条目。

    - UEFI：GRUB loopback 挂载 ISO + chainloader bootmgfw.efi（
      bootmgfw 从 ISO 读 BCD/WIM；GRUB EFI 的 initrd --name 与 iPXE 在
      VMware 固件下均不可用，此路径是 UEFI 引导 Windows PE 的标准做法）；
    - BIOS：memdisk 整盘映射引导（与 pxelinux 路径一致）。
    """
    http_host = _grub_http_host(pxe_server, http_port)
    conf = iso["conf"]
    efi_boot = conf.get("wim_efi_boot", "efi/boot/bootx64.efi")
    # loopback 经 HTTP 挂载：服务器已对所有文件响应加 Connection: close
    # （GRUB 的 http 读依赖连接关闭判断 EOF，keep-alive 会导致卡死）。
    wb = (
        f"\t\techo 'Mounting ISO (loopback, HTTP)...'\n"
        f"\t\tloopback loop (http,{http_host}){iso['url']}\n"
        f"\t\techo 'Loading bootmgfw.efi from ISO...'\n"
        f"\t\tchainloader (loop)/{efi_boot}\n"
        f"\t\tboot\n"
    )
    return (
        f"\tif [ \"$grub_platform\" = \"efi\" ]; then\n"
        f"{wb}"
        f"\telse\n"
        f"\t\tlinux16 (http,{http_host})/boot/memdisk\n"
        f"\t\tinitrd16 (http,{http_host}){iso['url']}\n"
        f"\tfi\n"
    )


def _grub_memdisk_entry(iso, pxe_server, http_port):
    http_host = _grub_http_host(pxe_server, http_port)
    return (
        f"\tif [ \"$grub_platform\" != \"efi\" ]; then\n"
        f"\t\tlinux16 (http,{http_host})/boot/memdisk\n"
        f"\t\tinitrd16 (http,{http_host}){iso['url']}\n"
        f"\tfi\n"
    )


def gen_grub_cfg(config, entries):
    """生成 grub.cfg。entries: IsoRegistry.entries() 结果。"""
    server = config.server_ip
    http_port = config.http_port
    http_host = _grub_http_host(server, http_port)
    http_base = _http_base(server, http_port)
    menu = config.menu()
    title = menu.get("title", "PXE Boot Menu")
    title = _ipxe_label(title)
    timeout = max(0, int(menu.get("timeout", 15)))
    password = config.data.get("password", "")

    lines = [
        "# PXE 网络引导菜单 - 由服务器自动生成，请勿手工编辑",
        f"set pxe_server={server}",
        f"set timeout={timeout}",
        "set default=0",
        "insmod all_video",
        "insmod net",
        'if [ "$grub_platform" = "efi" ]; then',
        "\tinsmod efinet",
        "\tinsmod efi_net",
        "else",
        "\tinsmod linux16",
        "fi",
        # GRUB 自身的网络栈需显式配置：TFTP 加载 grub.cfg 走的是固件
        # PXE 栈，grubnet 的 http/loopback 设备依赖 GRUB 自己的接口配置，
        # 因此主动发起 DHCP 使 (http,...) 设备可用。
        "insmod dhcp",
        "dhcp",
        "insmod http",
        "insmod loopback",
        "insmod iso9660",
        "insmod udf",
        "insmod linux",
        "insmod chain",
        "insmod reboot",
        "set color_normal=white/black",
        "set color_highlight=white/blue",
        "",
    ]
    if password:
        lines.append('set superusers="admin"')
        lines.append(f'password_pbkdf2 admin {password}')
        lines.append("export superusers")
        lines.append("")

    # 按目录分组（目录化菜单）
    groups = {}
    plain = []
    for e in entries:
        if e.get("group"):
            groups.setdefault(e["group"], []).append(e)
        else:
            plain.append(e)

    def emit_entries(es, indent=""):
        for iso in es:
            conf = iso["conf"]
            os_type = conf.get("os_type", "unknown")
            label = conf.get("label") or iso["name"]
            locked = ' --unrestricted' if password else ''
            lines.append(f'{indent}menuentry "{label}"{locked} {{')
            if os_type in ("ubuntu", "debian_live", "redhat", "debian_installer", "generic_linux"):
                lines.append(f'{indent}\tloopback loop (http,{http_host}){iso["url"]}')
            if os_type == "windows":
                lines.append(_grub_windows_entry(iso, server, http_port).rstrip("\n"))
            elif os_type == "windows_pe":
                # 固定 PE（boot 目录 BOOT.WIM）：无 ISO 可 loopback，链式转 iPXE wimboot
                lines.append(f'{indent}\techo \'Loading PE via iPXE (wimboot)...\'')
                lines.append(f'{indent}\tchainloader (http,{http_host})/boot/ipxe.efi')
                lines.append(f'{indent}\tboot')
            elif os_type in ("ubuntu", "debian_live", "redhat", "debian_installer", "generic_linux"):
                lines.append(_grub_linux_entry(iso, server, http_port).rstrip("\n"))
            else:
                lines.append(_grub_memdisk_entry(iso, server, http_port).rstrip("\n"))
            lines.append(f'{indent}}}')
            lines.append("")

    for g in sorted(groups):
        lines.append(f'submenu "{g}" {{')
        emit_entries(groups[g], "    ")
        lines.append("}")
        lines.append("")
    emit_entries(plain)

    # 工具菜单
    lines.append('menuentry "重新启动 (Reboot)" --class reboot {')
    lines.append("\treboot")
    lines.append("}")
    lines.append('menuentry "退出到 GRUB 命令行" {')
    lines.append("\texit")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def gen_pxelinux_cfg(config, entries):
    """生成 pxelinux.cfg/default（BIOS 回退菜单，memdisk 引导整盘 ISO）。"""
    server = config.server_ip
    http_base = _http_base(server, config.http_port)
    menu = config.menu()
    title = menu.get("title", "PXE Boot Menu")
    title = _ipxe_label(title)
    timeout = max(0, int(menu.get("timeout", 15)))
    lines = [
        "DEFAULT menu.c32",
        "PROMPT 0",
        f"TIMEOUT {timeout * 10}",
        "ONTIMEOUT local",
        f"MENU TITLE {title}",
        "",
    ]
    for iso in entries:
        conf = iso["conf"]
        os_type = conf.get("os_type", "unknown")
        if os_type == "windows_pe":
            # BIOS 下固定 PE 无 ISO（memdisk 整盘映射不可用），跳过；
            # 请走 UEFI（iPXE wimboot）路径。
            continue
        label = conf.get("label") or iso["name"]
        lines.append(f"LABEL {html.escape(iso['name'], quote=True)}")
        lines.append(f"  MENU LABEL {label}")
        lines.append("  KERNEL memdisk")
        lines.append(f"  INITRD {http_base}{iso['url']}")
        lines.append("  APPEND iso")
        lines.append("")
    lines.append("LABEL local")
    lines.append("  MENU LABEL Boot from Local Disk")
    lines.append("  LOCALBOOT 0")
    lines.append("")
    return "\n".join(lines)


def gen_ipxe_script(config, entries):
    """生成 iPXE 链式脚本：不显示选择菜单，直接自动引导第一个条目（默认 PE）。

    客户端链路：UEFI → iPXE shim → 本脚本 → wimboot 引导（无需人工选择）。
    """
    server = config.server_ip
    http_port = config.http_port
    http_srv = f"http://${{server}}" + (f":{http_port}" if int(http_port) != 80 else "")
    wins = [e for e in entries if e["conf"].get("os_type") in ("windows", "windows_pe")]
    lines = ["#!ipxe",
             f"set server {server}",
             ]
    # 无选择菜单：直接引导第一个条目（固定 PE 维护系统在前）
    if wins:
        lines.append("goto w1")
    else:
        lines.append("goto local")
    for i, e in enumerate(wins, 1):
        conf = e["conf"]
        if conf.get("os_type") == "windows_pe":
            # 固定 PE 条目：wimboot 4 件套直接来自 boot 目录（无需 ISO）
            pe_wim = conf.get("wim", "boot/BOOT.WIM")
            pe_efi = conf.get("wim_efi_boot", "boot/bootmgfw.efi")
            pe_bcd = conf.get("wim_bcd", "boot/BCD")
            pe_sdi = conf.get("wim_sdi", "boot/boot.sdi")
            lines += [
                f":w{i}",
                f"kernel --timeout 30 {http_srv}/boot/wimboot || goto reboot",
                f"initrd --name bootmgfw.efi --timeout 60 {http_srv}/{pe_efi} || goto reboot",
                f"initrd --name BCD --timeout 60 {http_srv}/{pe_bcd} || goto reboot",
                f"initrd --name boot.sdi --timeout 60 {http_srv}/{pe_sdi} || goto reboot",
                f"initrd --name boot.wim --timeout 300 {http_srv}/{pe_wim} || goto reboot",
                "boot || goto reboot",
                ":reboot",
                "echo ==== PXE boot failed or timed out, reporting then rebooting ====",
                # 回报用 kernel --timeout 10 限制最多等待 10 秒（imgfetch 无超时参数，
                # 服务器不可达时会等很久才重启）
                f"kernel --timeout 10 {http_srv}/api/pxe-fail?reason=boot-failed || echo [report-fail-ignored]",
                "reboot",
            ]
            continue
        iso_rel = e["url"][len("/iso/"):]           # 相对 iso_dirs 的路径
        iso_q = urllib.parse.quote(iso_rel, safe="/")
        # 无查询参数路径：/wim/<iso相对路径>/<ISO内路径>（GRUB/iPXE 都支持，
        # 避免 & 查询参数被引导器解析问题）
        wim_url = (f"{http_srv}/wim/{iso_q}/"
                   f"{urllib.parse.quote(conf.get('wim', 'sources/boot.wim'), safe='/')}")
        efi_url = (f"{http_srv}/wim/{iso_q}/"
                   f"{urllib.parse.quote(conf.get('wim_efi_boot', 'efi/microsoft/boot/bootmgfw.efi'), safe='/')}")
        bcd_url = (f"{http_srv}/wim/{iso_q}/"
                   f"{urllib.parse.quote(conf.get('wim_bcd', 'boot/bcd'), safe='/')}")
        sdi_url = (f"{http_srv}/wim/{iso_q}/"
                   f"{urllib.parse.quote(conf.get('wim_sdi', 'boot/boot.sdi'), safe='/')}")
        # wimboot 内存盘只有固定子目录（\BOOT\ \SOURCES\ \EFI\），所有
        # initrd 文件都落在根目录；服务端已把 BCD 内 .wim/.sdi 路径修补为
        # 根路径（\boot.wim / \boot.sdi，NUL 等长填充），因此 --name 必须
        # 用根文件名，bootmgfw 才能按 BCD 路径在内存盘命中。
        # 每个文件下载带超时（iPXE --timeout 单位秒）：下载卡住/失败即自动重启
        # （VM 重新 PXE 引导重试，避免停在 9% 这类无进展状态）
        lines += [
            f":w{i}",
            f"kernel --timeout 30 {http_srv}/boot/wimboot || goto reboot",
            f"initrd --name bootmgfw.efi --timeout 60 {efi_url} || goto reboot",
            f"initrd --name BCD --timeout 60 {bcd_url} || goto reboot",
            f"initrd --name boot.sdi --timeout 60 {sdi_url} || goto reboot",
            f"initrd --name boot.wim --timeout 180 {wim_url} || goto reboot",
            "boot || goto reboot",
            ":reboot",
            "echo ==== PXE boot failed or timed out, rebooting ====",
            "sleep 5",
            "reboot",
        ]
    lines += [":local", "exit"]
    return "\n".join(lines)


if __name__ == "__main__":
    from .config import Config
    from .bootconf import IsoRegistry
    cfg = Config()
    reg = IsoRegistry(cfg)
    es = reg.entries(force=True)
    print(gen_grub_cfg(cfg, es)[:4000])
