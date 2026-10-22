# -*- coding: utf-8 -*-
"""
PXE 网络引导服务器 - 配置模块
负责配置的加载/保存、IP 自动探测与默认值生成。
"""
import json
import os
import re
import socket
import subprocess
import sys
import ipaddress

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path_of(*parts):
    """返回项目根目录下的路径。"""
    return os.path.join(PROJECT_ROOT, *parts)


def local_ips():
    """返回本机所有网卡的非回环 IPv4 地址（ipconfig 解析，纯标准库）。"""
    ips = []
    try:
        out = subprocess.check_output("ipconfig", stderr=subprocess.DEVNULL)
    except OSError:
        out = b""
    if out:
        for raw in out.splitlines():
            line = raw.decode("gbk", "ignore")
            if "IPv4" not in line:
                continue
            m = re.search(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})", line)
            if m:
                ip = m.group(0)
                try:
                    a = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if a.is_loopback or ip in ("0.0.0.0", "255.255.255.255"):
                    continue
                if ip not in ips:
                    ips.append(ip)
    if not ips:
        try:
            host = socket.gethostbyname(socket.gethostname())
            if not host.startswith("127."):
                ips.append(host)
        except OSError:
            pass
    return ips


def detect_lan_ip():
    """探测默认路由上最可能的局域网 IPv4 地址。

    通过 UDP connect（不实际发包）获取本机默认出口 IP；
    失败时回退到第一个非回环 IPv4 地址，最后回退 127.0.0.1。
    """
    candidates = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            candidates.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    if not candidates:
        try:
            host = socket.gethostbyname(socket.gethostname())
            if not host.startswith("127."):
                candidates.append(host)
        except OSError:
            pass

    if not candidates:
        candidates.append("127.0.0.1")

    # 在候选集中优先选择 192.168.x.x / 10.x.x.x / 172.16-31.x.x
    for ip in candidates:
        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_private and not addr.is_loopback:
                return ip
        except ValueError:
            continue
    return candidates[0]


def _derive_pool(ip):
    """由服务器 IP 推导一个默认的 DHCP 地址池。"""
    try:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        base = int(net.network_address)
        start = str(ipaddress.ip_address(base + 100))
        end = str(ipaddress.ip_address(base + 200))
        return start, end
    except ValueError:
        return "192.168.1.100", "192.168.1.200"


def default_config():
    """生成一份带默认值的配置。"""
    ip = detect_lan_ip()
    start, end = _derive_pool(ip)
    return {
        "server_ip": ip,
        "http_port": 80,
        "tftp_port": 69,
        "dhcp_port": 67,
        "dhcp_enable": True,
        # dhcp 模式：dhcp = 本服务作为唯一 DHCP 服务器；proxy = 代理 DHCP（只下发引导参数，与现有 DHCP 共存）；off = 关闭
        "dhcp_mode": "dhcp",
        "ip_pool": {"start": start, "end": end},
        "netmask": "255.255.255.0",
        "gateway": ip,
        "dns": "",
        "domain": "",
        "lease_time": 7200,
        # 客户端 MAC 过滤：off / allow（白名单）/ deny（黑名单）
        "mac_filter": {"mode": "off", "list": []},
        "menu": {
            "title": "PXE 网络引导菜单",
            "timeout": 15,
            "default": "",
            "background": "",
        },
        "iso_dirs": ["isos"],
        "boot_dir": "boot",
        "web_dir": "web",
        "password": "",
        "log_lines": 200,
        # VNC 远程维护：PE 客户机启动后自动运行 VNC 服务端（免密 None 认证，
        # 纯内网使用不考虑安全；TightVNC 端 UseVncAuthentication=0 无密码直连）。
        # 若需要恢复密码认证，设置 1-8 位密码并同步修改 PE 端注册表配置
        "vnc_password": "",
        "vnc_port": 5900,
    }


