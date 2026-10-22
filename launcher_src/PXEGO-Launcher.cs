// PXEGO-Launcher.cs —— PXEGo 桌面启动器（WinForms，.NET 3.5 兼容，Win7 原生可运行）
// 编译（.NET 3.5，C# 3.0 语法）：
//   "C:\Windows\Microsoft.NET\Framework\v3.5\csc.exe" /nologo /target:winexe /optimize+
//     /out:PXEGO-Launcher.exe /win32manifest:app.manifest
//     /reference:System.Web.Extensions.dll
//     PXEGO-Launcher.cs
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;

namespace PXEGO {
    public class NicInfo {
        public string Name;
        public string Ip;
        public string Mac;
    }

    public class MainForm : Form {
        // 控件
        Label lblDot, lblState, lblUrl, lblPorts;
        ComboBox nicBox, dhcpModeBox;
        TextBox txtHttp;
        Panel nicPanel, portPanel;
        Button btnOpen, btnMin, btnExit, btnPorts, btnStop, btnScan, btnKill;

        // 状态
        string baseDir, cfgPath, pythonExe, manageUrl;
        int httpPort = 8080, tftpPort = 69, dhcpPort = 67;
        string serverIp = "192.168.1.10", dhcpMode = "dhcp";
        Dictionary<string, string> nicIpMap = new Dictionary<string, string>();
        List<NicInfo> nics = new List<NicInfo>();
        List<int> occupiers = new List<int>();
        Process svcProc, rcloneProc;
        bool nicApplying = false;

        // 颜色
        static Color BG = Color.FromArgb(20, 20, 28);
        static Color BG2 = Color.FromArgb(30, 30, 40);
        static Color FG = Color.FromArgb(232, 232, 240);
        static Color DIM = Color.FromArgb(154, 154, 174);
        static Color GREEN = Color.FromArgb(0, 230, 118);
        static Color RED = Color.FromArgb(255, 82, 82);
        static Color BTN = Color.FromArgb(38, 38, 51);
        static Color HOVER = Color.FromArgb(49, 49, 66);
        static Color BORDER = Color.FromArgb(58, 58, 74);

        public MainForm() {
            baseDir = AppDomain.CurrentDomain.BaseDirectory;
            cfgPath = Path.Combine(baseDir, "config.json");
            pythonExe = Path.Combine(baseDir, @"runtime\python\python.exe");
            if (!File.Exists(pythonExe)) { pythonExe = "python"; }
            LoadConfig();
            manageUrl = "http://" + serverIp + ":" + httpPort + "/";
            BuildUi();
            EnumerateNics();
            UpdateState();
            Timer t = new Timer();
            t.Interval = 3000;
            t.Tick += delegate { UpdateState(); };
            t.Start();
        }

        // ---------------- 配置（正则读写，零依赖，.NET 3.5 可编译） ----------------
        string GetJsonStr(string text, string key) {
            Match m = Regex.Match(text, "\"" + key + "\"\\s*:\\s*\"([^\"]*)\"");
            if (m.Success) return m.Groups[1].Value;
            Match m2 = Regex.Match(text, "\"" + key + "\"\\s*:\\s*([^,}\r\n\\s]+)");
            if (m2.Success) return m2.Groups[1].Value;
            return null;
        }
        string SetJsonStr(string text, string key, string value) {
            string pat = "\"" + key + "\"\\s*:\\s*\"[^\"]*\"";
            string rep = "\"" + key + "\": \"" + value + "\"";
            return Regex.IsMatch(text, pat) ? Regex.Replace(text, pat, rep) : text;
        }
        string SetJsonNum(string text, string key, int value) {
            string pat = "\"" + key + "\"\\s*:\\s*[^,}\r\n\\s]+";
            string rep = "\"" + key + "\": " + value;
            return Regex.IsMatch(text, pat) ? Regex.Replace(text, pat, rep) : text;
        }
        string SetJsonObj(string text, string key, string innerJson) {
            string pat = "\"" + key + "\"\\s*:\\s*\\{[^}]*\\}";
            string rep = "\"" + key + "\": { " + innerJson + " }";
            return Regex.IsMatch(text, pat) ? Regex.Replace(text, pat, rep) : text;
        }

