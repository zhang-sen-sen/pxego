# -*- coding: utf-8 -*-
"""
内置 HTTP 服务器：
- Web 管理界面（静态资源 + REST API）；
- ISO 文件流式服务（支持 Range 请求，供 GRUB loopback / wimboot / memdisk 使用）；
- 动态生成的引导文件：/grub.cfg、/pxelinux.cfg/default、/ipxe.cfg；
- /boot/* 引导二进制文件。
"""
import json
import os
import re
import shutil
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import vncproxy
from .iso9660 import read_iso_index, _norm

# ISO 内部大文件（.wim/.sdi）提取缓存目录：<项目目录>\boot_files
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "boot_files")

# noVNC 静态资源 MIME 类型（web/vnc/ 目录）
_VNC_CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".map": "application/json",
    ".json": "application/json",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
}


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


class PxeHttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PXE-BootServer/1.0"

    # ---------------- 工具 ----------------
    @property
    def ctx(self):
        return self.server.ctx

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text, code=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype, allow_range=True):
        try:
            size = os.path.getsize(path)
        except OSError:
            self._error(404, "not found")
            return
        # 大文件（引导 WIM/ISO）发送前调优 socket：增大发送缓冲、禁用 Nagle，
        # 提升吞吐、缩短传输窗口，降低 iPXE 侧 Connection timed out 概率
        if size > 20 * 1024 * 1024:
            try:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                           512 * 1024)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng and allow_range:
            m = urllib.parse.parse_qs(rng, keep_blank_values=True)
            spec = rng.strip()
            if spec.startswith("bytes="):
                spec = spec[len("bytes="):]
                parts = spec.split(",")[0].strip()
                if parts == "":
                    pass
                elif parts.startswith("-"):
                    length = int(parts[1:])
                    start = max(0, size - length)
                elif "-" in parts:
                    a, b = parts.split("-", 1)
                    start = int(a or 0)
                    end = int(b) if b else size - 1
                start = min(max(start, 0), size - 1)
                end = min(max(end, start), size - 1)
                partial = True
        length = end - start + 1
        if partial:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        # GRUB 的 http 读依赖连接关闭（读完 Content-Length 后等 EOF）：
        # keep-alive 会导致 GRUB 在 loopback 挂载时永久等待而卡死。
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return
        ip = self.client_address[0] if self.client_address else "-"
        name = os.path.basename(path)
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            total = length or 1
            last_pct = -1
            while remaining > 0:
                chunk = f.read(min(1048576, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
                # 大文件（>20MB）下载进度：在原条目上刷新（同 key 覆盖），
                # 不逐条追加日志。key 带客户端 IP，多客户端同时下载互不覆盖
                if size > 20 * 1024 * 1024:
                    pct = int((length - remaining) * 100 / total)
                    if pct > last_pct:
                        last_pct = pct
                        self.ctx.log_progress(f"{ip}:{name}",
                                              f"[{ip}] 下载 {name} {pct}%")

    def _send_wimboot_padded(self, path):
        """发送 wimboot，并补齐到其 setup 头声明的 syssize（341792B）。

        wimboot v2.9.0 的 bzImage setup 头 syssize=0x5372（×16=341792），
        实际文件仅 76064B；GRUB linux 加载器按 syssize 读取，短读会卡死，
        因此服务器端补零到声明的长度。
        """
        try:
            size = os.path.getsize(path)
        except OSError:
            raise ApiError(404, "not found")
        target = max(size, 341792)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(target))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        remaining = target
        with open(path, "rb") as f:
            while remaining > 0:
                chunk = f.read(min(1048576, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
            if remaining > 0:
                self.wfile.write(b"\x00" * remaining)

    def _send_bytes(self, data, ctype="application/octet-stream"):
        """发送内存字节（无 Range 支持，用于小文件如修补后的 BCD）。"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    @staticmethod
    def patch_bcd(data):
        """修补 BCD 中的 ramdisk 路径为根路径，使 wimboot 内存盘能命中。

        wimboot 把所有 initrd --name 文件放在内存盘根目录（虚拟 FAT 只有
        \\BOOT\\ \\SOURCES\\ \\EFI\\ 等固定子目录）。而 WePE 等定制 PE 的
        BCD 内路径为 \\WEPE\\WEPE64.WIM（子目录形式），bootmgfw 按该路径
        在内存盘找不到文件，报 0xc000000f（必要设备不可访问）。

        这里把 .wim/.sdi 路径改写为根路径 \\boot.wim / \\boot.sdi，并用
        NUL 填充到与原串等长（hive 的 value 长度字段不动；bootmgfw 按
        UTF-16 字符串读取，遇到 NUL 即终止，因此尾部填充不影响解析）。
        """
        pat = re.compile(rb"(?:[\x20-\x7e]\x00)+")
        out = bytearray(data)
        for m in pat.finditer(data):
            s = m.group().decode("utf-16-le", "replace")
            if "\\" not in s:
                continue
            low = s.lower()
            target = None
            if low.endswith(".wim") and low != r"\boot.wim":
                target = r"\boot.wim"
            elif low.endswith(".sdi") and low != r"\boot.sdi":
                target = r"\boot.sdi"
            if not target:
                continue
            new = target.encode("utf-16-le")
            if len(new) > len(s) * 2:
                continue  # 原路径比目标还短，无法填充，跳过
            out[m.start():m.end()] = new + b"\x00" * (len(s) * 2 - len(new))
        return bytes(out)

    def _local_file_for(self, iso_path, inner):
        """overrides['files'] 提供 ISO 内路径 → 本地提取文件的映射。

        用于 UDF/非常规镜像（read_iso_index 无法解析）时，从 boot_files/ 读取。
        返回本地文件绝对路径；无映射或文件缺失返回 None。
        """
        try:
            ov = self.ctx.bootconf.get(os.path.basename(iso_path)) or {}
            files = ov.get("files") or {}
            rel = files.get(_norm(inner))
            if rel:
                cand = os.path.abspath(rel)
                if os.path.isfile(cand):
                    return cand
        except Exception:
            pass
        return None

    def _cached_iso_inner(self, iso_path, inner):
        """从 ISO 提取可缓存大文件（.wim/.sdi）到 boot_files/，返回本地路径。

        首次请求时从 ISO 提取写盘，后续直接发本地文件（避免每次随机寻址读 ISO）。
        缓存失效依据 ISO 的 mtime；失败时返回 None（回退原逻辑）。
        """
        try:
            base = os.path.basename(iso_path)
            cache_sub = os.path.join(_CACHE_DIR, base)
            os.makedirs(cache_sub, exist_ok=True)
            meta = os.path.join(cache_sub, "_iso_mtime.txt")
            cached = os.path.join(cache_sub, os.path.basename(inner))
            iso_mtime = os.path.getmtime(iso_path)
            if os.path.isfile(cached) and os.path.isfile(meta):
                try:
                    if abs(float(open(meta, "r").read().strip()) - iso_mtime) < 0.5:
                        return cached
                except Exception:
                    pass
            data = self._read_iso_inner_bytes(iso_path, inner)
            if not data:
                return None
            tmp = cached + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, cached)
            with open(meta, "w") as f:
                f.write(str(iso_mtime))
            return cached
        except Exception:
            return None

    def _send_mount_script(self, path, iso):
        """生成 PE 内一键挂载脚本（网络引导一键挂载）。

        用法（PE 的 CMD / shift+F10）:
          powershell -c "irm http://<server>:8080/pxe/mount.ps1 | iex"             # 列出可用 ISO
          powershell -c "irm http://<server>:8080/pxe/mount.ps1?iso=PE-v2.5.3.iso | iex" # 挂载指定 ISO
        cmd 版:  curl -o X:/m.bat http://<server>:8080/pxe/mount.bat?iso=PE-v2.5.3.iso & X:/m.bat
        """
        cfg = self.ctx.config
        server = cfg.server_ip
        port = int(cfg.http_port or 80)
        share = getattr(cfg, "mount_share", None) or "pxeboot"
        iso = iso.strip() if iso else ""
        if iso and os.path.sep in iso:
            iso = os.path.basename(iso)
        base = f"http://{server}" + (f":{port}" if port != 80 else "")
        if path.endswith(".ps1"):
            if iso:
                script = (
                    "# PXE 一键挂载\n"
                    f"$server='{server}'\n"
                    f"$share='{share}'\n"
                    f"$iso='{iso}'\n"
                    "$drv='Z'\n"
                    "net use ${drv}: /delete /y 2>$null\n"
                    "net use ${drv}: \\\\${server}\\${share} /persistent:no\n"
                    "if ($LASTEXITCODE -ne 0) { Write-Host '[PXE] 共享挂载失败' -ForegroundColor Red; exit 1 }\n"
                    "Write-Host ('[PXE] 共享已就绪: ' + ${drv} + ':')\n"
                    "$p = Join-Path (${drv} + ':') $iso\n"
                    "if (-not (Test-Path $p)) { Write-Host ('[PXE] 镜像不存在: ' + $p) -ForegroundColor Red; Get-ChildItem (${drv} + ':\\*.iso') | Select-Object -ExpandProperty Name; exit 1 }\n"
                    "try { Mount-DiskImage -ImagePath $p -ErrorAction Stop | Out-Null; Write-Host ('[PXE] 已挂载 ' + $p + ' 为光驱') -ForegroundColor Green } catch { Write-Host ('[PXE] 挂载失败: ' + $_.Exception.Message) -ForegroundColor Red }\n"
                )
            else:
                script = (
                    "# PXE 一键挂载\n"
                    f"$server='{server}'\n"
                    f"$share='{share}'\n"
                    "$drv='Z'\n"
                    "net use ${drv}: /delete /y 2>$null\n"
                    "net use ${drv}: \\\\${server}\\${share} /persistent:no\n"
                    "if ($LASTEXITCODE -ne 0) { Write-Host '[PXE] 共享挂载失败' -ForegroundColor Red; exit 1 }\n"
                    "Write-Host '[PXE] 共享已就绪，可用镜像:'\n"
                    "Get-ChildItem (${drv} + ':\\*.iso') | Select-Object -ExpandProperty Name\n"
                    "Write-Host ''\n"
                    f"Write-Host ('用法: powershell -c \"irm {base}/pxe/mount.ps1?iso=<文件名> | iex\"')\n"
                )
            ctype = "text/plain; charset=utf-8"
        else:
            # .bat 版（cmd：net use + 提示；挂载交给 PowerShell 或资源管理器右键）
            if iso:
                script = (
                    "@echo off\r\n"
                    f"set VTOY_SERVER={server}\r\n"
                    f"set VTOY_SHARE={share}\r\n"
                    f"set VTOY_ISO={iso}\r\n"
                    "net use Z: /delete /y >nul 2>nul\r\n"
                    "net use Z: \\\\%VTOY_SERVER%\\%VTOY_SHARE% /persistent:no\r\n"
                    "if errorlevel 1 goto fail\r\n"
                    "echo [PXE] 共享已就绪\r\n"
                    "powershell -NoProfile -Command \"Mount-DiskImage -ImagePath 'Z:\\%VTOY_ISO%'\"\r\n"
                    "if errorlevel 1 goto fail2\r\n"
                    "echo [PXE] 已挂载 Z:\\%VTOY_ISO% 为光驱\r\n"
                    "goto done\r\n"
                    ":fail\r\n"
                    "echo [PXE] 共享挂载失败\r\n"
                    "goto done\r\n"
                    ":fail2\r\n"
                    "echo [PXE] ISO 挂载失败，可用资源管理器右键-装载\r\n"
                    "dir /b Z:\\*.iso\r\n"
                    ":done\r\n"
                )
            else:
                script = (
                    "@echo off\r\n"
                    f"set VTOY_SERVER={server}\r\n"
                    f"set VTOY_SHARE={share}\r\n"
                    "net use Z: /delete /y >nul 2>nul\r\n"
                    "net use Z: \\\\%VTOY_SERVER%\\%VTOY_SHARE% /persistent:no\r\n"
                    "if errorlevel 1 goto fail\r\n"
                    "echo [PXE] 共享已就绪，可用镜像:\r\n"
                    "dir /b Z:\\*.iso\r\n"
                    f"echo 挂载: curl -o X:\\m.bat \"{base}/pxe/mount.bat?iso=文件名\" ^&^& X:\\m.bat\r\n"
                    "goto done\r\n"
                    ":fail\r\n"
                    "echo [PXE] 共享挂载失败\r\n"
                    ":done\r\n"
                )
            ctype = "text/plain; charset=utf-8"
        body = script.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_iso_inner_bytes(self, iso_path, inner):
        """读取 ISO 内部文件全部字节（无 Range，供 BCD 修补等场景）。"""
        local = self._local_file_for(iso_path, inner)
        if local:
            with open(local, "rb") as f:
                return f.read()
        idx = read_iso_index(iso_path)
        key = _norm(inner)
        if key not in idx:
            cand = next((k for k in idx if k.replace(";", "").split(";")[0] == key),
                        None)
            if not cand:
                raise ApiError(404, f"file not found in iso: {inner}")
            key = cand
        size = int(idx[key].get("size", 0))
        lba = idx[key].get("lba")
        if lba is None:
            raise ApiError(501, f"iso entry has no extent: {inner}")
        with open(iso_path, "rb") as f:
            f.seek(lba * 2048)
            return f.read(size)

    def _send_iso_inner(self, iso_path, inner):
        """从 ISO 内部读取文件并按 Range 输出（供 iPXE/wimboot 使用）。"""
        local = self._local_file_for(iso_path, inner)
        if not local and inner.lower().endswith((".wim", ".sdi")):
            local = self._cached_iso_inner(iso_path, inner)
        if local:
            size = os.path.getsize(local)
            start, end = 0, max(0, size - 1)
            partial = False
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                spec = rng[len("bytes="):].split(",")[0].strip()
                if spec.startswith("-"):
                    start = max(0, size - int(spec[1:]))
                elif "-" in spec:
                    a, b = spec.split("-", 1)
                    start = min(int(a or 0), max(0, size - 1))
                    end = min(int(b) if b else size - 1, max(0, size - 1))
                start = min(max(start, 0), max(0, size - 1))
                end = min(max(end, start), max(0, size - 1))
                partial = True
            length = end - start + 1
            if partial:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(local, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1048576, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return
        idx = read_iso_index(iso_path)
        key = _norm(inner)
        if key not in idx:
            # 宽松匹配：去掉版本号差异
            cand = next((k for k in idx if k.replace(";", "").split(";")[0] == key),
                        None)
            if not cand:
                raise ApiError(404, f"file not found in iso: {inner}")
            key = cand
        size = int(idx[key].get("size", 0))
        lba = idx[key].get("lba")
        if lba is None:
            raise ApiError(501, f"iso entry has no extent: {inner}")
        start, end = 0, max(0, size - 1)
        partial = False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0].strip()
            if spec.startswith("-"):
                start = max(0, size - int(spec[1:]))
            elif "-" in spec:
                a, b = spec.split("-", 1)
                start = min(int(a or 0), max(0, size - 1))
                end = min(int(b) if b else size - 1, max(0, size - 1))
            start = min(max(start, 0), max(0, size - 1))
            end = min(max(end, start), max(0, size - 1))
            partial = True
        length = end - start + 1
        if partial:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        # 同 _send_file：GRUB 依赖连接关闭判断 EOF
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(iso_path, "rb") as f:
            f.seek(lba * 2048 + start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1048576, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _error(self, code, msg):
        self._json({"error": msg}, code)

    def _log_desc(self, path):
        """把 HTTP 请求路径翻译成中文日志描述。"""
        p = path.split("?", 1)[0]
        if p in ("/", "/index.html"):
            return "Web 管理界面加载"
        if p == "/api/status":
            return "Web 界面状态查询"
        if p == "/api/isos":
            return "ISO 镜像列表查询"
        if p == "/api/bootfiles":
            return "引导文件状态查询"
        if p == "/api/config":
            return "服务器配置读取"
        if p == "/api/override":
            return "引导覆盖配置操作"
        if p == "/api/online":
            return "客户端上线通知"
        if p == "/api/vnc":
            return "客户端 VNC 状态回报"
        if p == "/api/step":
            return None   # 静默：PE 步骤回报由 client_step 统一记日志
        if p == "/api/upload":
            return "上传 ISO 镜像"
        if p.startswith("/wim/"):
            return f"引导镜像下载 (wimboot) {p[len('/wim/'):]}"
        if p.startswith("/iso/"):
            name = p[len('/iso/'):]
            # 名称取自 isos 目录真实文件（动态解析，目录改名/新增自动对应）
            real = self.ctx.resolve_iso(name) if hasattr(self, "ctx") else None
            if real:
                name = os.path.basename(real)
            return f"下载系统镜像 {name}"
        if p.startswith("/pxe/"):
            return f"PE 工具脚本下载 {p[len('/pxe/'):]}"
        if p.startswith("/boot/"):
            return f"引导文件下载 {p[len('/boot/'):]}"
        if p.startswith("/menu"):
            return "引导菜单请求"
        if p.startswith("/ipxe"):
            return "iPXE 脚本请求"
        if p.startswith("/api/"):
            return f"API 请求 {p}"
        return f"请求 {p}"

    @staticmethod
    def _step_name(p):
        """按路径归类客户端引导步骤（进行中/失败状态用）。"""
        if p.startswith("/wim/"):
            inner = p.rsplit("/", 1)[-1].lower() if "/" in p else ""
            return "下载系统镜像" if "boot.wim" in inner else "下载引导文件"
        if p.startswith("/boot/"):
            return "下载引导文件"
        if p.startswith("/iso/"):
            return "下载系统镜像"
        if p.startswith("/pxe/"):
            return "挂载虚拟光驱"
        if p in ("/ipxe.cfg", "/autoexec.ipxe", "/default.ipxe",
                 "/loader/ipxe.cfg", "/loader/autoexec.ipxe", "/loader/default.ipxe",
                 "/grub.cfg", "/pxelinux.cfg/default"):
            return "获取引导配置"
        return None

    def _track_client(self, p):
        step = self._step_name(p)
        if step:
            self.ctx.client_http_event(self.client_address[0], step)

    def _track_result(self, p, ok):
        step = self._step_name(p)
        if step:
            self.ctx.client_http_result(self.client_address[0], ok, step)

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，由状态模块统一记录

    # ---------------- 路由 ----------------
    def _handle_log_stream(self):
        """SSE 实时日志流：新日志逐条推送，前端逐条显示。"""
        ctx = self.ctx
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = ctx.subscribe_log()
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                except Exception:
                    # 15 秒无日志发心跳注释保活
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                    continue
                try:
                    self.wfile.write(("data: " + line + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    break
        finally:
            ctx.unsubscribe_log(q)

    # ---------------- VNC 远程控制（noVNC WebSocket 代理）----------------
    def _vnc_origin_ok(self):
        """反跨站 WebSocket 劫持：仅允许同源或本机来源的页面连接。"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True  # 非浏览器客户端（如脚本）无 Origin
        try:
            o = urllib.parse.urlparse(origin)
            if o.scheme not in ("http", "https"):
                return False
            netloc = o.netloc
            host = self.headers.get("Host", "")
            if netloc == host:
                return True
            host_part = netloc.rsplit(":", 1)[0] if ":" in netloc else netloc
            if host_part in ("localhost", "127.0.0.1", "::1"):
                return True
        except Exception:
            return False
        return False

    def _handle_vnc_ws(self):
        """WebSocket 升级并桥接到客户机 VNC：/vnc/ws?host=<ip>&port=<5900>。

        端口限制 5900-5999（VNC 常用段），配合 Origin 校验防止被当作
        任意 TCP 隧道滥用。会话空闲 300 秒自动断开。
        """
        import ipaddress
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        host = (qs.get("host") or [""])[0].strip()
        port_s = (qs.get("port") or [""])[0].strip() or "5900"
        try:
            port = int(port_s)
        except ValueError:
            return self._error(400, "invalid vnc port")
        if not host or not (5900 <= port <= 5999):
            return self._error(400, "invalid vnc target (host + port 5900-5999)")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return self._error(400, "invalid vnc host")
        if not self._vnc_origin_ok():
            return self._error(403, "origin not allowed")
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            return self._error(400, "missing Sec-WebSocket-Key")
        # noVNC 请求子协议 "binary"；浏览器要求服务端必须选择并回显，
        # 否则握手失败（"Sent non-empty Sec-WebSocket-Protocol header but no response"）
        subproto = self.headers.get("Sec-WebSocket-Protocol", "")
        if subproto:
            subproto = subproto.split(",")[0].strip()
        tcp = None
        try:
            tcp = socket.create_connection((host, port), timeout=8)
            tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            self.ctx.log(f"VNC 连接失败 {host}:{port} ({e})")
            return self._error(502, f"无法连接 VNC 服务 {host}:{port}")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", vncproxy.accept_key(key))
        if subproto:
            self.send_header("Sec-WebSocket-Protocol", subproto)
        self.end_headers()
        self.close_connection = True
        try:
            self.connection.settimeout(300)  # 空闲 5 分钟自动回收死会话
        except OSError:
            pass
        # VNC 会话日志用 log_progress 同 key 覆盖：进行中/结束各一条，
        # 结束后日志区只保留最新 1 条 VNC 日志（同 key 刷新，不追加堆积）
        vkey = f"vnc:{host}:{port}"
        self.ctx.log_progress(vkey, f"远程控制 {host}:{port} 会话中")
        try:
            vncproxy.run_proxy(
                self.rfile, self.wfile, tcp,
                log=lambda m: self.ctx.log_progress(
                    vkey, f"远程控制 {host}:{port} {m}"))
        finally:
            try:
                tcp.close()
            except OSError:
                pass
        return None

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/api/logs/stream":
            self._handle_log_stream()
            return
        quiet = p in ("/api/status", "/api/isos", "/api/pxe-fail", "/api/online",
                      "/api/vnc", "/api/step", "/vnc/ws")
        desc = None if quiet else self._log_desc(self.path)
        _ip = self.client_address[0] if self.client_address else "-"
        try:
            if not quiet:
                self._track_client(p)
            self._route("GET")
            if desc:
                self.ctx.log(f"[{_ip}] {desc} 成功")
                self._track_result(p, True)
        except ApiError as e:
            if desc:
                self.ctx.log(f"[{_ip}] {desc} 失败({e.code})")
                self._track_result(p, False)
            self._error(e.code, e.msg)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            if desc:
                self.ctx.log(f"[{_ip}] {desc} 失败(异常)")
                self._track_result(p, False)
            try:
                self._error(500, "internal error")
            except Exception:
                pass

    def do_HEAD(self):
        p = self.path.split("?", 1)[0]
        quiet = p in ("/api/status", "/api/isos", "/api/pxe-fail", "/api/online",
                      "/api/vnc", "/api/step", "/vnc/ws")
        desc = None if quiet else self._log_desc(self.path)
        _ip = self.client_address[0] if self.client_address else "-"
        try:
            if not quiet:
                self._track_client(p)
            self._route("HEAD")
            if desc:
                self.ctx.log(f"[{_ip}] {desc} 成功")
                self._track_result(p, True)
        except ApiError as e:
            if desc:
                self.ctx.log(f"[{_ip}] {desc} 失败({e.code})")
                self._track_result(p, False)
        except Exception:
            if desc:
                self.ctx.log(f"[{_ip}] {desc} 失败(异常)")
                self._track_result(p, False)

    def do_POST(self):
        try:
            self._route("POST")
        except ApiError as e:
            self._error(e.code, e.msg)
        except Exception as e:
            self.ctx.log(f"HTTP POST 异常 {self.path}: {e}")
            try:
                self._error(500, "internal error")
            except Exception:
                pass

    def do_PATCH(self):
        try:
            self._route("PATCH")
        except ApiError as e:
            self._error(e.code, e.msg)
        except Exception as e:
            self.ctx.log(f"HTTP PATCH 异常 {self.path}: {e}")
            try:
                self._error(500, "internal error")
            except Exception:
                pass

    def do_DELETE(self):
        try:
            self._route("DELETE")
        except ApiError as e:
            self._error(e.code, e.msg)
        except Exception:
            pass

    def _route(self, method):
        ctx = self.ctx
        path = urllib.parse.urlparse(self.path).path
        path = urllib.parse.unquote(path)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        # ---- Web 管理界面 ----
        if method in ("GET", "HEAD"):
            if path in ("/", "/index.html"):
                return self._send_file(os.path.join(ctx.web_dir, "index.html"),
                                       "text/html; charset=utf-8", allow_range=False)
            if path in ("/style.css", "/app.js", "/favicon.ico"):
                name = os.path.basename(path)
                ctype = {"style.css": "text/css; charset=utf-8",
                         "app.js": "application/javascript; charset=utf-8",
                         "favicon.ico": "image/x-icon"}.get(name, "application/octet-stream")
                return self._send_file(os.path.join(ctx.web_dir, name), ctype,
                                       allow_range=False)

        # ---- VNC 远程控制 ----
        if method == "GET" and path == "/vnc/ws":
            return self._handle_vnc_ws()

        # ---- noVNC 静态资源（web/vnc/ 目录）----
        if method in ("GET", "HEAD") and path.startswith("/vnc/"):
            rel = path[len("/vnc/"):]
            root = os.path.abspath(os.path.join(ctx.web_dir, "vnc"))
            cand = os.path.abspath(os.path.join(root, rel))
            if not cand.startswith(root + os.sep) or not os.path.isfile(cand):
                raise ApiError(404, "not found")
            ctype = _VNC_CTYPES.get(os.path.splitext(rel)[1].lower(),
                                   "application/octet-stream")
            return self._send_file(cand, ctype, allow_range=False)

        # ---- PXE 挂载脚本（PE 内一行命令执行）----
        if method in ("GET", "HEAD") and path in ("/pxe/mount.ps1", "/pxe/mount.bat"):
            iso = (qs.get("iso") or [""])[0]
            return self._send_mount_script(path, iso)

        # ---- API ----
        if path == "/api/status" and method in ("GET", "HEAD"):
            return self._json(ctx.status())

        if path == "/api/isos" and method in ("GET", "HEAD"):
            return self._json({"isos": ctx.iso_list()})

        if path == "/api/isos" and method == "POST":
            # 重新扫描
            ctx.registry.scan(force=True)
            return self._json({"ok": True, "isos": ctx.iso_list()})

        if path == "/api/upload" and method == "POST":
            return self._handle_upload()

        if path == "/api/config" and method in ("GET", "HEAD"):
            # 实时读 config.json：与窗口启动器（launcher 直接写文件）保持显示一致
            try:
                with open(ctx.config.path, "r", encoding="utf-8") as _f:
                    return self._json(json.load(_f))
            except Exception:
                return self._json(ctx.config.data)

        if path == "/api/config" and method == "PATCH":
            return self._handle_config_patch()

        if path == "/api/restart" and method == "POST":
            return self._handle_restart()

        if path == "/api/override" and method == "PATCH":
            return self._handle_override()

        if path == "/api/override" and method == "DELETE":
            name = qs.get("name", [""])[0]
            ctx.bootconf.remove(name)
            return self._json({"ok": True})

        if path == "/api/bootfiles" and method in ("GET", "HEAD"):
            return self._json(ctx.boot_status())

        # ---- PE 上线通知（客户端 ping 通服务器后由 PE 内脚本调用）----
        if path == "/api/online" and method in ("GET", "HEAD"):
            mac = (qs.get("mac") or [""])[0]
            ip = (qs.get("ip") or [""])[0]
            # proxy 模式下 DHCP 拿不到客户端 IP，直接用 HTTP 连接源 IP 兜底
            if not ip and self.client_address:
                ip = self.client_address[0]
            if mac:
                ctx.client_online(mac, ip or None)
                return self._json({"ok": True})
            raise ApiError(400, "mac required")

        # ---- PE 步骤回报（下载完成/挂载完成/解压完成）----
        if path == "/api/step" and method in ("GET", "HEAD"):
            mac = (qs.get("mac") or [""])[0]
            step = (qs.get("step") or [""])[0]
            if mac and step:
                ctx.client_step(mac, step)
                return self._json({"ok": True})
            raise ApiError(400, "mac/step required")

        # ---- PE 内 VNC 服务端状态回报（mac + port + state=online/offline）----
        if path == "/api/vnc" and method in ("GET", "HEAD"):
            mac = (qs.get("mac") or [""])[0]
            port = (qs.get("port") or [""])[0] or str(ctx.config.vnc_port)
            state = (qs.get("state") or [""])[0].lower() or "online"
            if mac:
                ctx.client_vnc(mac, port, state)
                return self._json({"ok": True})
            raise ApiError(400, "mac required")

        # ---- iPXE 引导失败回报（下载失败/超时自动重启前调用，记录日志）----
        if path == "/api/pxe-fail" and method in ("GET", "HEAD"):
            reason = (qs.get("reason") or [""])[0] or "unknown"
            _fip = self.client_address[0] if self.client_address else "-"
            ctx.log(f"[{_fip}] 客户端引导失败({reason})，自动重启重试")
            return self._text("ok")

        # ---- 引导文件 ----
        if path == "/grub.cfg":
            return self._text(ctx.generate_grub_cfg(), ctype="text/plain; charset=utf-8")
        if path == "/pxelinux.cfg/default":
            return self._text(ctx.generate_pxelinux_cfg(),
                              ctype="text/plain; charset=utf-8")
        if path == "/ipxe.cfg":
            return self._text(ctx.generate_ipxe_cfg(), ctype="text/plain; charset=utf-8")
        # iPXE 默认脚本搜索路径（HTTP 版）+ loader/ 别名
        if path in ("/autoexec.ipxe", "/default.ipxe",
                    "/loader/autoexec.ipxe", "/loader/default.ipxe", "/loader/ipxe.cfg"):
            return self._text(ctx.generate_ipxe_cfg(), ctype="text/plain; charset=utf-8")

        if path.startswith("/boot/"):
            rel = path[len("/boot/"):]
            root = os.path.abspath(ctx.boot_dir)
            cand = os.path.abspath(os.path.join(root, rel))
            if not cand.startswith(root + os.sep):
                raise ApiError(404, "not found")
            if not os.path.isfile(cand):
                raise ApiError(404, "not found")
            if os.path.basename(rel).lower() in ("bcd", "bcd.efi"):
                # boot 目录静态 BCD 发送前修补：把 .wim/.sdi 子目录路径
                # 改写为根路径 \boot.wim/\boot.sdi（NUL 等长填充），
                # 使 bootmgfw 能从 wimboot 内存盘根目录读到 WIM/SDI。
                with open(cand, "rb") as f:
                    raw = f.read()
                patched = type(self).patch_bcd(raw) if raw else raw
                return self._send_bytes(patched, "application/octet-stream")
            if os.path.basename(rel) == "wimboot":
                # GRUB 的 linux 加载器按 wimboot setup 头 syssize×16（341792B）
                # 读取内核；官方 wimboot v2.9.0 实际文件仅 76064B，短读会令
                # GRUB 卡死。补齐到 syssize（尾部补零不影响内核代码）。
                return self._send_wimboot_padded(cand)
            return self._send_file(cand, "application/octet-stream")

        if path.startswith("/iso/"):
            iso_name = path[len("/iso/"):]
            # ASCII 别名 → 真实 ISO 文件名（避免 PE 内 BAT 的 %XX 编码问题）
            ALIASES = {
                "fengniao.iso": "PE-v2.5.3.iso",
                "pe.iso": "PE-v2.5.3.iso",
            }
            iso_name = ALIASES.get(iso_name.lower(), iso_name)
            real = ctx.resolve_iso(iso_name)
            if not real:
                raise ApiError(404, "iso not found")
            return self._send_file(real, "application/octet-stream")

        if path == "/isofs":
            # ISO 内部文件直取：/isofs?iso=<相对路径>&file=<ISO 内路径>
            iso_rel = qs.get("iso", [""])[0]
            inner = qs.get("file", [""])[0]
            if not iso_rel or not inner:
                raise ApiError(400, "iso and file query params required")
            real = ctx.resolve_iso(iso_rel)
            if not real:
                raise ApiError(404, "iso not found")
            return self._send_iso_inner(real, inner)

        if path.startswith("/wim/"):
            # 从 ISO 提取文件的无查询参数路径：/wim/<iso相对路径>/<ISO内路径>
            # （GRUB 的 grub.cfg 解析器把 '&' 当保留字符，故 wimboot 的
            # initrd 文件走此路径而非 /isofs 查询参数形式）
            rest = path[len("/wim/"):]
            if "/" not in rest:
                raise ApiError(404, "not found")
            iso_rel, inner = rest.split("/", 1)
            real = ctx.resolve_iso(iso_rel)
            if not real:
                raise ApiError(404, "iso not found")
            if inner.lower().endswith("bcd"):
                # BCD 发送前做路径修补：把 \WEPE\WEPE64.WIM 等子目录路径
                # 改写为根路径 \boot.wim/\boot.sdi（NUL 等长填充），使
                # bootmgfw 能从 wimboot 内存盘根目录读到 WIM/SDI。
                raw = self._read_iso_inner_bytes(real, inner)
                patched = type(self).patch_bcd(raw) if raw else raw
                return self._send_bytes(patched, "application/octet-stream")
            return self._send_iso_inner(real, inner)

        raise ApiError(404, f"not found: {path}")

    # ---------------- 上传 ----------------
    def _handle_upload(self):
        ctx = self.ctx
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            raise ApiError(400, "empty body")
        target_dir = ctx.config.iso_dirs[0]
        os.makedirs(target_dir, exist_ok=True)

        if ctype.startswith("multipart/form-data"):
            name, path = self._save_multipart(target_dir, length)
        else:
            # 直接二进制流上传（curl --data-binary @x.iso -H "Content-Type: application/octet-stream"）
            name = self.headers.get("X-Filename", "upload.iso")
            name = os.path.basename(name)
            path = os.path.join(target_dir, name)
            with open(path, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        ctx.registry.scan(force=True)
        return self._json({"ok": True, "name": name, "path": path})

    def _save_multipart(self, target_dir, length):
        """流式 multipart/form-data 解析：提取 filename（含 RFC 5987 filename*），
        文件体按精确剩余量分块写入，避免大 ISO 一次性载入内存。"""
        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', self.headers.get("Content-Type", ""))
        boundary = (m.group(1) or m.group(2)).strip().encode() if m else None
        if not boundary:
            raise ApiError(400, "missing multipart boundary")
        delim = b"--" + boundary
        term = b"\r\n" + delim

        # 逐字节读第一个字段头（固定大小 read 会因请求体不足而挂起）
        head = b""
        while b"\r\n\r\n" not in head:
            b = self.rfile.read(1)
            if not b:
                raise ApiError(400, "invalid multipart upload")
            head += b
        head_str, body_head = head.split(b"\r\n\r\n", 1)
        hs = head_str.decode("utf-8", "replace")
        # filename="..." 优先；退而求其次 filename=裸值；支持 RFC 5987 filename*=UTF-8''xx
        fm = re.search(r'filename="([^"]*)"', hs) or re.search(r"filename=([^;\r\n]+)", hs)
        filename = fm.group(1).strip() if fm else None
        if not filename:
            raise ApiError(400, "invalid multipart upload")
        name = os.path.basename(filename)
        path = os.path.join(target_dir, name)
        keep = len(term) - 1

        with open(path, "wb") as f:
            remaining = length - len(head)
            buf = body_head
            while True:
                idx = buf.find(term)
                if idx >= 0:
                    f.write(buf[:idx])
                    break
                if remaining <= 0:
                    f.write(buf)
                    break
                if len(buf) > keep:
                    f.write(buf[:-keep])
                    buf = buf[-keep:]
                take = min(65536, remaining)
                chunk = self.rfile.read(take)
                if not chunk:
                    f.write(buf)
                    break
                buf = buf + chunk
                remaining -= len(chunk)
        return name, path

    def _handle_config_patch(self):
        ctx = self.ctx
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            patch = json.loads(self.rfile.read(length or 65536).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "invalid json")
        if not isinstance(patch, dict):
            raise ApiError(400, "invalid json")
        # 浅合并 + 深度合并 menu / mac_filter / ip_pool
        cfg = ctx.config.data
        for k, v in patch.items():
            if k in ("menu", "mac_filter", "ip_pool") and isinstance(v, dict):
                cfg.setdefault(k, {})
                cfg[k].update(v)
            else:
                cfg[k] = v
        ctx.config.save()
        return self._json({"ok": True, "config": cfg})

    def _handle_restart(self):
        """保存后重启整个服务（main.py + http_server.py 8081）。"""
        import subprocess
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        py = os.path.join(base, "runtime", "python", "python.exe")
        script = os.path.join(base, "tools", "_restart.ps1")
        ps = (
            "Start-Sleep -Seconds 2\n"
            "$pids = @()\n"
            "$pids += (Get-NetTCPConnection -LocalPort 8888 -State Listen -ErrorAction SilentlyContinue).OwningProcess\n"
            "$pids += (Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue).OwningProcess\n"
            "$pids = $pids | Where-Object { $_ } | Sort-Object -Unique\n"
            "foreach ($p in $pids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }\n"
            "Start-Sleep -Seconds 1\n"
            f'$py = "{py}"\n'
            f'$base = "{base}"\n'
            'Start-Process -FilePath $py -ArgumentList @("main.py","--no-browser","--quiet") -WorkingDirectory $base -WindowStyle Hidden '
            '-RedirectStandardOutput (Join-Path $base "tools\\service.log") -RedirectStandardError (Join-Path $base "tools\\service.err")\n'
            'Start-Process -FilePath $py -ArgumentList @("http_server.py","8081") -WorkingDirectory $base -WindowStyle Hidden '
            '-RedirectStandardOutput (Join-Path $base "tools\\rclone-svc.log") -RedirectStandardError (Join-Path $base "tools\\rclone-svc.err")\n'
        )
        try:
            with open(script, "w", encoding="utf-8-sig") as f:
                f.write(ps)
            subprocess.Popen(
                'cmd /c start "" /b powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%s"' % script,
                cwd=base,
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
        except Exception as e:
            raise ApiError(500, "restart failed: %s" % e)
        return self._json({"ok": True, "restarting": True})

    def _handle_override(self):
        ctx = self.ctx
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length or 65536).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "invalid json")
        name = body.get("name")
        override = body.get("override")
        if not name or not isinstance(override, dict):
            raise ApiError(400, "name and override required")
        ctx.bootconf.set(name, override)
        ctx.registry.scan(force=True)
        return self._json({"ok": True})


class PxeHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, ctx):
        self.ctx = ctx
        super().__init__(addr, handler)
