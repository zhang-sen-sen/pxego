# -*- coding: utf-8 -*-
"""
内置 DHCP 服务器（RFC 2131/2132 + PXE 扩展）。

两种模式：
- dhcp  ：本服务作为网络中唯一的 DHCP 服务器，负责分配地址并下发 PXE 引导参数；
- proxy ：代理 DHCP，只响应 PXE 客户端（vendor class 以 "PXEClient" 开头），
          仅下发引导选项（option 66/67），与现有路由器 DHCP 共存；
- off   ：不启动。

同时提供客户端信息收集（MAC / 架构 / 厂商类 / 主机名）供 Web 界面展示。
"""
import ipaddress
import socket
import struct
import threading
import time

# DHCP 消息类型
DHCP_DISCOVER = 1
DHCP_OFFER = 2
DHCP_REQUEST = 3
DHCP_DECLINE = 4
DHCP_ACK = 5
DHCP_NAK = 6
DHCP_RELEASE = 7
DHCP_INFORM = 8

MSG_NAMES = {
    DHCP_DISCOVER: "发现",
    DHCP_OFFER: "提供",
    DHCP_REQUEST: "请求",
    DHCP_DECLINE: "拒绝",
    DHCP_ACK: "确认",
    DHCP_NAK: "拒绝",
    DHCP_RELEASE: "释放",
    DHCP_INFORM: "通知",
}

MAGIC_COOKIE = b"\x63\x82\x53\x63"

# option 编号
OPT_MSG_TYPE = 53
OPT_SERVER_ID = 54
OPT_REQUESTED_IP = 50
OPT_LEASE_TIME = 51
OPT_MESSAGE = 56
OPT_RENEWAL = 58
OPT_REBIND = 59
OPT_VENDOR_CLASS = 60
OPT_CLIENT_ARCH = 93
OPT_CLIENT_NDI = 94
OPT_CLIENT_MACHINE_ID = 97
OPT_HOST_NAME = 12
OPT_SUBNET_MASK = 1
OPT_ROUTER = 3
OPT_DNS = 6
OPT_DOMAIN = 15
OPT_TFTP_SERVER = 66
OPT_BOOTFILE = 67
OPT_BROADCAST_ADDR = 28
OPT_INTERFACE_MTU = 26
OPT_USER_CLASS = 77
OPT_IPXE_SCRIPT = 175
OPT_END = 255
OPT_PAD = 0

ARCH_NAMES = {
    0x0000: "BIOS",
    0x0006: "EFI IA32",
    0x0007: "EFI x86-64",
    0x0009: "EFI ARM64",
    0x000B: "EFI x86-64 HTTP",
}


def _mac_str(raw):
    return ":".join(f"{b:02x}" for b in raw)


# ------------------------------------------------------------------ 报文编解码 --