        void LoadConfig() {
            try {
                if (File.Exists(cfgPath)) {
                    string t = File.ReadAllText(cfgPath, Encoding.UTF8);
                    string v;
                    if ((v = GetJsonStr(t, "server_ip")) != null) serverIp = v;
                    if ((v = GetJsonStr(t, "http_port")) != null) { int x; if (int.TryParse(v, out x)) httpPort = x; }
                    if ((v = GetJsonStr(t, "tftp_port")) != null) { int x; if (int.TryParse(v, out x)) tftpPort = x; }
                    if ((v = GetJsonStr(t, "dhcp_port")) != null) { int x; if (int.TryParse(v, out x)) dhcpPort = x; }
                    if ((v = GetJsonStr(t, "dhcp_mode")) != null) dhcpMode = v;
                }
            } catch { }
        }

        // ---------------- 网卡 ----------------
        void EnumerateNics() {
            nics.Clear();
            nicIpMap.Clear();
            try {
                foreach (NetworkInterface ni in NetworkInterface.GetAllNetworkInterfaces()) {
                    if (ni.OperationalStatus != OperationalStatus.Up) continue;
                    if (ni.NetworkInterfaceType == NetworkInterfaceType.Loopback) continue;
                    IPInterfaceProperties ip = ni.GetIPProperties();
                    foreach (UnicastIPAddressInformation u in ip.UnicastAddresses) {
                        if (u.Address.AddressFamily == AddressFamily.InterNetwork) {
                            string ipStr = u.Address.ToString();
                            if (ipStr.StartsWith("169.254")) continue;
                            string mac = "";
                            try { mac = ni.GetPhysicalAddress().ToString(); } catch { }
                            if (mac.Length == 12) {
                                StringBuilder sb = new StringBuilder();
                                for (int i = 0; i < 6; i++) {
                                    if (i > 0) sb.Append(':');
                                    sb.Append(mac.Substring(i * 2, 2));
                                }
                                mac = sb.ToString();
                            }
                            nics.Add(new NicInfo { Name = ni.Description, Ip = ipStr, Mac = mac });
                        }
                    }
                }
            } catch { }
            nicBox.Items.Clear();
            foreach (NicInfo n in nics) {
                string shortName = n.Name;
                if (shortName.Length > 32) shortName = shortName.Substring(0, 32);
                string item = n.Ip + "  ·  " + shortName;
                nicBox.Items.Add(item);
                nicIpMap[item] = n.Ip;
            }
            if (nics.Count > 0) {
                nicApplying = true;
                int idx = 0;
                for (int i = 0; i < nicBox.Items.Count; i++) {
                    if (nicIpMap[(string)nicBox.Items[i]] == serverIp) { idx = i; break; }
                }
                nicBox.SelectedIndex = idx;
                nicApplying = false;
            }
        }

        // ---------------- 端口枚举（netstat） ----------------
        List<int> GetListenPids(string proto, int port) {
            List<int> pids = new List<int>();
            try {
                Process p = new Process();
                p.StartInfo.FileName = "netstat";
                p.StartInfo.Arguments = "-ano";
                p.StartInfo.UseShellExecute = false;
                p.StartInfo.RedirectStandardOutput = true;
                p.StartInfo.CreateNoWindow = true;
                p.Start();
                string outStr = p.StandardOutput.ReadToEnd();
                p.WaitForExit();
                string[] lines = outStr.Split('\n');
                foreach (string raw in lines) {
                    string line = raw.TrimEnd('\r');
                    if (proto == "TCP") {
                        if (line.IndexOf("LISTENING") >= 0 && Regex.IsMatch(line, ":" + port + @"\s")) {
                            Match m = Regex.Match(line, @"(\d+)\s*$");
                            if (m.Success) { int pid; if (int.TryParse(m.Groups[1].Value, out pid)) pids.Add(pid); }
                        }
                    } else {
                        if (Regex.IsMatch(line, @"^\s*UDP\s+[^\s]+:" + port + @"\s")) {
                            Match m = Regex.Match(line, @"(\d+)\s*$");
                            if (m.Success) { int pid; if (int.TryParse(m.Groups[1].Value, out pid)) pids.Add(pid); }
                        }
                    }
                }
            } catch { }
            List<int> uniq = new List<int>();
            foreach (int pid in pids) { if (!uniq.Contains(pid)) uniq.Add(pid); }
            return uniq;
        }

