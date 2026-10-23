# -*- coding: utf-8 -*-
"""
服务协调层：组装 DHCP / TFTP / HTTP 三个服务，提供统一状态、日志与引导文件生成。
"""
import collections
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone

# 项目按中国内网使用，日志时间固定 UTC+8，避免容器系统时区（UTC）导致差 8 小时
_CN_TZ = timezone(timedelta(hours=8))


def _cn_now():
    """返回 UTC+8 的当前时间（datetime，含微秒）。"""
    return datetime.now(_CN_TZ)


def _contains_ip(low, ip):
    """日志行小写文本中是否“恰好”出现该 IPv4（后面不是字母/数字）。

    覆盖 [IP]、至 ('IP', port)、来自 IP: 等全部格式；
    ip 后跟数字/字母则视为更长的地址前缀，不算匹配（如 192.168.10.15 vs .153）。
    """
    start = 0
    while True:
        idx = low.find(ip, start)
        if idx < 0:
            return False
        after = idx + len(ip)
        if after < len(low) and low[after].isalnum():
            start = idx + 1
            continue
        return True

from . import grubcfg
from .bootconf import BootConf, IsoRegistry
from .config import Config
from .dhcp import DhcpServer
from .httpd import PxeHttpHandler, PxeHttpServer
from .tftp import TftpServer