class DhcpPacket:
    """DHCP/BOOTP 报文解析与构造。"""

    def __init__(self, raw=None):
        self.op = 1
        self.htype = 1
        self.hlen = 6
        self.hops = 0
        self.xid = 0
        self.secs = 0
        self.flags = 0
        self.ciaddr = "0.0.0.0"
        self.yiaddr = "0.0.0.0"
        self.siaddr = "0.0.0.0"
        self.giaddr = "0.0.0.0"
        self.chaddr = b"\x00" * 16
        self.options = {}
        if raw:
            self._parse(raw)

    def _parse(self, raw):
        if len(raw) < 240:
            raise ValueError("报文过短")
        self.op = raw[0]
        self.htype = raw[1]
        self.hlen = raw[2]
        self.hops = raw[3]
        self.xid = struct.unpack(">I", raw[4:8])[0]
        self.secs = struct.unpack(">H", raw[8:10])[0]
        self.flags = struct.unpack(">H", raw[10:12])[0]
        self.ciaddr = str(ipaddress.ip_address(raw[12:16]))
        self.yiaddr = str(ipaddress.ip_address(raw[16:20]))
        self.siaddr = str(ipaddress.ip_address(raw[20:24]))
        self.giaddr = str(ipaddress.ip_address(raw[24:28]))
        self.chaddr = raw[28:44]
        # options 从 236 开始（BOOTP 固定头 236 字节）
        pos = 236
        if raw[236:240] != MAGIC_COOKIE:
            return
        pos = 240
        while pos < len(raw):
            code = raw[pos]
            if code == OPT_END:
                break
            if code == OPT_PAD:
                pos += 1
                continue
            ln = raw[pos + 1]
            self.options[code] = raw[pos + 2: pos + 2 + ln]
            pos += 2 + ln

    @property
    def msg_type(self):
        v = self.options.get(OPT_MSG_TYPE)
        return v[0] if v else None

    @property
    def vendor_class(self):
        v = self.options.get(OPT_VENDOR_CLASS)
        return v.decode("latin-1") if v else ""

    @property
    def client_arch(self):
        v = self.options.get(OPT_CLIENT_ARCH)
        if v and len(v) >= 2:
            return struct.unpack(">H", v[:2])[0]
        return None

    @property
    def requested_ip(self):
        v = self.options.get(OPT_REQUESTED_IP)
        if v and len(v) == 4:
            return str(ipaddress.ip_address(v))
        return None

    @property
    def hostname(self):
        v = self.options.get(OPT_HOST_NAME)
        return v.decode("utf-8", "replace") if v else ""

    @property
    def is_pxe(self):
        return self.vendor_class.upper().startswith("PXECLIENT")

    @property
    def is_ipxe(self):
        """iPXE 客户端（重 DHCP 阶段以 vendor class 'iPXE' 请求）。"""
        return self.vendor_class.upper().startswith("IPXE")

    @property
    def is_pxe_family(self):
        return self.is_pxe or self.is_ipxe

    @property
    def mac(self):
        return _mac_str(self.chaddr[:self.hlen]) if self.hlen else ""


def _opt(code, value):
    """构造 DHCP option。IP 字符串（如 "192.168.1.1"）须转为 4 字节二进制，
    普通字符串（bootfile/vendor/脚本）原样编码，int 打包为大端 4 字节。"""
    if value is None:
        return b""
    if isinstance(value, str):
        try:
            value = ipaddress.ip_address(value).packed
        except ValueError:
            value = value.encode()
    elif isinstance(value, int):
        value = struct.pack(">I", value)
    return bytes([code, len(value)]) + value