        // ---------------- 服务 ----------------
        bool TestService() {
            try {
                TcpClient c = new TcpClient();
                IAsyncResult ar = c.BeginConnect("127.0.0.1", httpPort, null, null);
                bool ok = ar.AsyncWaitHandle.WaitOne(500);
                if (ok) c.EndConnect(ar);
                c.Close();
                return ok;
            } catch { return false; }
        }

        void StartPythonProc(string args, string log, ref Process holder) {
            try {
                Directory.CreateDirectory(Path.Combine(baseDir, "tools"));
                ProcessStartInfo psi = new ProcessStartInfo();
                psi.FileName = pythonExe;
                psi.Arguments = args;
                psi.WorkingDirectory = baseDir;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                Process p = Process.Start(psi);
                holder = p;
                StreamWriter w = new StreamWriter(Path.Combine(baseDir, Path.Combine("tools", log)), false, Encoding.UTF8);
                p.OutputDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) { lock (w) w.WriteLine(e.Data); } };
                p.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) { lock (w) w.WriteLine(e.Data); } };
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
            } catch { }
        }

        bool StartService() {
            if (TestService()) {
                // 已运行：确保 8081 后端
                if (GetListenPids("TCP", 8081).Count == 0) {
                    StartPythonProc("http_server.py 8081", "rclone-svc.log", ref rcloneProc);
                }
                return true;
            }
            if (!File.Exists(pythonExe) && pythonExe == "python") { SetState("未找到可用的 Python，无法启动服务", RED); return false; }
            SetState("正在启动服务...", DIM);
            try { File.Delete(Path.Combine(baseDir, Path.Combine("tools", "service.log"))); } catch { }
            try { File.Delete(Path.Combine(baseDir, Path.Combine("tools", "service.err"))); } catch { }
            StartPythonProc("main.py --no-browser --quiet", "service.log", ref svcProc);
            StartPythonProc("http_server.py 8081", "rclone-svc.log", ref rcloneProc);
            for (int i = 0; i < 30; i++) {
                System.Threading.Thread.Sleep(500);
                if (TestService()) { UpdateState(); return true; }
            }
            SetState("服务启动超时（未在 15 秒内就绪）", RED);
            return false;
        }

        void StopService() {
            try { if (svcProc != null) { svcProc.Kill(); svcProc = null; } } catch { }
            try { if (rcloneProc != null) { rcloneProc.Kill(); rcloneProc = null; } } catch { }
        }

        void StopProject() {
            List<int> pids = new List<int>();
            pids.AddRange(GetListenPids("UDP", 67));
            pids.AddRange(GetListenPids("UDP", 69));
            pids.AddRange(GetListenPids("TCP", httpPort));
            pids.AddRange(GetListenPids("TCP", 8081));
            int killed = 0;
            foreach (int id in pids) {
                if (id == 0) continue;
                try { Process.GetProcessById(id).Kill(); killed++; } catch { }
            }
            svcProc = null; rcloneProc = null;
            UpdateState();
            SetState(killed > 0 ? "服务已停止" : "服务未运行", RED);
        }

        void StopPortUsers() {
            List<int> pids = GetListenPids("TCP", httpPort);
            foreach (int id in pids) { try { Process.GetProcessById(id).Kill(); } catch { } }
        }

        // ---------------- 网卡切换 / 端口应用 ----------------
        string AddIpInt(string ipStr, int n) {
            byte[] b = IPAddress.Parse(ipStr).GetAddressBytes();
            Array.Reverse(b);
            uint v = BitConverter.ToUInt32(b, 0) + (uint)n;
            byte[] b2 = BitConverter.GetBytes(v);
            Array.Reverse(b2);
            return new IPAddress(b2).ToString();
        }

        string[] GetSubnetPool(string ip, string maskStr) {
            byte[] ipb = IPAddress.Parse(ip).GetAddressBytes();
            byte[] mkb = IPAddress.Parse(maskStr).GetAddressBytes();
            byte[] netb = new byte[4];
            for (int i = 0; i < 4; i++) netb[i] = (byte)(ipb[i] & mkb[i]);
            string netIp = new IPAddress(netb).ToString();
            int prefix = 0;
            foreach (byte b in mkb) { for (int i = 7; i >= 0; i--) { if (((b >> i) & 1) == 1) prefix++; } }
            double hosts = Math.Pow(2, 32 - prefix) - 2;
            int startN = (int)Math.Min(100, hosts - 2);
            int endN = (int)Math.Min(200, hosts - 2);
            if (endN < startN) { startN = 10; endN = (int)Math.Min(200, hosts - 2); if (endN < startN) { startN = 2; endN = (int)hosts; } }
            return new string[] { AddIpInt(netIp, startN), AddIpInt(netIp, endN) };
        }

        void SetNic(string ip) {
            if (string.IsNullOrEmpty(ip) || ip == serverIp) return;
            string text = "";
            try { text = File.ReadAllText(cfgPath, Encoding.UTF8); } catch { return; }
            if (text.Length == 0) return;
            text = SetJsonStr(text, "server_ip", ip);
            string maskStr = "255.255.255.0";
            string mv = GetJsonStr(text, "netmask");
            if (mv != null && mv.Length > 0) maskStr = mv;
            string[] pool = GetSubnetPool(ip, maskStr);
            text = SetJsonObj(text, "ip_pool", "\"start\": \"" + pool[0] + "\", \"end\": \"" + pool[1] + "\"");
            text = SetJsonStr(text, "gateway", "");
            try { File.WriteAllText(cfgPath, text, new UTF8Encoding(false)); } catch { }
            serverIp = ip;
            manageUrl = "http://" + serverIp + ":" + httpPort + "/";
            lblUrl.Text = manageUrl;
            SetState("已切换网卡 " + ip + "，地址池 " + pool[0] + "-" + pool[1] + "（点启动并打开）", DIM);
        }

        void ApplyPorts() {
            int hp;
            if (!int.TryParse(txtHttp.Text.Trim(), out hp) || hp < 1 || hp > 65535) {
                SetState("HTTP 端口须为 1-65535 的整数", RED);
                return;
            }
            string sel = (string)dhcpModeBox.SelectedItem;
            string mode = dhcpMode;
            if (sel != null && dhcpModeMap.ContainsKey(sel)) mode = dhcpModeMap[sel];
            bool modeChanged = (mode != dhcpMode);
            bool portChanged = (hp != httpPort);
            string text = "";
            try { text = File.ReadAllText(cfgPath, Encoding.UTF8); } catch { }
            if (text.Length == 0) return;
            text = SetJsonNum(text, "http_port", hp);
            text = SetJsonNum(text, "tftp_port", 69);
            text = SetJsonNum(text, "dhcp_port", 67);
            if (modeChanged) text = SetJsonStr(text, "dhcp_mode", mode);
            try { File.WriteAllText(cfgPath, text, new UTF8Encoding(false)); } catch { }
            httpPort = hp; tftpPort = 69; dhcpPort = 67;
            if (modeChanged) dhcpMode = mode;
            manageUrl = "http://" + serverIp + ":" + httpPort + "/";
            lblUrl.Text = manageUrl;
            string msg = "HTTP 端口已更新为 " + hp;
            if (modeChanged) msg += "，DHCP 模式已切换为 " + sel;
            if (TestService() && (portChanged || modeChanged)) {
                SetState(msg + "，正在重启服务...", DIM);
                StopProject();
                System.Threading.Thread.Sleep(500);
                if (StartService()) { SetState(msg + "，服务已重启生效", GREEN); }
                else { SetState(msg + "，服务重启失败", RED); }
            } else if (TestService()) {
                SetState(msg + "（服务运行中，重启后生效）", DIM);
            } else {
                SetState(msg + "（点启动并打开）", DIM);
            }
        }

        // ---------------- 端口检测 / 清理 ----------------
        void ScanPorts() {
            occupiers.Clear();
            List<string> lines = new List<string>();
            string[][] checks = new string[][] {
                new string[] { "UDP", "67", "DHCP" },
                new string[] { "UDP", "69", "TFTP" },
                new string[] { "TCP", httpPort.ToString(), "HTTP" },
                new string[] { "TCP", "8081", "RCLONE" }
            };
            foreach (string[] c in checks) {
                List<int> pids = GetListenPids(c[0], int.Parse(c[1]));
                if (pids.Count == 0) {
                    lines.Add(c[2] + "（" + c[0] + " " + c[1] + "）：空闲");
                } else {
                    List<string> desc = new List<string>();
                    foreach (int pid in pids) {
                        try {
                            string nm = Process.GetProcessById(pid).ProcessName;
                            desc.Add(pid + "（" + nm + "）");
                            occupiers.Add(pid);
                        } catch { desc.Add(pid + "（未知）"); }
                    }
                    lines.Add(c[2] + "（" + c[0] + " " + c[1] + "）：被 " + string.Join("、", desc.ToArray()) + " 占用");
                }
            }
            MessageBox.Show(string.Join("\n", lines.ToArray()), "端口占用检测", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        void KillOccupiers() {
            if (occupiers.Count == 0) {
                MessageBox.Show("没有检测到需要清理的占用进程。\n请先点端口检测。", "清理占用", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            List<string> names = new List<string>();
            foreach (int pid in occupiers) {
                try { names.Add(pid + "（" + Process.GetProcessById(pid).ProcessName + "）"); }
                catch { names.Add(pid + "（已退出）"); }
            }
            if (MessageBox.Show("将强制停止以下占用进程：\n" + string.Join("\n", names.ToArray()) + "\n\n确定？", "确认清理",
                MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
            foreach (int pid in occupiers) { try { Process.GetProcessById(pid).Kill(); } catch { } }
            occupiers.Clear();
            SetState("已清理占用进程，请点打开链接重启服务", DIM);
        }

        // ---------------- UI 状态 ----------------
        void SetState(string text, Color color) {
            lblState.Text = text;
            lblDot.ForeColor = color;
        }

        void UpdateState() {
            bool running = TestService();
            if (running) {
                lblDot.ForeColor = GREEN;
                lblState.Text = "服务运行中：HTTP/TFTP/DHCP";
                lblUrl.Text = manageUrl;
                lblUrl.Visible = true;
            } else {
                lblDot.ForeColor = RED;
                lblState.Text = "服务未运行";
                lblUrl.Visible = false;
            }
            if (nicBox != null) nicBox.Enabled = !running;
            if (dhcpModeBox != null) dhcpModeBox.Enabled = !running;
        }

        // ---------------- UI ----------------
        Dictionary<string, string> dhcpModeMap = new Dictionary<string, string>();
        int btnY, footY;

        void BuildUi() {
            Text = "PXE 网络引导服务器";
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedSingle;
            ClientSize = new Size(600, 620);
            BackColor = BG;
            Font = new Font("Microsoft YaHei UI", 9);
            MaximizeBox = false;

            // 标题
            Label title = new Label();
            title.Text = "PXE 网络引导服务器";
            title.Font = new Font("Microsoft YaHei UI", 18, FontStyle.Bold);
            title.ForeColor = FG;
            title.BackColor = BG;
            title.Size = new Size(600, 44);
            title.Location = new Point(0, 20);
            title.TextAlign = ContentAlignment.MiddleCenter;
            Controls.Add(title);

            Label sub = new Label();
            sub.Text = "网络引导  ·  支持 UEFI + BIOS";
            sub.Font = new Font("Microsoft YaHei UI", 10);
            sub.ForeColor = DIM;
            sub.BackColor = BG;
            sub.Size = new Size(600, 28);
            sub.Location = new Point(0, 68);
            sub.TextAlign = ContentAlignment.MiddleCenter;
            Controls.Add(sub);

            // 状态卡
            Panel card = new Panel();
            card.Size = new Size(440, 110);
            card.Location = new Point(80, 104);
            card.BackColor = BG2;
            card.Paint += delegate(object s, PaintEventArgs e) {
                e.Graphics.DrawRectangle(new Pen(BORDER), 0, 0, card.Width - 1, card.Height - 1);
            };
            Controls.Add(card);

            lblDot = new Label();
            lblDot.Text = "●";
            lblDot.Font = new Font("Segoe UI", 14);
            lblDot.ForeColor = RED;
            lblDot.BackColor = BG2;
            lblDot.Size = new Size(24, 26);
            lblDot.Location = new Point(20, 14);
            card.Controls.Add(lblDot);

            lblState = new Label();
            lblState.Text = "服务未运行";
            lblState.Font = new Font("Microsoft YaHei UI", 11, FontStyle.Bold);
            lblState.ForeColor = FG;
            lblState.BackColor = BG2;
            lblState.Size = new Size(400, 26);
            lblState.Location = new Point(56, 14);
            card.Controls.Add(lblState);

            lblUrl = new Label();
            lblUrl.Text = manageUrl;
            lblUrl.Font = new Font("Consolas", 11);
            lblUrl.ForeColor = GREEN;
            lblUrl.BackColor = BG2;
            lblUrl.Size = new Size(400, 24);
            lblUrl.Location = new Point(20, 48);
            lblUrl.Visible = false;
            card.Controls.Add(lblUrl);

            lblPorts = new Label();
            lblPorts.Text = "HTTP :" + httpPort + "   TFTP :" + tftpPort + "   DHCP :" + dhcpPort;
            lblPorts.Font = new Font("Consolas", 10);
            lblPorts.ForeColor = DIM;
            lblPorts.BackColor = BG2;
            lblPorts.Size = new Size(400, 24);
            lblPorts.Location = new Point(20, 78);
            card.Controls.Add(lblPorts);

            // 网卡卡
            nicPanel = new Panel();
            nicPanel.BackColor = BG2;
            nicPanel.Size = new Size(440, 80);
            nicPanel.Location = new Point(80, 222);
            nicPanel.Paint += delegate(object s, PaintEventArgs e) {
                e.Graphics.DrawRectangle(new Pen(BORDER), 0, 0, nicPanel.Width - 1, nicPanel.Height - 1);
            };
            Controls.Add(nicPanel);

            Label nicTitle = new Label();
            nicTitle.Text = "选择启动网卡";
            nicTitle.Font = new Font("Microsoft YaHei UI", 10, FontStyle.Bold);
            nicTitle.ForeColor = FG;
            nicTitle.BackColor = BG2;
            nicTitle.Size = new Size(400, 24);
            nicTitle.Location = new Point(20, 12);
            nicPanel.Controls.Add(nicTitle);

            nicBox = new ComboBox();
            nicBox.DropDownStyle = ComboBoxStyle.DropDownList;
            nicBox.FlatStyle = FlatStyle.Flat;
            nicBox.BackColor = BG2;
            nicBox.ForeColor = FG;
            nicBox.Font = new Font("Microsoft YaHei UI", 10);
            nicBox.Size = new Size(400, 30);
            nicBox.Location = new Point(20, 42);
            nicBox.DropDownHeight = 320;
            nicBox.SelectedIndexChanged += delegate {
                if (nicApplying) return;
                string sel = (string)nicBox.SelectedItem;
                if (sel != null && nicIpMap.ContainsKey(sel)) SetNic(nicIpMap[sel]);
            };
            nicPanel.Controls.Add(nicBox);

            // 端口卡
            portPanel = new Panel();
            portPanel.BackColor = BG2;
            portPanel.Size = new Size(440, 190);
            portPanel.Location = new Point(80, 310);
            portPanel.Paint += delegate(object s, PaintEventArgs e) {
                e.Graphics.DrawRectangle(new Pen(BORDER), 0, 0, portPanel.Width - 1, portPanel.Height - 1);
            };
            Controls.Add(portPanel);

            Label portTitle = new Label();
            portTitle.Text = "服务端口（HTTP 可改 · 其余固定）";
            portTitle.Font = new Font("Microsoft YaHei UI", 10, FontStyle.Bold);
            portTitle.ForeColor = FG;
            portTitle.BackColor = BG2;
            portTitle.Size = new Size(400, 24);
            portTitle.Location = new Point(20, 12);
            portPanel.Controls.Add(portTitle);

            txtHttp = NewPortBox(httpPort.ToString(), 110, 44, false, 80);
            portPanel.Controls.Add(NewPortLabel("HTTP :", 20, 44, 80));
            portPanel.Controls.Add(txtHttp);
            portPanel.Controls.Add(NewPortLabel("RCLONE :", 240, 44, 90));
            portPanel.Controls.Add(NewPortBox("8081", 340, 44, true, 80));
            portPanel.Controls.Add(NewPortLabel("TFTP :", 20, 74, 80));
            portPanel.Controls.Add(NewPortBox(tftpPort.ToString(), 110, 74, true, 80));
            portPanel.Controls.Add(NewPortLabel("DHCP :", 240, 74, 90));
            portPanel.Controls.Add(NewPortBox(dhcpPort.ToString(), 340, 74, true, 80));

            portPanel.Controls.Add(NewPortLabel("DHCP模式", 20, 104, 100));
            dhcpModeBox = new ComboBox();
            dhcpModeBox.DropDownStyle = ComboBoxStyle.DropDownList;
            dhcpModeBox.FlatStyle = FlatStyle.Flat;
            dhcpModeBox.BackColor = BTN;
            dhcpModeBox.ForeColor = FG;
            dhcpModeBox.Font = new Font("Microsoft YaHei UI", 10);
            dhcpModeBox.Size = new Size(300, 28);
            dhcpModeBox.Location = new Point(130, 104);
            dhcpModeMap["完整DHCP（唯一服务器）"] = "dhcp";
            dhcpModeMap["代理DHCP（与路由器共存）"] = "proxy";
            dhcpModeMap["关闭"] = "off";
            dhcpModeBox.Items.Add("完整DHCP（唯一服务器）");
            dhcpModeBox.Items.Add("代理DHCP（与路由器共存）");
            dhcpModeBox.Items.Add("关闭");
            dhcpModeBox.SelectedIndex = 0;
            foreach (string k in dhcpModeMap.Keys) {
                if (dhcpModeMap[k] == dhcpMode) { dhcpModeBox.SelectedItem = k; break; }
            }
            portPanel.Controls.Add(dhcpModeBox);

            btnPorts = NewBtn("应用", 20, 140, 90, 34);
            btnPorts.Click += delegate { ApplyPorts(); };
            portPanel.Controls.Add(btnPorts);
            btnStop = NewBtn("停止", 120, 140, 90, 34);
            btnStop.Click += delegate { StopProject(); };
            portPanel.Controls.Add(btnStop);
            btnScan = NewBtn("端口检测", 220, 140, 100, 34);
            btnScan.Click += delegate { ScanPorts(); };
            portPanel.Controls.Add(btnScan);
            btnKill = NewBtn("清理占用", 330, 140, 90, 34);
            btnKill.Click += delegate { KillOccupiers(); };
            portPanel.Controls.Add(btnKill);

            btnY = 310 + 190 + 16;
            footY = btnY + 60;

            // 按钮行
            Panel row = new Panel();
            row.Size = new Size(600, 58);
            row.Location = new Point(0, btnY);
            row.BackColor = BG;
            btnOpen = NewBigBtn("  启动并打开  ", 70, 230, GREEN, Color.FromArgb(6, 43, 20));
            btnOpen.Click += delegate {
                if (TestService()) { Process.Start(manageUrl); return; }
                if (!StartService()) return;
                UpdateState();
                Process.Start(manageUrl);
            };
            row.Controls.Add(btnOpen);
            btnMin = NewBigBtn("  最小化  ", 320, 110, BTN, FG);
            btnMin.Font = new Font("Microsoft YaHei UI", 10);
            btnMin.Click += delegate { WindowState = FormWindowState.Minimized; };
            row.Controls.Add(btnMin);
            btnExit = NewBigBtn("  退出  ", 450, 120, Color.FromArgb(58, 35, 35), Color.White);
            btnExit.Font = new Font("Microsoft YaHei UI", 10);
            btnExit.Click += delegate { StopService(); StopPortUsers(); Close(); };
            row.Controls.Add(btnExit);
            Controls.Add(row);

            // 底部
            Label foot = new Label();
            foot.Text = "www.pxe-boot.local";
            foot.Font = new Font("Consolas", 8);
            foot.ForeColor = Color.FromArgb(85, 85, 102);
            foot.BackColor = BG;
            foot.Size = new Size(600, 18);
            foot.Location = new Point(0, footY);
            foot.TextAlign = ContentAlignment.MiddleCenter;
            Controls.Add(foot);

            ClientSize = new Size(600, footY + 34);
        }

        Label NewPortLabel(string text, int x, int y, int w) {
            Label l = new Label();
            l.Text = text;
            l.Font = new Font("Consolas", 10);
            l.ForeColor = FG;
            l.BackColor = BG2;
            l.Size = new Size(w, 24);
            l.Location = new Point(x, y);
            return l;
        }

        TextBox NewPortBox(string val, int x, int y, bool readOnly, int w) {
            TextBox t = new TextBox();
            t.Text = val;
            t.ReadOnly = readOnly;
            t.Enabled = !readOnly;
            t.Font = new Font("Consolas", 10);
            t.ForeColor = FG;
            t.BackColor = BTN;
            t.BorderStyle = BorderStyle.FixedSingle;
            t.Size = new Size(w, 24);
            t.Location = new Point(x, y);
            return t;
        }

        Button NewBtn(string text, int x, int y, int w, int h) {
            Button b = new Button();
            b.Text = text;
            b.Font = new Font("Microsoft YaHei UI", 9, FontStyle.Bold);
            b.ForeColor = FG;
            b.BackColor = BTN;
            b.FlatStyle = FlatStyle.Flat;
            b.FlatAppearance.BorderSize = 0;
            b.FlatAppearance.MouseOverBackColor = HOVER;
            b.Size = new Size(w, h);
            b.Location = new Point(x, y);
            b.Cursor = Cursors.Hand;
            return b;
        }

        Button NewBigBtn(string text, int x, int w, Color bg, Color fg) {
            Button b = new Button();
            b.Text = text;
            b.Font = new Font("Microsoft YaHei UI", 11, FontStyle.Bold);
            b.ForeColor = fg;
            b.BackColor = bg;
            b.FlatStyle = FlatStyle.Flat;
            b.FlatAppearance.BorderSize = 0;
            b.FlatAppearance.MouseOverBackColor = Color.FromArgb(Math.Min(255, bg.R + 16), Math.Min(255, bg.G + 16), Math.Min(255, bg.B + 16));
            b.Size = new Size(w, 42);
            b.Location = new Point(x, 0);
            b.Cursor = Cursors.Hand;
            return b;
        }

        protected override void OnShown(EventArgs e) {
            base.OnShown(e);
            // 居中主屏
            Screen scr = Screen.PrimaryScreen;
            int x = scr.Bounds.X + Math.Max(0, (scr.Bounds.Width - Width) / 2);
            int y = scr.Bounds.Y + Math.Max(0, (scr.Bounds.Height - Height) / 2);
            Location = new Point(x, y);
        }
    }

    public class Program {
        [STAThread]
        public static void Main() {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
        }
    }
}