class ServerContext:
    """所有服务的共享上下文。"""

    def __init__(self, config=None, quiet=False):
        self.config = config or Config()
        self.bootconf = BootConf()
        self.registry = IsoRegistry(self.config, self.bootconf)
        self.start_time = time.time()
        max_lines = max(1, int(self.config.data.get("log_lines", 200)))
        self._log_buf = collections.deque(maxlen=max_lines)
        # 进度类日志（下载百分比等）：同 key 覆盖刷新，不逐条追加
        self._latest_progress = {}
        self.quiet = quiet
        # 日志文件：仅记录本次运行期间的日志；每次启动即清空旧日志
        # （等效于"退出服务后就清理"——重启后日志总是从空开始）
        log_path = self.config.data.get("log_file") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "pxe.log")
        self._log_file = log_path
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            open(log_path, "w", encoding="utf-8").close()
        except OSError:
            self._log_file = None

        self.dhcp = DhcpServer(self.config, on_client=self._on_client,
                               log=self.log, is_online=self._is_client_online)
        self.tftp = TftpServer(self.config, file_provider=self._tftp_file, log=self.log)
        self.httpd = None
        self._threads = []
        self._client_events = collections.deque(maxlen=200)
        self._client_state = {}   # norm_mac -> {mac, ip, time, arch, hostname, state, state_ts, seq}
        self._ever_online = set()  # 所有曾上线过的客户端 MAC（合计统计）
        self._ip_mac = {}         # ip -> norm_mac（HTTP 请求按源 IP 关联客户端）
        self._seq_next = 1        # 客户端序号（按 DHCP 首次登记顺序 001/002…）
        self._running = False     # 下线检测线程运行标志
        self._log_lock = threading.Lock()
        self._log_waiters = set() # SSE 日志订阅队列

    @staticmethod
    def _norm_mac(m):
        return "".join(ch for ch in str(m).lower() if ch.isalnum())

    @staticmethod
    def _fmt_mac(m):
        n = ServerContext._norm_mac(m)
        return ":".join(n[i:i + 2] for i in range(0, len(n), 2)) if n else ""

    # ---------------- 日志 ----------------
    _IP_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
    _MAC_RE = re.compile(r"客户端MAC=([0-9a-fA-F:]+)")

    def _is_client_online(self, mac):
        """按 MAC 判断客户端是否已上线（供 DHCP 日志源头过滤）。"""
        st = self._client_state.get(self._norm_mac(mac))
        return bool(st and st.get("state") == "已上线")

    def _is_online_client_log(self, msg):
        """已上线客户端的中间过程日志（DHCP/TFTP/下载/进度）是否应过滤。

        客户端已上线（state=已上线）后，仅保留上线成功/下线相关日志，
        不再显示它的过程日志，避免刷屏。
        """
        if ("已上线" in msg or "已下线" in msg or "上线通知" in msg
                or "上线成功" in msg):
            return False
        # 提取客户端标识（MAC 或 IP）
        key = None
        m = self._MAC_RE.search(msg)
        if m:
            key = self._norm_mac(m.group(1))
        if not key:
            m = re.search(r"\[(" + self._IP_RE.pattern + r")\]", msg)
            if m:
                key = self._ip_mac.get(m.group(1))
        if not key:
            m = re.search(r"(?:来自|至)\s+(" + self._IP_RE.pattern + r")", msg)
            if m:
                key = self._ip_mac.get(m.group(1))
        if not key:
            return False
        st = self._client_state.get(key)
        return bool(st and st.get("state") == "已上线")

    def subscribe_log(self):
        """SSE 日志订阅：返回一个接收新日志行的队列。"""
        q = queue.Queue(maxsize=1000)
        with self._log_lock:
            self._log_waiters.add(q)
        return q

    def unsubscribe_log(self, q):
        with self._log_lock:
            self._log_waiters.discard(q)

    def log(self, msg):
        # 屏蔽 Web 管理界面加载 / 静态资源请求 / DHCP 发送响应日志
        if ("Web 管理界面加载" in msg or "/style.css" in msg
                or "/app.js" in msg or "DHCP 发送响应" in msg):
            return
        # 已上线客户端的中间过程日志：仅保留上线成功相关，避免刷屏
        if self._is_online_client_log(msg):
            return
        now = _cn_now()
        ts = now.strftime("%H:%M:%S") + f".{now.microsecond // 1000:03d}"
        line = f"[{ts}] {msg}"
        # 文件追加与上线清理回写都持有 _log_lock，避免并发写竞态
        with self._log_lock:
            self._log_buf.append(line)
            if self._log_file:
                try:
                    with open(self._log_file, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass
            for q in list(self._log_waiters):
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass
        if not self.quiet:
            print(line)

    def logs(self, n=None):
        # 合并常规日志 + 各进度 key 的最新一行，并按时间戳排序
        # （进度行来自 _latest_progress，单独追加会造成显示乱序）
        out = list(self._log_buf) + list(self._latest_progress.values())
        out.sort(key=self._ts_key)
        return out[-n:] if n else out

    @staticmethod
    def _ts_key(line):
        """日志行 "[HH:MM:SS.mmm] ..." → 可比较毫秒数。"""
        try:
            return (int(line[1:3]) * 3600 + int(line[4:6]) * 60
                    + int(line[7:9])) * 1000 + int(line[10:13])
        except Exception:
            return 0

    def log_progress(self, key, msg):
        """进度日志：在原条目上刷新（同 key 覆盖），不逐条追加。

        SSE 推送带替换标记（\\x1eR\\x1e<key>\\x1e<line>），前端收到后
        替换对应条目而不是追加一行。已上线客户端的进度不再显示。
        """
        if self._is_online_client_log(msg):
            return
        now = _cn_now()
        ts = now.strftime("%H:%M:%S") + f".{now.microsecond // 1000:03d}"
        line = f"[{ts}] {msg}"
        self._latest_progress[key] = line
        with self._log_lock:
            for q in list(self._log_waiters):
                try:
                    q.put_nowait("\x1eR\x1e" + key + "\x1e" + line)
                except queue.Full:
                    pass

    def _on_client(self, mac, info):
        key = self._norm_mac(mac)
        now = time.time()
        st = self._client_state.get(key)
        if st is None:
            st = {"mac": self._fmt_mac(key), "ip": info.get("ip"),
                  "time": _cn_now().strftime("%H:%M:%S"), "arch": info.get("arch"),
                  "hostname": info.get("hostname"),
                  "state": "正在获取引导配置", "state_ts": now,
                  "seq": self._seq_next, "ips": set()}
            self._seq_next += 1
            self._client_state[key] = st
        else:
            st["ip"] = info.get("ip") or st.get("ip")
            if info.get("arch") is not None:
                st["arch"] = info.get("arch")
            st["hostname"] = info.get("hostname") or st.get("hostname")
            # 已上线/已下线的客户端再次发起 DHCP = 重新引导，重置状态
            if st.get("state") in ("已上线", "已下线"):
                st["state"] = "正在获取引导配置"
                st["state_ts"] = now
        if info.get("ip"):
            self._ip_mac[str(info["ip"])] = key
            st.setdefault("ips", set()).add(str(info["ip"]))
        self._client_events.appendleft({"time": _cn_now().strftime("%H:%M:%S"),
                                        "mac": self._fmt_mac(key), **info})

    def client_http_event(self, ip, step):
        """HTTP 请求（按源 IP 关联客户端）更新进行中的步骤。"""
        key = self._ip_mac.get(str(ip))
        if not key:
            return
        st = self._client_state.get(key)
        if st and st.get("state") != "已上线":
            st["state"] = "正在" + step
            st["state_ts"] = time.time()

    def client_http_result(self, ip, ok, step):
        """HTTP 响应结果：失败时把状态标为 步骤+失败。"""
        key = self._ip_mac.get(str(ip))
        if not key:
            return
        st = self._client_state.get(key)
        if st and not ok and st.get("state") != "已上线":
            st["state"] = step + "失败"
            st["state_ts"] = time.time()

    def client_online(self, mac, ip=None):
        """PE 启动完成（ping 通服务器）后通知上线。

        上线成功后清掉该客户端的全部过程日志，只保留"已上线00X号"一条，
        并通知前端同步清掉界面上已显示的过程日志。
        ip 由 PE 上报（proxy 模式下 DHCP 不分配地址，服务器拿不到 IP，
        必须由 PE 携带，否则无法显示真实 IP，也无法做下线 ping 检测）。
        """
        key = self._norm_mac(mac)
        st = self._client_state.get(key)
        if st is None:
            # 兜底：未经过 DHCP 建档（proxy 环境/建档缺失）也能登记上线
            st = {"mac": self._fmt_mac(key), "ip": ip,
                  "time": _cn_now().strftime("%H:%M:%S"), "arch": None,
                  "hostname": "", "state": "正在获取引导配置",
                  "state_ts": time.time(), "seq": self._seq_next}
            self._seq_next += 1
            self._client_state[key] = st
        if ip:
            st["ip"] = str(ip)
            self._ip_mac[str(ip)] = key
            st.setdefault("ips", set()).add(str(ip))
        if st:
            st["state"] = "已上线"
            st["state_ts"] = time.time()
            self._ever_online.add(key)
            self.log(f"客户端 {st.get('ip') or '?'} ({self._fmt_mac(key)}) "
                     f"已上线{st.get('seq', 0):03d}号")
            # 清掉该客户端全部过程日志，只保留上线成功这一条
            self._purge_client_logs(key)
            # 通知前端同步清掉该客户端已显示的过程日志（无需刷新页面）
            self._notify_client_purged(key)

    def _notify_client_purged(self, key):
        """向前端推送清理事件：\\x1eP\\x1e<ip1,ip2,...>\\x1e<macColon>\\x1e<macPlain>。

        前端收到后删除该客户端的全部过程日志行，只保留上线/下线状态行。
        ip 以逗号分隔携带该客户端用过的全部历史 IP（DHCP 分配可能变化，
        只传最终 IP 会漏掉旧 IP 的引导日志）。
        """
        st = self._client_state.get(key)
        if not st:
            return
        ip = str(st.get("ip") or "")
        ips = list(st.get("ips") or [])
        if ip and ip not in ips:
            ips.append(ip)
        ip_str = ",".join(ips)
        mac_colon = (self._fmt_mac(key) or "").lower()
        mac_plain = (key or "").lower()
        with self._log_lock:
            for q in list(self._log_waiters):
                try:
                    q.put_nowait("\x1eP\x1e" + ip_str + "\x1e" + mac_colon
                                 + "\x1e" + mac_plain)
                except queue.Full:
                    pass

    def _purge_client_logs(self, key):
        """客户端上线成功后，从 log_buf 中清掉它的全部过程日志。

        只保留上线/下线状态行（"已上线00X号"/"已下线00X号"），
        过程日志（DHCP/TFTP/下载/进度等）不再显示。
        按 MAC + 全部历史 IP 匹配，避免客户端换 IP 后旧引导日志残留。
        """
        st = self._client_state.get(key)
        if not st:
            return
        ip = str(st.get("ip") or "")
        ips = list(st.get("ips") or [])
        if ip and ip not in ips:
            ips.append(ip)
        mac_colon = (self._fmt_mac(key) or "").lower()
        mac_plain = (key or "").lower()
        keep = []
        with self._log_lock:
            for line in self._log_buf:
                if self._line_is_client(line, ips, mac_colon, mac_plain):
                    if "已上线" in line or "已下线" in line:
                        keep.append(line)
                else:
                    keep.append(line)
            self._log_buf.clear()
            self._log_buf.extend(keep)
            # 同步清理 pxe.log 文件：只保留上线/下线状态行，
            # 避免 tail 日志文件时看到已上线客户端的引导过程日志残留。
            if self._log_file:
                self._purge_log_file(ips, mac_colon, mac_plain)
        # 同时清掉该客户端的进度日志（合并显示时不再带回来）
        for k in list(self._latest_progress):
            if self._line_is_client(self._latest_progress[k], ips,
                                    mac_colon, mac_plain):
                del self._latest_progress[k]

    def _purge_log_file(self, ips, mac_colon, mac_plain):
        """回写 pxe.log：清掉指定客户端的全部过程日志（保留上线/下线行）。

        与 _log_buf 同规则（_line_is_client 匹配），保证文件与界面一致。
        调用方须持有 _log_lock。
        """
        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                file_lines = f.readlines()
            kept = []
            for raw in file_lines:
                line = raw.rstrip("\n")
                if self._line_is_client(line, ips, mac_colon, mac_plain):
                    if "已上线" in line or "已下线" in line:
                        kept.append(raw)
                else:
                    kept.append(raw)
            with open(self._log_file, "w", encoding="utf-8") as f:
                f.writelines(kept)
        except OSError:
            pass

    def _line_is_client(self, line, ips, mac_colon, mac_plain):
        """判断日志行是否属于指定客户端（MAC 或任意历史 IP 关联）。

        IP 用“包含 + 边界”匹配：覆盖 [IP]、至 ('IP', port)、来自 IP:、客户端 IP ( 等
        所有日志格式；同时避免 192.168.10.15 误匹配 192.168.10.153。
        """
        if not line:
            return False
        low = line.lower()
        if mac_colon and ("客户端mac=" + mac_colon in low
                          or "mac=" + mac_colon in low):
            return True
        if mac_plain and mac_plain in low.replace(":", ""):
            return True
        for ip in (ips or []):
            if not ip:
                continue
            if _contains_ip(low, ip):
                return True
        return False

    # PE 内脚本按阶段回报：下载成功 / 挂载成功 / 解压成功
    STEP_TEXTS = {
        "downloaded": ("正在挂载虚拟光驱", "系统镜像下载完成"),
        "mounted":    ("正在解压外置软件", "虚拟光驱挂载成功 (Y:)"),
        "extracted":  ("正在加载外置工具", "外置软件解压成功"),
    }

    def client_step(self, mac, step):
        """PE 步骤回报：更新客户端状态并记录中文日志。"""
        key = self._norm_mac(mac)
        st = self._client_state.get(key)
        if not st or st.get("state") == "已上线":
            return
        pair = self.STEP_TEXTS.get(step)
        if not pair:
            return
        state, logtxt = pair
        st["state"] = state
        st["state_ts"] = time.time()
        self.log(f"客户端 {st.get('ip') or '?'} ({self._fmt_mac(key)}) {logtxt}")

    def client_vnc(self, mac, port, state):
        """PE 内 VNC 服务端状态回报：state = online / offline，port 为 VNC 端口。

        供 Web 界面显示"远程控制"入口；客户端整体下线时由 _check_online 联动清空。
        """
        key = self._norm_mac(mac)
        st = self._client_state.get(key)
        if st is None:
            return  # 未经 DHCP 建档的客户端不登记 VNC
        port = max(1, min(65535, int(port or 5900)))
        online = state == "online" or state == "1" or state == "on"
        st["vnc"] = {
            "port": port,
            "state": "online" if online else "offline",
            "ts": time.time(),
        }
        if online:
            self.log(f"客户端 {st.get('ip') or '?'} ({self._fmt_mac(key)}) "
                     f"VNC 已就绪（端口 {port}），可远程控制")
        else:
            self.log(f"客户端 {st.get('ip') or '?'} ({self._fmt_mac(key)}) VNC 已停止")

    # ---------------- 已上线客户端下线检测 ----------------
    def _ping(self, ip):
        """探测客户端是否在线。
        Linux 容器内没有 ping 命令（精简镜像），改用 TCP 连接探测：
        优先 VNC 5900（PE 在线且 VNC 就绪的强信号），回退常见 Windows 端口。
        """
        try:
            import socket
            for port in (5900, 135, 139, 445):
                try:
                    s = socket.create_connection((ip, port), timeout=1.5)
                    s.close()
                    return True
                except OSError:
                    continue
            return False
        except Exception:
            return False

    def _check_online(self):
        """每 30 秒检测已上线/已下线客户端：ping 不通 → 已下线00X号；恢复 → 已上线00X号。"""
        for key, st in list(self._client_state.items()):
            ip = st.get("ip")
            if not ip:
                continue
            state = st.get("state")
            if state not in ("已上线", "已下线"):
                continue
            alive = self._ping(ip)
            seq = st.get("seq", 0)
            if state == "已上线" and not alive:
                st["state"] = "已下线"
                st["state_ts"] = time.time()
                if st.get("vnc"):
                    st["vnc"]["state"] = "offline"
                self.log(f"客户端 {ip} ({st['mac']}) 已下线{seq:03d}号")
            elif state == "已下线" and alive:
                st["state"] = "已上线"
                st["state_ts"] = time.time()
                self._ever_online.add(key)
                # PE 若回报过 VNC 端口，恢复上线时同步恢复 VNC 在线（PE 未重启的话
                # TightVNC 仍在监听；下次 PE 回报会刷新真实状态）
                if st.get("vnc") and st["vnc"].get("port"):
                    st["vnc"]["state"] = "online"
                self.log(f"客户端 {ip} ({st['mac']}) 已上线{seq:03d}号")

    def _monitor_loop(self):
        while self._running:
            deadline = time.time() + 30
            try:
                self._check_online()
            except Exception:
                pass
            left = deadline - time.time()
            if left > 0:
                time.sleep(left)

    # ---------------- 引导文件提供 ----------------
    def _tftp_file(self, name):
        """TFTP 动态文件提供器（支持 GRUB/pxelinux 前缀路径，如 /grub/grub.cfg）。"""
        norm = name.replace("\\", "/").lstrip("/")
        if norm in ("grub.cfg", "grub/grub.cfg"):
            return self.generate_grub_cfg().encode("utf-8")
        if norm in ("pxelinux.cfg/default", "pxelinux.cfg/pxelinux.cfg/default"):
            return self.generate_pxelinux_cfg().encode("utf-8")
        if norm in ("ipxe.cfg", "grub/ipxe.cfg"):
            return self.generate_ipxe_cfg().encode("utf-8")
        # iPXE 默认脚本搜索（autoexec.ipxe / default.ipxe / <mac>.ipxe）
        if norm in ("autoexec.ipxe", "default.ipxe", "loader/autoexec.ipxe", "loader/default.ipxe"):
            return self.generate_ipxe_cfg().encode("utf-8")
        if norm.endswith(".ipxe") and "-" in norm or (norm.endswith(".ipxe") and norm.split(".")[0].count(":") >= 3):
            # <mac>.ipxe（如 aa-bb-cc-dd-ee-ff.ipxe 或 00:0c:...ipxe）
            return self.generate_ipxe_cfg().encode("utf-8")
        if norm in ("wimboot", "boot/wimboot"):
            # wimboot 补齐到其 setup 头声明的 syssize（341792B），
            # 避免 GRUB linux 加载器按 syssize 读取时短读卡死
            p = os.path.join(self.config.boot_dir, "wimboot")
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    data = f.read()
                if len(data) < 341792:
                    data += b"\x00" * (341792 - len(data))
                return data
            return None
        if norm.startswith("wim/"):
            # TFTP 提取 ISO 内文件：wim/<iso相对路径>/<ISO内路径>
            rest = norm[len("wim/"):]
            if "/" in rest:
                iso_rel, inner = rest.split("/", 1)
                real = self.registry.resolve_iso(iso_rel)
                if real:
                    data = self._read_iso_inner(real, inner)
                    if data is not None:
                        return data
        return None

    def _read_iso_inner(self, iso_path, inner, max_size=64 * 1024 * 1024):
        """从 ISO 读取内部文件（供 TFTP 提供 wimboot 的 bootmgfw/BCD/sdi 等）。"""
        from .iso9660 import read_iso_index, _norm
        try:
            idx = read_iso_index(iso_path)
        except Exception:
            return None
        key = _norm(inner)
        rec = idx.get(key)
        if rec is None:
            cand = next((k for k in idx if k.replace(";", "").split(";")[0] == key), None)
            rec = idx.get(cand) if cand else None
        if rec is None:
            return None
        size = int(rec.get("size", 0))
        if size > max_size:
            # 大文件（如 WIM 179MB）走 HTTP，TFTP 只服务小引导文件
            return None
        lba = rec.get("lba")
        if lba is None:
            return None
        try:
            with open(iso_path, "rb") as f:
                f.seek(int(lba) * 2048)
                return f.read(size)
        except OSError:
            return None

    @property
    def web_dir(self):
        return self.config.web_dir

    @property
    def boot_dir(self):
        return self.config.boot_dir

    def generate_grub_cfg(self):
        return grubcfg.gen_grub_cfg(self.config, self.registry.entries())

    def generate_pxelinux_cfg(self):
        return grubcfg.gen_pxelinux_cfg(self.config, self.registry.entries())

    def generate_ipxe_cfg(self):
        return grubcfg.gen_ipxe_script(self.config, self.registry.entries())

    def resolve_iso(self, url_path):
        return self.registry.resolve_iso(url_path)

    def iso_list(self):
        out = []
        for e in self.registry.entries():
            out.append({
                "name": e["name"], "url": e["url"], "group": e["group"],
                "size": e["size"], "mtime": e["mtime"],
                "os_type": e["conf"].get("os_type", "unknown"),
                "reason": e["conf"].get("reason", ""),
                "kernel": e["conf"].get("kernel", ""),
                "initrd": e["conf"].get("initrd", ""),
                "args": e["conf"].get("args", ""),
                "wimboot_files": e["conf"].get("wimboot_files", []),
                "label": e["conf"].get("label", ""),
                "has_override": bool(self.bootconf.get(e["name"])),
            })
        return out

    def boot_status(self):
        """检查引导文件是否就绪。"""
        root = self.config.boot_dir
        checks = [
            ("grubx64.efi", "UEFI x86-64 GRUB2（必需）"),
            ("grubia32.efi", "UEFI IA32 GRUB2（可选）"),
            ("grubaa64.efi", "UEFI ARM64 GRUB2（可选）"),
            ("pxelinux.0", "BIOS PXELINUX（可选）"),
            ("ldlinux.c32", "PXELINUX 依赖（可选）"),
            ("menu.c32", "PXELINUX 菜单模块（可选）"),
            ("libutil.c32", "PXELINUX 菜单依赖（可选）"),
            ("libcom32.c32", "PXELINUX 菜单依赖（可选）"),
            ("memdisk", "通用 ISO 引导（可选）"),
            ("wimboot", "Windows ISO 引导（wimboot，UEFI/iPXE 使用）"),
            ("undionly.kpxe", "iPXE BIOS 引导（可选）"),
            ("ipxe.efi", "iPXE UEFI x86-64 引导（Windows UEFI 必需）"),
            ("ipxeia32.efi", "iPXE UEFI IA32 引导（可选）"),
            ("ipxea64.efi", "iPXE UEFI ARM64 引导（可选）"),
        ]
        return [{"name": n, "ok": os.path.isfile(os.path.join(root, n)),
                 "desc": d} for n, d in checks]

    def status(self):
        cfg = self.config
        # DHCP 显示与磁盘 config.json 同源：launcher 直接写文件时，
        # 服务进程内存可能滞后，实时读文件保证 WEB 与窗口启动器显示一致
        try:
            with open(cfg.path, "r", encoding="utf-8") as _f:
                _disk = json.load(_f)
            _de = bool(_disk.get("dhcp_enable", cfg.dhcp_enable))
            _dm = _disk.get("dhcp_mode", cfg.dhcp_mode)
        except Exception:
            _de, _dm = cfg.dhcp_enable, cfg.dhcp_mode
        return {
            "server_ip": cfg.server_ip,
            "http_port": cfg.http_port,
            "tftp_port": cfg.tftp_port,
            "dhcp_port": cfg.dhcp_port,
            "dhcp_enable": _de,
            "dhcp_mode": _dm,
            "uptime": int(time.time() - self.start_time),
            "isos": len([e for e in self.registry.entries()]),
            "leases": self.dhcp.leases_snapshot(),
            "online_count": sum(1 for c in self._client_state.values() if c.get("state") == "已上线"),
            "offline_count": sum(1 for c in self._client_state.values() if c.get("state") == "已下线"),
            "total_online": len(self._ever_online),
            "clients": self._clients_dedup(),
            "dhcp_stats": dict(self.dhcp.stats),
            "tftp_stats": dict(self.tftp.stats),
            "boot_files": self.boot_status(),
            "logs": self.logs(min(200, self._log_buf.maxlen)),
        }

    def _clients_dedup(self):
        """客户端状态列表（按首次 DHCP 出现的顺序），每条含当前引导状态。

        st 内部 ips 是 set（历史 IP 去重），JSON 序列化前转成排序列表。
        """
        out = []
        for st in list(self._client_state.values())[:50]:
            item = dict(st)
            if isinstance(item.get("ips"), set):
                item["ips"] = sorted(item["ips"])
            out.append(item)
        return out

    # ---------------- 启动 / 停止 ----------------
    def start(self):
        cfg = self.config
        # 1) HTTP
        try:
            http_addr = ("0.0.0.0", cfg.http_port)
            self.httpd = PxeHttpServer(http_addr, PxeHttpHandler, self)
            t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            t.start()
            self._threads.append(t)
            self.log(f"HTTP 服务器已启动 (端口 {cfg.http_port})")
            self.log(f"Web 管理界面: http://{cfg.server_ip}:{cfg.http_port}/")
        except OSError as e:
            self.log(f"HTTP 启动失败（端口 {cfg.http_port} 可能被占用或需要管理员权限）: {e}")

        # 2) TFTP
        if cfg.tftp_port:
            try:
                t = threading.Thread(target=self.tftp.start, daemon=True)
                t.start()
                self._threads.append(t)
            except OSError as e:
                self.log(f"TFTP 启动失败: {e}")

        # 3) DHCP
        if cfg.dhcp_enable and cfg.dhcp_mode != "off":
            try:
                t = threading.Thread(target=self.dhcp.start, daemon=True)
                t.start()
                self._threads.append(t)
            except OSError as e:
                self.log(f"DHCP 启动失败（端口 {cfg.dhcp_port} 需要管理员权限或被占用）: {e}")
        else:
            self.log("DHCP 服务器已禁用（可设置 dhcp_mode=proxy 与现有路由器 DHCP 共存）")

        # 4) 已上线客户端下线检测（每 30 秒）
        self._running = True
        t = threading.Thread(target=self._monitor_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self):
        self._running = False
        self.dhcp.stop()
        self.tftp.stop()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.log("所有服务已停止")