def build_dhcp_reply(req, msg_type, yiaddr="0.0.0.0", siaddr="0.0.0.0",
                     options=None, bootfile=None):
    """根据请求报文构造 DHCP 应答。"""
    options = options or {}
    opts = bytearray()
    opts += _opt(OPT_MSG_TYPE, bytes([msg_type]))
    for code, val in options.items():
        if val is None:
            continue
        opts += _opt(code, val)
    if bootfile:
        opts += _opt(OPT_BOOTFILE, bootfile)
    # option 66（TFTP 服务器名，文本格式）：部分固件忽略 siaddr 只认此选项。
    if siaddr != "0.0.0.0":
        opts += _opt(OPT_TFTP_SERVER, siaddr.encode())
    # option 43（PXE 厂商选项，Intel PXE 2.1）。EDK2 的 PxeBcParseVendorOptions
    # 直接从 value[0] 按子选项 TLV 解析，子选项 6（DiscoveryControl）置位使 offer
    # 判定为 PXE10。值 0x08（bit3）= 禁用 prompt/menu：EDK2 的 PxeBcSelectBootPrompt
    # 在"bit3 置位且存在 bootfile"时返回 EFI_ABORTED，从而跳过菜单与 BOOTSTRAP
    # 中止检查，直接用 DHCP 提供的 bootfile 走 TFTP（这是让 UEFI 客户端直接引导
    # 的标准做法；0x02 会导致客户端走默认菜单→BOOTSTRAP→中止）。
    opts += _opt(43, bytes([6, 1, 8]))
    # option 17（root path）：供 GRUB 的 grubnet 定位配置文件目录。
    # GRUB 加载 (tftp,$server)/<root-path>/grub.cfg；TFTP 动态生成
    # grub/grub.cfg，故 root-path = "grub"（Debian GRUB 2.06 默认前缀
    # 为 /boot/grub，PXE 下不匹配，必须用 root-path 显式指定）。
    opts += _opt(17, b"grub")
    opts += bytes([OPT_END])
    # 填充到至少 300 字节（RFC 2131 最小 300）
    while 236 + 4 + len(opts) < 300:
        opts += bytes([OPT_PAD])

    pkt = bytearray()
    pkt += bytes([2, req.htype, req.hlen, 0])                 # op=BOOTREPLY
    pkt += struct.pack(">I", req.xid)
    pkt += struct.pack(">H", 0)                               # secs
    pkt += struct.pack(">H", req.flags & 0x8000)              # 保留广播位
    pkt += ipaddress.ip_address(req.ciaddr).packed
    pkt += ipaddress.ip_address(yiaddr).packed
    pkt += ipaddress.ip_address(siaddr).packed
    pkt += ipaddress.ip_address(req.giaddr).packed
    pkt += req.chaddr.ljust(16, b"\x00")[:16]
    pkt += b"\x00" * 64                                       # sname
    # file 字段：PXE 兼容关键。部分 UEFI 固件（如 VMware）只读 BOOTP file
    # 字段作为引导文件名，不读 option 67。故 bootfile 同时写入 file 字段。
    if bootfile and len(bootfile.encode()) <= 128:
        pkt += bootfile.encode().ljust(128, b"\x00")
    else:
        pkt += b"\x00" * 128
    pkt += MAGIC_COOKIE
    pkt += opts
    return bytes(pkt)


# ------------------------------------------------------------------ 服务器 ----