class Config:
    """配置对象：持有默认值，可加载/保存 config.json。"""

    def __init__(self, path=None):
        self.path = path or path_of("config.json")
        self.data = default_config()
        self.load()

    def load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._merge(loaded)
            except (OSError, json.JSONDecodeError) as e:
                print(f"[config] 读取配置失败，使用默认配置: {e}")
        # 保证 server_ip 有效：仅当未配置或为回环地址时自动探测；
        # 显式配置的 IP（例如通过 GUI 切换的网卡）一律保留，不做自动纠正。
        cur = self.data.get("server_ip")
        if not cur:
            self.data["server_ip"] = detect_lan_ip()
        else:
            try:
                a = ipaddress.ip_address(cur)
                if a.is_loopback:
                    self.data["server_ip"] = detect_lan_ip()
            except ValueError:
                self.data["server_ip"] = detect_lan_ip()

    def _merge(self, loaded):
        """将外部配置递归合并进默认配置（缺失字段保留默认值）。"""
        def rec(base, patch):
            for k, v in patch.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    rec(base[k], v)
                else:
                    base[k] = v
        rec(self.data, loaded)

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    # ---- 便捷访问 ----
    @property
    def server_ip(self):
        return self.data.get("server_ip", "127.0.0.1")

    @property
    def http_port(self):
        return int(self.data.get("http_port", 80))

    @property
    def tftp_port(self):
        return int(self.data.get("tftp_port", 69))

    @property
    def dhcp_port(self):
        return int(self.data.get("dhcp_port", 67))

    @property
    def dhcp_enable(self):
        return bool(self.data.get("dhcp_enable", True))

    @property
    def dhcp_mode(self):
        return self.data.get("dhcp_mode", "dhcp")

    @property
    def iso_dirs(self):
        dirs = self.data.get("iso_dirs") or ["isos"]
        return [path_of(d) if not os.path.isabs(d) else d for d in dirs]

    @property
    def boot_dir(self):
        d = self.data.get("boot_dir", "boot")
        return path_of(d) if not os.path.isabs(d) else d

    @property
    def web_dir(self):
        d = self.data.get("web_dir", "web")
        return path_of(d) if not os.path.isabs(d) else d

    def lease_time(self):
        return int(self.data.get("lease_time", 7200))

    def mac_filter(self):
        return self.data.get("mac_filter", {"mode": "off", "list": []})

    def menu(self):
        return self.data.get("menu", {})

    def bootfile_for_arch(self, arch_code):
        """根据客户端架构（DHCP option 93）返回引导文件名。

        arch_code: 0x00 BIOS / 0x06 EFI IA32 / 0x07 EFI x64 / 0x09 EFI ARM64
        """
        boot = {
            # BIOS 走原生驱动版 ipxe.pxe（EMBED=dhcp+chain ${ipxe-script}，内置网卡驱动、
            # 不依赖 PXE ROM 的 UNDI——undionly.kpxe 依赖 UNDI，在部分虚拟/物理固件上会
            # UNDI API 失败）。启动即执行内嵌脚本：重新 dhcp 拿 proxy OFFER 的 option175 →
            # chain ipxe.cfg 直引 BOOT.WIM，不显示任何 PXE 菜单（UEFI/Legacy 均直引 PE）
            0x00: "ipxe.pxe",
            0x06: "grubia32.efi",
            # UEFI x64 使用 iPXE shim 签名版（VMware EFI 固件下 GRUB 的
            # loopback 随机读不可靠，iPXE shim 在 VMware 下稳定
            0x07: "sb-uefi-snponly-shim.efi",
            0x09: "grubaa64.efi",
        }
        return boot.get(arch_code, "grubx64.efi")

    def bootfile_default(self):
        return "sb-uefi-snponly-shim.efi"

    @property
    def vnc_password(self):
        return str(self.data.get("vnc_password", "") or "")

    @property
    def vnc_port(self):
        return int(self.data.get("vnc_port", 5900) or 5900)


if __name__ == "__main__":
    c = Config()
    print(json.dumps(c.data, ensure_ascii=False, indent=2))
