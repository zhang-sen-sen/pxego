# -*- coding: utf-8 -*-
# PXE HTTP 只读文件服务器 v2 - 完整 Range 支持（206 部分响应），为 rclone 随机读取优化
# 特性: Range / 高并发 / 无日志 / 大文件流式
# 用法: python http_server.py [端口]  默认 8081
import http.server
import socketserver
import os
import sys
import re
import mimetypes

BASE = os.path.dirname(os.path.abspath(__file__))
ISO_DIR = os.path.join(BASE, "isos")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081


class RangeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _translate(self, path):
        # 防目录穿越
        import urllib.parse
        p = urllib.parse.unquote(path.split("?", 1)[0])
        p = os.path.normpath(os.path.join(ISO_DIR, p.lstrip("/")))
        if not p.startswith(os.path.normpath(ISO_DIR)):
            return None
        return p

    def _list_dir(self, path):
        # 目录列表格式与 Python SimpleHTTPRequestHandler 一致（rclone http 后端可解析出文件大小）
        import time as _time
        entries = sorted(os.listdir(path))
        items = []
        for name in entries:
            full = os.path.join(path, name)
            display = name + ("/" if os.path.isdir(full) else "")
            if os.path.isdir(full):
                # 目录 href 必须带结尾斜杠，rclone http 后端才识别为目录
                items.append('<li><a href="%s/">%s/</a></li>' % (name, name))
            else:
                st = os.stat(full)
                mtime = _time.strftime("%d-%b-%Y %H:%M", _time.localtime(st.st_mtime))
                items.append('<li><a href="%s">%s</a> %s %d</li>' % (name, display, mtime, st.st_size))
        body = ("<!DOCTYPE html><html><head><title>Directory listing for /</title></head>"
                "<body><h1>Directory listing for /</h1><hr><ul>%s</ul><hr></body></html>"
                % "".join(items)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(404, "Not Found")
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        content_range = None
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                rs, re_ = m.group(1), m.group(2)
                try:
                    if rs == "" and re_ != "":
                        n = int(re_)
                        start = max(0, size - n)
                        end = size - 1
                    else:
                        start = int(rs) if rs else 0
                        end = int(re_) if re_ else size - 1
                        if end >= size:
                            end = size - 1
                except ValueError:
                    self.send_error(400, "Bad Request")
                    return
                if start > end or start >= size:
                    self.send_error(416, "Range Not Satisfiable")
                    self.send_header("Content-Range", "bytes */%d" % size)
                    return
                status = 206
                content_range = "bytes %d-%d/%d" % (start, end, size)
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self):
        if self.path == "/api/size":
            # 返回 isos 目录全部文件的实际总字节数（供 rclone 显示真实容量）
            try:
                total = sum(
                    os.path.getsize(os.path.join(ISO_DIR, f))
                    for f in os.listdir(ISO_DIR)
                    if os.path.isfile(os.path.join(ISO_DIR, f))
                )
                body = str(total).encode("ascii")
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.send_error(500)
            return
        if self.path == "/api/server":
            # 返回主服务 HTTP 端口（PE 内回报/上线通知用），格式 http_port=<port>
            try:
                import json as _json
                with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
                    _d = _json.load(f)
                _hp = int(_d.get("http_port", 8080) or 8080)
            except Exception:
                _hp = 8080
            body = ("http_port=%d" % _hp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        p = self._translate(self.path)
        if p is None:
            self.send_error(403, "Forbidden")
            return
        if os.path.isdir(p):
            self._list_dir(p)
        elif os.path.isfile(p):
            self._send_file(p)
        else:
            self.send_error(404, "Not Found")

    def do_HEAD(self):
        p = self._translate(self.path)
        if p is None:
            self.send_error(403)
            return
        if os.path.isfile(p):
            size = os.path.getsize(p)
            self.send_response(200)
            self.send_header("Content-type", mimetypes.guess_type(p)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
        else:
            self.send_error(404)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _udp_discover_server():
    """UDP 广播发现应答：PE 端广播 PXEDISCOVER，回复 PXESERVER=<server_ip>
    服务器 IP 取自 config.json 的 server_ip，换电脑改配置即可，无需动 PE
    UDP 发现与 TCP 文件服务共用 8081 端口（两协议独立，互不冲突）"""
    import socket
    try:
        import json as _json
        with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
            _d = _json.load(f)
        _ip = str(_d.get("server_ip", "") or "").strip()
    except Exception:
        _ip = ""
    if not _ip:
        return
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", PORT))
    except OSError:
        return
    while True:
        try:
            data, addr = s.recvfrom(1024)
            if b"PXEDISCOVER" in data:
                s.sendto(("PXESERVER=" + _ip).encode("ascii"), addr)
        except Exception:
            pass


def _start_udp_discover():
    import threading
    threading.Thread(target=_udp_discover_server, daemon=True).start()


if __name__ == "__main__":
    if not os.path.isdir(ISO_DIR):
        print("isos 目录不存在: %s" % ISO_DIR, flush=True)
        sys.exit(1)
    _start_udp_discover()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), RangeHandler)
    print("HTTP 服务器 v2 已启动: 0.0.0.0:%d  根目录: %s  (UDP 发现共用 %d 已启用)" % (PORT, ISO_DIR, PORT), flush=True)
    server.serve_forever()