class DhcpServer:
    def __init__(self, config, on_client=None, log=None, is_online=None):
        self.config = config
        self.on_client = on_client     # 可选回调：记录客户端信息
        self.log = log or (lambda msg: None)
        # is_online(mac) -> bool：客户端是否已上线。
        # 已上线客户端产生的 DHCP 交互日志（收到/确认/响应）不再记录，
        # 避免上线成功后日志继续刷 DHCP 过程。
        self._is_online = is_online
        self.sock = None
        self.running = False
        self._leases = {}              # mac -> dict(ip, xid, state, ts)
        self._ip_used = {}             # ip -> mac
        self._lock = threading.Lock()
        self.clients = {}              # mac -> dict(ip, arch, vendor, hostname, last_seen)
        self.stats = {"offers": 0, "acks": 0, "naks": 0, "releases": 0, "ignored": 0}

    def _log(self, msg, mac=None):
        """DHCP 日志入口：已上线客户端的 DHCP 交互日志跳过。"""
        if mac and self._is_online and self._is_online(mac):
            return
        self.log(msg)

    # ---------------- 地址池 ----------------
    def _pool(self):
        p = self.config.data.get("ip_pool", {})
        start = ipaddress.ip_address(p.get("start", "192.168.1.100"))
        end = ipaddress.ip_address(p.get("end", "192.168.1.200"))
        if int(end) < int(start):
            start, end = end, start
        return start, end

    def _server_net(self):
        """server_ip 所在子网（按 netmask）。DHCP 只服务该子网内的客户端。"""
        mask = self.config.data.get("netmask", "255.255.255.0")
        try:
            return ipaddress.ip_network(f"{self.config.server_ip}/{mask}", strict=False)
        except ValueError:
            return ipaddress.ip_network("0.0.0.0/0")

    def _pool_ips(self):
        start, end = self._pool()
        cur = int(start)
        while cur <= int(end):
            yield str(ipaddress.ip_address(cur))
            cur += 1

    def _alloc(self, mac):
        """为 MAC 分配一个地址池内的 IP（避免与已占用冲突）。
        只分配 server_ip 所在子网内的地址：切换到其他网卡后，
        旧网段池中的地址全部跳过，旧网段客户端不再获得租约。"""
        with self._lock:
            lease = self._leases.get(mac)
            if lease and lease["state"] == "bound":
                return lease["ip"]
            server_ip = self.config.server_ip
            net = self._server_net()
            for ip in self._pool_ips():
                if ip == server_ip:
                    continue
                if ipaddress.ip_address(ip) not in net:
                    continue
                if ip in self._ip_used and self._ip_used[ip] != mac:
                    continue
                self._ip_used[ip] = mac
                self._leases[mac] = {"ip": ip, "xid": 0, "state": "bound",
                                     "ts": time.time()}
                return ip
        return None

    def _free(self, mac):
        with self._lock:
            lease = self._leases.pop(mac, None)
            if lease:
                self._ip_used.pop(lease["ip"], None)

    def _mac_allowed(self, mac):
        mf = self.config.mac_filter()
        mode = mf.get("mode", "off")
        lst = [m.lower() for m in mf.get("list", [])]
        if mode == "off":
            return True
        if mode == "allow":
            return mac.lower() in lst
        if mode == "deny":
            return mac.lower() not in lst
        return True

    def _ip_repr(self, raw):
        """把 option 中的 4 字节 IP 原始值解析为可读 IP 字符串。"""
        if raw and len(raw) == 4:
            try:
                return str(ipaddress.ip_address(raw))
            except ValueError:
                pass
        return None

    def _arch_txt(self, arch):
        if arch is None:
            return "未知"
        return f"{arch} ({ARCH_NAMES.get(arch, '未知')})"

    # ---------------- 核心处理 ----------------
    def dispatch(self, data, addr):
        """处理一条 DHCP 报文；返回 (reply_bytes, dest_addr) 或 None。"""
        try:
            req = DhcpPacket(data)
        except ValueError:
            return None
        if req.op != 1 or req.hlen not in (0, 6):
            return None
        if not self._mac_allowed(req.mac):
            return None

        mtype = req.msg_type
        mode = self.config.dhcp_mode
        cfg = self.config.data
        self._log(f"DHCP 收到请求 类型={MSG_NAMES.get(mtype, mtype)} "
                  f"客户端MAC={req.mac} 架构={self._arch_txt(req.client_arch)} "
                  f"厂商={req.vendor_class or '(空)'} 来源={addr[0]}:{addr[1]}", req.mac)
        http_port = int(cfg.get("http_port", 80) or 80)
        ipxe_script = (f"http://{cfg.get('server_ip')}:{http_port}/ipxe.cfg"
                       if http_port != 80 else f"http://{cfg.get('server_ip')}/ipxe.cfg")

        # ---- 代理模式：只处理 PXE/iPXE 客户端，只发引导选项 ----
        if mode == "proxy":
            if mtype in (DHCP_DISCOVER,) and req.is_pxe_family:
                bootfile = self.config.bootfile_for_arch(req.client_arch or 0x00)
                opts = {
                    # server id：让固件识别这是"代理 DHCP 引导建议"（EDK2 固件
                    # 对无 server id 的代理 OFFER 会忽略并继续重发 DISCOVER）
                    OPT_SERVER_ID: cfg.get("server_ip"),
                    OPT_VENDOR_CLASS: req.options.get(OPT_VENDOR_CLASS),
                    # iPXE 链式脚本地址（iPXE 客户端据此拉取 ipxe.cfg）
                    OPT_IPXE_SCRIPT: ipxe_script,
                }
                reply = build_dhcp_reply(req, DHCP_OFFER, yiaddr="0.0.0.0",
                                         siaddr=cfg.get("server_ip"),
                                         options=opts, bootfile=bootfile)
                dest = self._dest(req, None)
                self.stats["offers"] += 1
                self._note_client(req, None)
                return reply, dest, req.mac
            return None

        if mode != "dhcp":
            return None

        if mtype == DHCP_DISCOVER:
            if not req.is_pxe:
                # 非 PXE 客户端：也分配地址（作为唯一 DHCP 服务器）
                pass
            ip = self._alloc(req.mac)
            if ip is None:
                return None
            bootfile = self.config.bootfile_for_arch(req.client_arch or 0x00)
            opts = {
                OPT_SERVER_ID: cfg.get("server_ip"),
                OPT_LEASE_TIME: self.config.lease_time(),
                OPT_RENEWAL: self.config.lease_time() // 2,
                OPT_REBIND: int(self.config.lease_time() * 0.875),
                OPT_SUBNET_MASK: cfg.get("netmask"),
                OPT_ROUTER: cfg.get("gateway") or None,
                OPT_DNS: cfg.get("dns") or None,
                OPT_DOMAIN: cfg.get("domain") or None,
                OPT_VENDOR_CLASS: req.options.get(OPT_VENDOR_CLASS),
                OPT_IPXE_SCRIPT: ipxe_script,
            }
            reply = build_dhcp_reply(req, DHCP_OFFER, yiaddr=ip, siaddr=cfg.get("server_ip"),
                                     options=opts, bootfile=bootfile)
            self.stats["offers"] += 1
            self._note_client(req, ip)
            return reply, self._dest(req, ip), req.mac

        if mtype == DHCP_REQUEST:
            # 客户端在 REQUEST 中携带 option 54（选中的服务器）：
            # 若指向其他 DHCP 服务器，本服务器应忽略，不得替它 NAK。
            srv_opt = req.options.get(OPT_SERVER_ID)
            if srv_opt and srv_opt != ipaddress.ip_address(cfg.get("server_ip")).packed:
                self.stats["ignored"] += 1
                return None
            ip = self._leases.get(req.mac, {}).get("ip")
            ci = req.ciaddr if req.ciaddr != "0.0.0.0" else None
            req_ip = req.requested_ip or ci
            if ip and (req_ip in (None, ip)) and req.giaddr == "0.0.0.0":
                # 正常 ACK
                self._log(f"DHCP 请求确认 客户端MAC={req.mac} 租约IP={ip} "
                          f"请求IP={req_ip or '(无)'} "
                          f"服务器ID={self._ip_repr(req.options.get(OPT_SERVER_ID)) or '(无)'}",
                          req.mac)
                with self._lock:
                    if req.mac in self._leases:
                        self._leases[req.mac]["state"] = "bound"
                        self._leases[req.mac]["ts"] = time.time()
                bootfile = self.config.bootfile_for_arch(req.client_arch or 0x00)
                opts = {
                    OPT_SERVER_ID: cfg.get("server_ip"),
                    OPT_LEASE_TIME: self.config.lease_time(),
                    OPT_RENEWAL: self.config.lease_time() // 2,
                    OPT_REBIND: int(self.config.lease_time() * 0.875),
                    OPT_SUBNET_MASK: cfg.get("netmask"),
                    OPT_ROUTER: cfg.get("gateway") or None,
                    OPT_DNS: cfg.get("dns") or None,
                    OPT_DOMAIN: cfg.get("domain") or None,
                    OPT_VENDOR_CLASS: req.options.get(OPT_VENDOR_CLASS),
                    OPT_IPXE_SCRIPT: ipxe_script,
                }
                reply = build_dhcp_reply(req, DHCP_ACK, yiaddr=ip, siaddr=cfg.get("server_ip"),
                                         options=opts, bootfile=bootfile)
                self.stats["acks"] += 1
                self._note_client(req, ip)
                return reply, self._dest(req, ip), req.mac
            # 冲突 → NAK（仅当请求地址属于本服务器子网；旧网段客户端的
            # REQUEST 一律静默忽略，避免干扰其他网段/服务器的 DHCP）
            in_net = True
            if req_ip:
                try:
                    in_net = ipaddress.ip_address(req_ip) in self._server_net()
                except ValueError:
                    in_net = False
            if in_net:
                self._log(f"DHCP 请求拒绝(NAK) 客户端MAC={req.mac} 请求IP={req_ip or '(无)'} "
                          f"原因=地址不可用", req.mac)
                reply = build_dhcp_reply(req, DHCP_NAK, yiaddr="0.0.0.0",
                                         options={OPT_SERVER_ID: cfg.get("server_ip"),
                                                  OPT_MESSAGE: "address not available"},
                                         bootfile=None)
                self.stats["naks"] += 1
                return reply, self._dest(req, None), req.mac
            self.stats["ignored"] += 1
            return None

        if mtype == DHCP_RELEASE:
            self._free(req.mac)
            self.stats["releases"] += 1
            return None

        if mtype == DHCP_DECLINE:
            self._free(req.mac)
            self.stats["releases"] += 1
            return None

        if mtype == DHCP_INFORM:
            opts = {
                OPT_SERVER_ID: cfg.get("server_ip"),
                OPT_SUBNET_MASK: cfg.get("netmask"),
                OPT_ROUTER: cfg.get("gateway") or None,
                OPT_DNS: cfg.get("dns") or None,
            }
            reply = build_dhcp_reply(req, DHCP_ACK, yiaddr="0.0.0.0", options=opts)
            self.stats["acks"] += 1
            return reply, self._dest(req, None), req.mac

        return None

    def _dest(self, req, ip=None):
        """按 RFC 2131 确定应答目的地址。
        ip 为分配地址时，广播应答发往该地址所在子网的定向广播（Windows 下
        255.255.255.255 会从默认路由接口发出，NAT/多网卡环境下客户端收不到）。"""
        if req.giaddr != "0.0.0.0":
            return (req.giaddr, 67)          # 中继
        if req.ciaddr != "0.0.0.0":
            return (req.ciaddr, 68)
        # ciaddr=0：客户端尚未配置 IP，单播帧无法投递（Windows PE 在 VMware
        # 下反复收不到单播 OFFER，要等 68 秒切广播），故无论 flags 都广播。
        if ip:
            try:
                mask = self.config.data.get("netmask", "255.255.255.0")
                net = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
                return (str(net.broadcast_address), 68)
            except ValueError:
                pass
        return ("255.255.255.255", 68)

    def _note_client(self, req, ip):
        info = {
            "ip": ip,
            "arch": req.client_arch,
            "vendor": req.vendor_class,
            "hostname": req.hostname,
            "last_seen": time.time(),
        }
        self.clients[req.mac] = info
        if self.on_client:
            self.on_client(req.mac, info)

    # ---------------- 网络循环 ----------------
    def start(self):
        port = self.config.dhcp_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("0.0.0.0", port))
        self.running = True
        self.log(f"DHCP 服务器已启动 (模式={self.config.dhcp_mode}, 端口 {port})")
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                continue
            try:
                res = self.dispatch(data, addr)
            except Exception as e:
                self.log(f"DHCP 处理异常: {e}")
                continue
            if res:
                reply, dest, mac = res
                try:
                    self.sock.sendto(reply, dest)
                    self._log(f"DHCP 发送响应 {dest[0]}:{dest[1]} 长度={len(reply)}", mac)
                except OSError as e:
                    self.log(f"DHCP 发送失败: {e}")

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def leases_snapshot(self):
        with self._lock:
            return [{"mac": m, "ip": v["ip"], "state": v["state"],
                     "age": int(time.time() - v["ts"])} for m, v in self._leases.items()]
