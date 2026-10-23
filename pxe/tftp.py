# -*- coding: utf-8 -*-
"""
内置 TFTP 服务器（RFC 1350 + RFC 2347/2348/2349）。

特性：
- 支持 blksize / tsize / timeout 选项协商；
- 每个传输独立线程 + 独立 UDP socket（标准 TFTP 行为）；
- 块号 65535 回绕；
- 路径穿越防护；
- 支持从“提供器”动态生成文件（如 grub.cfg / pxelinux.cfg）。
"""
import os
import socket
import struct
import threading
import time

OP_RRQ = 1
OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5
OP_OACK = 6

ERR_NOT_FOUND = 1
ERR_ACCESS = 2
ERR_ILLEGAL = 4
ERR_OPTION = 8

DEFAULT_BLKSIZE = 512
MAX_BLKSIZE = 16384
TIMEOUT = 3.0
MAX_RETRIES = 8


class TftpServer:
    def __init__(self, config, file_provider=None, log=None):
        """file_provider: callable(filename) -> bytes | None（可为动态生成文件）"""
        self.config = config
        self.file_provider = file_provider or (lambda name: None)
        self.log = log or (lambda msg: None)
        self.sock = None
        self.running = False
        self.transfers = {}   # id -> dict
        self.stats = {"requests": 0, "errors": 0, "bytes": 0}
        self._lock = threading.Lock()

    # ---------------- 文件解析 ----------------
    def _warm_boot_wim(self):
        """BOOT.WIM 启动预热到内存：706MB 大文件逐块 seek+read 在群晖 NAS 上
        会因存储 IO 抖动中断（实测 2.5MB/s、中途断）。预热后全程内存传输，
        速度回到网卡上限且不会中断。"""
        try:
            res = self._resolve("boot/BOOT.WIM")
            if res and "path" in res:
                with open(res["path"], "rb") as f:
                    self._boot_wim_cache = f.read()
                self.log("BOOT.WIM 已预热到内存 (%d MB)" % (len(self._boot_wim_cache) // 1024 // 1024))
                return True
        except Exception as e:
            self.log("BOOT.WIM 预热失败: %s" % e)
        return False

    def _resolve(self, filename):
        """解析 TFTP 路径到文件对象；防目录穿越。

        兼容 GRUB 的 (tftp)/boot/ 前缀：TFTP 根即 boot_dir，因此
        /boot/ipxe.efi 应映射为 boot_dir/ipxe.efi。
        """
        # BOOT.WIM 已预热到内存时直接内存传输（避免 NAS 存储 IO 抖动中断）
        name = filename.replace("\\", "/").lstrip("/")
        if "BOOT.WIM" in name and getattr(self, "_boot_wim_cache", None):
            return {"data": self._boot_wim_cache, "name": name}
        if ".." in name.split("/") or "\x00" in name:
            return None
        root = os.path.abspath(self.config.boot_dir)
        # ISO 文件（GRUB loopback 挂载）：iso/<相对 iso_dirs 的路径>
        if name.startswith("iso/"):
            rel = name[len("iso/"):]
            for d in self.config.iso_dirs:
                base = os.path.abspath(d)
                cand = os.path.abspath(os.path.join(base, rel))
                if cand.startswith(base + os.sep) and os.path.isfile(cand):
                    return {"path": cand, "name": name}
            return None
        # GRUB 的 /boot/ 前缀：剥离后优先动态生成、再查 boot 根
        if name.startswith("boot/"):
            sub = name[len("boot/"):]
            gen = self.file_provider(sub)
            if gen is not None:
                return {"data": gen, "name": sub}
            cand = os.path.abspath(os.path.join(root, sub))
            if cand.startswith(root + os.sep) and os.path.isfile(cand):
                return {"path": cand, "name": sub}
        # 动态生成文件优先
        gen = self.file_provider(name)
        if gen is not None:
            return {"data": gen, "name": name}
        cand = os.path.abspath(os.path.join(root, name))
        if not cand.startswith(root + os.sep) and cand != root:
            return None
        if not os.path.isfile(cand):
            return None
        return {"path": cand, "name": name}

    def _send_error(self, sock, addr, code, msg):
        pkt = struct.pack("!H", OP_ERROR) + struct.pack("!H", code) + msg.encode() + b"\x00"
        try:
            sock.sendto(pkt, addr)
        except OSError:
            pass

    # ---------------- 单个 RRQ 传输 ----------------
    def _handle_rrq(self, data, addr):
        try:
            _, rest = struct.unpack("!H", data[:2])[0], data[2:]
            parts = rest.split(b"\x00")
            filename = parts[0].decode("utf-8", "replace")
            mode = parts[1].decode("ascii", "replace") if len(parts) > 1 else "octet"
            if mode.lower() != "octet":
                self._send_error(self.sock, addr, ERR_ILLEGAL, "only octet mode supported")
                return
            opts = {}
            i = 2
            while i + 1 < len(parts):
                k = parts[i].decode("ascii", "replace").lower()
                v = parts[i + 1].decode("ascii", "replace")
                opts[k] = v
                i += 2
        except Exception:
            self._send_error(self.sock, addr, ERR_ILLEGAL, "bad request")
            return

        self.log(f"TFTP 收到文件请求 {filename!r} 模式={mode} 选项={opts} 来自 {addr[0]}:{addr[1]}")

        res = self._resolve(filename)
        if res is None:
            self._send_error(self.sock, addr, ERR_NOT_FOUND, f"file not found: {filename}")
            return
        self.stats["requests"] += 1
        self.log(f"TFTP 发送文件 {filename} 至 {addr[0]}:{addr[1]} (大小={res.get('size', '?')})")

        fh = None
        if "data" in res:
            size = len(res["data"])
            read = lambda off, n: res["data"][off:off + n]
        else:
            try:
                fh = open(res["path"], "rb")
                size = os.path.getsize(res["path"])
                if size >= 16 * 1024 * 1024:
                    # 大文件（BOOT.WIM 706MB）预读优化：1MB 块读入内存再切片发送。
                    # 逐块 seek+read 在群晖 NAS 上每块约 2.7ms（存储 IO 抖动），
                    # 17 万次 IO 中途必然超时中断；1MB 预读把磁盘 IO 降到 ~700 次，
                    # 传输速度回到网卡上限，中途卡顿概率降低两个数量级。
                    _pread = bytearray(1024 * 1024)
                    _pread_start = -1
                    def read(off, n):
                        nonlocal _pread, _pread_start
                        start = off - (off % (1024 * 1024))
                        if _pread_start != start:
                            fh.seek(start)
                            _pread = bytearray(fh.read(1024 * 1024))
                            _pread_start = start
                        rel = off - start
                        if rel >= len(_pread):
                            return b""
                        return bytes(_pread[rel:rel + n])
                else:
                    def read(off, n):
                        fh.seek(off)
                        return fh.read(n)
            except OSError:
                self._send_error(self.sock, addr, ERR_ACCESS, "cannot open file")
                return

        blksize = DEFAULT_BLKSIZE
        timeout = TIMEOUT
        # 选项协商
        ack_opts = {}
        if "blksize" in opts:
            try:
                b = int(opts["blksize"])
                # BOOT.WIM 大文件特判：16384 大块（v11 提速：块数减半）。
                # 关键约束：iPXE TFTP 单会话块数上限 ~131069（实测 4096×131069=536MB
                # 后客户端停止 ack），706MB 用 16384 时仅 43125 块，远低于上限，
                # 理论耗时减半（UDP 分片 11 片/块，UNDI 可重组，实测 16384 可行）。
                if "BOOT.WIM" in filename:
                    blksize = MAX_BLKSIZE
                else:
                    blksize = min(max(b, 8), MAX_BLKSIZE)
                ack_opts["blksize"] = str(blksize)
            except ValueError:
                pass
        if "tsize" in opts:
            ack_opts["tsize"] = str(size)
        if "timeout" in opts:
            try:
                timeout = min(max(int(opts["timeout"]), 1), 10)
                ack_opts["timeout"] = str(timeout)
            except ValueError:
                pass

        try:
            tsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tsock.settimeout(timeout)
        except OSError:
            if fh:
                fh.close()
            return

        try:
            import traceback as _tb
            try:
                self._transfer(tsock, addr, read, size, blksize, ack_opts, fh, filename)
            except Exception:
                self.log("TFTP 传输异常 %s %s: %s" % (filename, addr, _tb.format_exc()))
                _tb.print_exc()
        finally:
            try:
                tsock.close()
            except OSError:
                pass
            if fh:
                try:
                    fh.close()
                except OSError:
                    pass

    def _transfer(self, tsock, addr, read, size, blksize, ack_opts, fh, filename):
        try:
            if ack_opts:
                oack = struct.pack("!H", OP_OACK)
                for k, v in ack_opts.items():
                    oack += k.encode() + b"\x00" + v.encode() + b"\x00"
                tsock.sendto(oack, addr)
                if not self._wait_ack(tsock, addr, 0):
                    return

            block = 1
            offset = 0
            total = 0
            while True:
                chunk = read(offset, blksize)
                if not chunk:
                    break
                pkt = struct.pack("!HH", OP_DATA, block & 0xFFFF) + chunk
                # 重传机制
                sent = False
                for attempt in range(MAX_RETRIES):
                    tsock.sendto(pkt, addr)
                    try:
                        ack, aaddr = tsock.recvfrom(2048)
                    except socket.timeout:
                        continue
                    if aaddr != addr:
                        continue
                    if len(ack) >= 4 and struct.unpack("!H", ack[:2])[0] == OP_ACK:
                        ackblock = struct.unpack("!H", ack[2:4])[0]
                        if ackblock == (block & 0xFFFF):
                            sent = True
                            break
                    # 其它报文（如 ERROR）：继续等待或放弃
                    if len(ack) >= 4 and struct.unpack("!H", ack[:2])[0] == OP_ERROR:
                        break
                if not sent:
                    break
                offset += len(chunk)
                total += len(chunk)
                block += 1
                if len(chunk) < blksize:
                    break
            self.stats["bytes"] += total
            # 传输结果日志：确认大文件（BOOT.WIM）到底传了多少字节/块
            self.log("TFTP 完成 %s 至 %s 共 %d 字节 %d 块 (blksize=%d)" % (filename, addr, total, block - 1, blksize))
        finally:
            if fh:
                try:
                    fh.close()
                except OSError:
                    pass

    @staticmethod
    def _wait_ack(sock, addr, blockno, timeout=None):
        end = time.time() + (timeout or TIMEOUT)
        while time.time() < end:
            try:
                ack, aaddr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            if aaddr != addr:
                continue
            if len(ack) >= 4 and struct.unpack("!H", ack[:2])[0] == OP_ACK and \
               struct.unpack("!H", ack[2:4])[0] == blockno:
                return True
        return False

    # ---------------- 主循环 ----------------
    def start(self):
        port = self.config.tftp_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.running = True
        self._warm_boot_wim()
        self.log(f"TFTP 服务器已启动 (端口 {port})")
        while self.running:
            try:
                data, addr = self.sock.recvfrom(8192)
            except OSError:
                continue
            if len(data) < 4:
                continue
            op = struct.unpack("!H", data[:2])[0]
            if op == OP_RRQ:
                threading.Thread(target=self._handle_rrq, args=(data, addr),
                                 daemon=True).start()
            elif op == OP_WRQ:
                self._send_error(self.sock, addr, ERR_ILLEGAL, "write not supported")

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
