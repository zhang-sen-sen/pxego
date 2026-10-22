# -*- coding: utf-8 -*-
"""
VNC WebSocket 代理：浏览器 noVNC(WebSocket) <-> 客户机 VNC(TCP)。

零依赖纯标准库实现 RFC 6455 服务端帧编解码与双向桥接，
Web 管理界面内一键远程控制 PE 客户机桌面。

用法（在 httpd.py 中）：
    完成 HTTP 升级握手后，调用 run_proxy(rfile, wfile, sock, log) 进入桥接循环。
"""
import base64
import hashlib
import socket
import struct
import threading

# RFC 6455 固定 GUID，握手响应计算用
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BIN = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


def accept_key(sec_websocket_key):
    """由客户端 Sec-WebSocket-Key 计算 Sec-WebSocket-Accept。"""
    digest = hashlib.sha1(
        (sec_websocket_key + _WS_MAGIC).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _read_exact(rfile, n):
    """从缓冲读流精确读取 n 字节（HTTP 升级后剩余帧数据仍在 rfile 缓冲中）。"""
    data = b""
    while len(data) < n:
        chunk = rfile.read(n - len(data))
        if not chunk:
            raise ConnectionError("websocket stream closed")
        data += chunk
    return data


def read_frame(rfile):
    """读取一个 WebSocket 帧，返回 (opcode, payload)；payload 已去除掩码。"""
    h = _read_exact(rfile, 2)
    opcode = h[0] & 0x0F
    masked = bool(h[1] & 0x80)
    length = h[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(rfile, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(rfile, 8))[0]
    mask = _read_exact(rfile, 4) if masked else None
    payload = _read_exact(rfile, length)
    if mask:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return opcode, payload


def write_frame(wfile, opcode, payload):
    """发送一个 WebSocket 帧（服务端→客户端，不掩码），随后冲刷。"""
    header = bytes([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack("!H", n)
    else:
        header += bytes([127]) + struct.pack("!Q", n)
    wfile.write(header + payload)
    wfile.flush()


def run_proxy(rfile, wfile, tcp_sock, log=None):
    """双向桥接循环：WebSocket 帧 <-> VNC TCP 字节流。

    任一侧关闭即终止另一侧，最后关闭 TCP socket（由调用方负责 close）。
    """
    stop = threading.Event()

    def ws_to_tcp():
        try:
            while not stop.is_set():
                opcode, payload = read_frame(rfile)
                if opcode == _OP_CLOSE:
                    stop.set()
                    break
                if opcode == _OP_PING:
                    try:
                        write_frame(wfile, _OP_PONG, payload)
                    except Exception:
                        stop.set()
                        break
                    continue
                # text(1)/binary(2)/continuation(0)：字节原样转发给 VNC
                if opcode in (_OP_TEXT, _OP_BIN, _OP_CONT):
                    try:
                        tcp_sock.sendall(payload)
                    except OSError:
                        stop.set()
                        break
        except (ConnectionError, OSError):
            stop.set()
        finally:
            try:
                tcp_sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def tcp_to_ws():
        try:
            while not stop.is_set():
                data = tcp_sock.recv(65536)
                if not data:
                    stop.set()
                    break
                try:
                    write_frame(wfile, _OP_BIN, data)
                except (ConnectionError, OSError):
                    stop.set()
                    break
        except (ConnectionError, OSError):
            stop.set()
        finally:
            try:
                write_frame(wfile, _OP_CLOSE, b"")
            except Exception:
                pass

    t1 = threading.Thread(target=ws_to_tcp, daemon=True)
    t2 = threading.Thread(target=tcp_to_ws, daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    if log:
        try:
            log("VNC 会话结束")
        except Exception:
            pass
