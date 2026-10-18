# -*- coding: utf-8 -*-
"""
PXE 网络引导服务器 —— 桌面启动器。

用法：
    python launcher.py        # 显示启动器窗口（推荐用 pythonw 无控制台运行）

功能：
    - 打开链接：若服务未运行则后台启动 main.py，然后打开浏览器访问管理页面
    - 最小化  ：收起到任务栏
    - 退出    ：停止服务进程并关闭窗口
    - 服务端口：可修改 HTTP/TFTP/DHCP 三个端口，保存后自动重启服务生效
"""
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))

# 主题色（深色 + 绿色）
BG = "#14141c"          # 窗口背景
BG2 = "#1e1e28"         # 卡片背景
FG = "#e8e8f0"          # 主文字
FG_DIM = "#9a9aae"      # 次要文字
GREEN = "#00e676"       # 主色
GREEN_DARK = "#009444"  # 按钮按下
RED = "#ff5252"         # 退出/停止
BTN_BG = "#262633"
BTN_ACTIVE = "#32323f"


def load_cfg():
    """读取 config.json，返回 (server_ip, http_port, tftp_port, dhcp_port)；不存在则用默认值。"""
    path = os.path.join(BASE, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        ip = d.get("server_ip", "192.168.1.10")
        hp = int(d.get("http_port", 80) or 80)
        tp = int(d.get("tftp_port", 69) or 69)
        dp = int(d.get("dhcp_port", 67) or 67)
        return ip, hp, tp, dp
    except Exception:
        return "192.168.1.10", 8080, 69, 67


def list_nics():
    """解析 ipconfig /all，返回 [(名称, IPv4, MAC)]，仅保留有有效 IPv4 的网卡。"""
    try:
        out = subprocess.run(["ipconfig", "/all"], capture_output=True,
                             encoding="gbk", errors="replace").stdout or ""
    except Exception:
        return []
    nics, cur = [], None
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        # 段标题顶格（无缩进）且含 adapter/适配器
        if not raw.startswith((" ", "\t")) and ("适配器" in line or "adapter" in low) and ":" in line:
            if cur and cur.get("ip"):
                nics.append(cur)
            if "适配器" in line:
                name = line.split("适配器", 1)[1].rstrip(":").strip()
            else:
                name = line.split("adapter", 1)[1].rstrip(":").strip()
            cur = {"name": name, "ip": None, "mac": None}
        elif cur is not None:
            if "ipv4" in low:
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                val = val.split("(")[0].strip()   # 去掉 (Preferred) 等后缀
                if val and val.count(".") == 3 and not val.startswith("169.254"):
                    cur["ip"] = val
            elif "物理地址" in low or "physical address" in low:
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                if val:
                    cur["mac"] = val
    if cur and cur.get("ip"):
        nics.append(cur)
    return [(n["name"], n["ip"], n["mac"] or "") for n in nics]


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.server_ip, self.http_port, self.tftp_port, self.dhcp_port = load_cfg()
        self.manage_url = f"http://{self.server_ip}:{self.http_port}/"
        self.http_var = tk.StringVar(value=str(self.http_port))
        self.tftp_var = tk.StringVar(value=str(self.tftp_port))
        self.dhcp_var = tk.StringVar(value=str(self.dhcp_port))
        self.proc = None          # 服务子进程
        self._lock = threading.Lock()

        self.title("PXE 网络引导服务器")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.nics = list_nics()
        w, h = 420, 470
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if self.nics:
            h = min(660, 470 + 30 * len(self.nics))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2 - 40}")

        self._build()
        self._check_state()
        self.after(2000, self._tick)

    # ---------------- 界面 ----------------
    def _build(self):
        # Logo（Canvas 绘制：圆 + 节点连线 + PXE 字样）
        logo = tk.Canvas(self, width=180, height=150, bg=BG, highlightthickness=0)
        logo.pack(pady=(26, 4))
        self._draw_logo(logo)

        title = tk.Label(self, text="PXE 网络引导服务器", font=("Microsoft YaHei UI", 16, "bold"),
                         fg=FG, bg=BG)
        title.pack()
        sub = tk.Label(self, text="网络引导  ·  支持 UEFI + BIOS",
                       font=("Microsoft YaHei UI", 9), fg=FG_DIM, bg=BG)
        sub.pack(pady=(2, 0))

        # 状态卡
        card = tk.Frame(self, bg=BG2, padx=18, pady=12)
        card.pack(pady=18)
        self.state_dot = tk.Label(card, text="●", font=("Segoe UI", 12), fg=RED, bg=BG2)
        self.state_dot.grid(row=0, column=0)
        self.state_text = tk.Label(card, text="服务未运行", font=("Microsoft YaHei UI", 10),
                                   fg=FG, bg=BG2)
        self.state_text.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.url_label = tk.Label(card, text=self.manage_url, font=("Consolas", 10),
                                  fg=GREEN, bg=BG2, cursor="hand2")
        self.url_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.url_label.bind("<Button-1>", lambda e: self._open())
        self.port_label = tk.Label(
            card, text=f"HTTP :{self.http_port}   TFTP :{self.tftp_port}   DHCP :{self.dhcp_port}",
            font=("Consolas", 9), fg=FG_DIM, bg=BG2)
        self.port_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))

        # 网卡选择卡
        nic_card = tk.Frame(self, bg=BG2, padx=18, pady=10)
        nic_card.pack(pady=(0, 14), fill="x", padx=24)
        tk.Label(nic_card, text="选择启动网卡（显示当前 IP）",
                 font=("Microsoft YaHei UI", 10, "bold"), fg=FG, bg=BG2
                 ).pack(anchor="w", pady=(0, 4))
        self.nic_var = tk.StringVar()
        if not self.nics:
            tk.Label(nic_card, text="未检测到有 IP 的网卡", font=("Microsoft YaHei UI", 9),
                     fg=RED, bg=BG2).pack(anchor="w")
        else:
            for name, ip, mac in self.nics:
                label = f"{ip}  ·  {name}"
                rb = tk.Radiobutton(
                    nic_card, text=label, variable=self.nic_var, value=ip,
                    font=("Microsoft YaHei UI", 9), fg=FG, bg=BG2,
                    selectcolor=BG2, activebackground=BG2, highlightthickness=0,
                    anchor="w", command=self._apply_nic)
                rb.pack(anchor="w", fill="x")
            if any(ip == self.server_ip for _, ip, _ in self.nics):
                self.nic_var.set(self.server_ip)
            else:
                self.nic_var.set(self.nics[0][1])

        # 服务端口卡（HTTP 可改；TFTP/DHCP 为 PXE 协议固定端口 69/67）
        port_card = tk.Frame(self, bg=BG2, padx=18, pady=10)
        port_card.pack(pady=(0, 12), fill="x", padx=24)
        tk.Label(port_card, text="服务端口（HTTP 可改 · TFTP/DHCP 固定）",
                 font=("Microsoft YaHei UI", 10, "bold"), fg=FG, bg=BG2
                 ).pack(anchor="w", pady=(0, 6))
        port_row = tk.Frame(port_card, bg=BG2)
        port_row.pack(anchor="w")
        for pname, pvar, locked in (("HTTP", self.http_var, False),
                                    ("TFTP", self.tftp_var, True),
                                    ("DHCP", self.dhcp_var, True)):
            tk.Label(port_row, text=f"{pname} :", font=("Consolas", 9), fg=FG, bg=BG2
                     ).pack(side="left")
            tk.Entry(port_row, textvariable=pvar, width=7,
                     font=("Consolas", 9), fg=FG, bg="#262633", insertbackground=FG,
                     relief="flat", justify="center",
                     state="disabled" if locked else "normal"
                     ).pack(side="left", padx=(2, 12))
        btn_row = tk.Frame(port_card, bg=BG2)
        btn_row.pack(anchor="e", pady=(4, 0))
        tk.Button(btn_row, text="应用端口", command=self._apply_ports, cursor="hand2",
                  font=("Microsoft YaHei UI", 9, "bold"), fg="#062b14", bg=GREEN,
                  activebackground=GREEN_DARK, activeforeground="white",
                  relief="flat", bd=0, padx=10, pady=4
                  ).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="端口检测", command=self._scan_ports, cursor="hand2",
                  font=("Microsoft YaHei UI", 9, "bold"), fg=FG, bg=BTN_BG,
                  activebackground=BTN_ACTIVE, relief="flat", bd=0, padx=10, pady=4
                  ).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="清理占用", command=self._kill_occupiers, cursor="hand2",
                  font=("Microsoft YaHei UI", 9, "bold"), fg=FG, bg=BTN_BG,
                  activebackground=BTN_ACTIVE, relief="flat", bd=0, padx=10, pady=4
                  ).pack(side="left")

        # 按钮行
        btns = tk.Frame(self, bg=BG)
        btns.pack(pady=(6, 0))
        self.btn_open = tk.Button(
            btns, text="  打开链接  ", command=self._open, cursor="hand2",
            font=("Microsoft YaHei UI", 11, "bold"), fg="#062b14", bg=GREEN,
            activebackground=GREEN_DARK, activeforeground="white",
            relief="flat", bd=0, padx=20, pady=8)
        self.btn_open.grid(row=0, column=0, padx=6)
        tk.Button(btns, text="  最小化  ", command=self.iconify, cursor="hand2",
                  font=("Microsoft YaHei UI", 10), fg=FG, bg=BTN_BG,
                  activebackground=BTN_ACTIVE, relief="flat", bd=0, padx=14, pady=8
                  ).grid(row=0, column=1, padx=6)
        tk.Button(btns, text="  退出  ", command=self._exit, cursor="hand2",
                  font=("Microsoft YaHei UI", 10), fg="white", bg="#3a2323",
                  activebackground="#552222", relief="flat", bd=0, padx=14, pady=8
                  ).grid(row=0, column=2, padx=6)

        foot = tk.Label(self, text="www.pxe-boot.local", font=("Consolas", 8),
                        fg="#555566", bg=BG)
        foot.pack(side="bottom", pady=10)

    def _draw_logo(self, cv):
        # 外环 + 内圆 + 节点 + 连线 + PXE
        cv.create_oval(40, 20, 140, 120, outline=GREEN, width=3)
        cv.create_oval(52, 32, 128, 108, outline="#3a3a4a", width=1)
        # 中心节点
        cv.create_oval(82, 62, 98, 78, fill=GREEN, outline="")
        # 三个外围节点
        for (x, y) in ((70, 40), (110, 40), (90, 100)):
            cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill=BG2, outline=GREEN, width=2)
        # 连线
        for (x, y) in ((70, 40), (110, 40), (90, 100)):
            cv.create_line(90, 70, x, y, fill="#3a8a6a", width=2)
        # 下载箭头（中间）
        cv.create_line(90, 52, 90, 78, fill=BG, width=4)
        cv.create_line(80, 70, 90, 78, fill=BG, width=4)
        cv.create_line(100, 70, 90, 78, fill=BG, width=4)
        cv.create_text(90, 112, text="PXE", font=("Consolas", 10, "bold"),
                       fill=GREEN)

    # ---------------- 服务管理 ----------------
    def _service_alive(self):
        """探测管理端点是否可访问。"""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.http_port}/api/status",
                                        timeout=1.5) as r:
                return r.status == 200
        except Exception:
            return False

    def _start_service(self):
        if self._service_alive():
            return True
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return self._service_alive()
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "main.py", "--no-browser", "--quiet"],
                    cwd=BASE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self.proc = None
                return False
        # 等待服务就绪
        for _ in range(20):
            time.sleep(0.3)
            if self._service_alive():
                return True
        return False

    def _stop_service(self):
        with self._lock:
            p = self.proc
            self.proc = None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    # ---------------- 事件 ----------------
    def _open(self):
        if not self._start_service():
            self.state_text.config(text="服务启动失败，请检查端口占用")
            self.state_dot.config(fg=RED)
            return
        self._check_state()
        webbrowser.open(self.manage_url)

    def _apply_nic(self):
        """切换启动网卡：更新 config.json 的 server_ip，若服务在运行则重启使其生效。"""
        ip = self.nic_var.get()
        if not ip or ip == self.server_ip:
            return
        path = os.path.join(BASE, "config.json")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d["server_ip"] = ip
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        self.server_ip = ip
        self.manage_url = f"http://{self.server_ip}:{self.http_port}/"
        self.url_label.config(text=self.manage_url)
        was_running = self._service_alive()
        if was_running:
            self._stop_service()
            self._start_service()
        self._check_state()
        self.state_text.config(
            text=f"已切换网卡 {ip}" + ("，服务已重启生效" if was_running else "，点击打开链接启动服务"))

    def _apply_ports(self):
        """应用 HTTP 端口：校验 → 写回 config.json（TFTP/DHCP 固定 69/67）→ 重启服务。"""
        try:
            hp = int(self.http_var.get().strip())
            if not (1 <= hp <= 65535):
                raise ValueError
        except ValueError:
            self.state_text.config(text="HTTP 端口须为 1-65535 的整数")
            return
        path = os.path.join(BASE, "config.json")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d["http_port"] = hp
        d["tftp_port"] = 69
        d["dhcp_port"] = 67
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        self.http_port, self.tftp_port, self.dhcp_port = hp, 69, 67
        self.manage_url = f"http://{self.server_ip}:{self.http_port}/"
        self.url_label.config(text=self.manage_url)
        self.port_label.config(
            text=f"HTTP :{self.http_port}   TFTP :{self.tftp_port}   DHCP :{self.dhcp_port}")
        was_running = self._service_alive()
        if was_running:
            self._stop_service()
            self._start_service()
        self._check_state()
        self.state_text.config(
            text=f"HTTP 端口已更新为 {hp}" +
                 ("，服务已重启生效" if was_running else "，点击打开链接启动服务"))

    # ---------------- 端口占用检测 / 清理 ----------------
    def _port_pids(self, proto, port):
        """返回占用指定端口的 PID 列表（netstat 解析）。"""
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 encoding="gbk", errors="replace").stdout or ""
        except Exception:
            return []
        pids = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0].upper() != proto:
                continue
            local = parts[1]
            if not local.endswith(f":{port}"):
                continue
            if proto == "TCP" and "LISTENING" not in line.upper():
                continue
            pid = parts[-1]
            if pid.isdigit() and int(pid) not in pids:
                pids.append(int(pid))
        return pids

    def _proc_name(self, pid):
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, encoding="gbk", errors="replace").stdout or ""
            if "," in out:
                return out.split(",")[0].strip('"')
        except Exception:
            pass
        return str(pid)

    def _is_svc_process(self, pid):
        """判断 pid 是否为本服务进程（main.py / http_server.py 后端 8081）。"""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5).stdout or ""
            return ("main.py" in out) or ("http_server.py" in out)
        except Exception:
            return False

    def _scan_ports(self):
        import tkinter.messagebox as mb
        lines = []
        occupiers = []
        svc_pid = getattr(self.proc, "pid", None)
        for proto, port, name in (("UDP", 67, "DHCP"),
                                  ("UDP", 69, "TFTP"),
                                  ("TCP", self.http_port, "HTTP"),
                                  ("TCP", 8081, "RCLONE后端(8081)")):
            pids = self._port_pids(proto, port)
            if not pids:
                lines.append(f"{name}（{proto} {port}）：空闲")
            else:
                desc = []
                for p in pids:
                    if p == svc_pid or self._is_svc_process(p):
                        desc.append(f"{p}（本服务）")
                    else:
                        desc.append(f"{p}（{self._proc_name(p)}）")
                        occupiers.append(p)
                lines.append(f"{name}（{proto} {port}）：被 {'、'.join(desc)} 占用")
        self._occupiers = list(dict.fromkeys(occupiers))
        mb.showinfo("端口占用检测", "\n".join(lines))

    def _kill_occupiers(self):
        import tkinter.messagebox as mb
        pids = getattr(self, "_occupiers", []) or []
        if not pids:
            mb.showinfo("清理占用", "没有检测到需要清理的占用进程。\n请先点“端口检测”。")
            return
        names = "\n".join(f"{p}（{self._proc_name(p)}）" for p in pids)
        if not mb.askyesno("确认清理", f"将强制停止以下占用进程：\n{names}\n\n确定？"):
            return
        for p in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(p)],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
        self._occupiers = []
        self.state_text.config(text="已清理占用进程，请点击打开链接重启服务")

    def _exit(self):
        self._stop_service()
        self.destroy()

    def _check_state(self):
        alive = self._service_alive()
        if alive:
            self.state_dot.config(fg=GREEN)
            self.state_text.config(text="服务运行中（HTTP/TFTP/DHCP）")
            self.btn_open.config(text="  打开链接  ")
        else:
            self.state_dot.config(fg=RED)
            self.state_text.config(text="服务未运行")
            self.btn_open.config(text="  启动并打开  ")

    def _tick(self):
        self._check_state()
        self.after(2000, self._tick)


if __name__ == "__main__":
    Launcher().mainloop()
