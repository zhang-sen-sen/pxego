/* PXE 网络引导服务器 - Web 管理界面 */
"use strict";

const $ = (sel) => document.querySelector(sel);
// 客户端列表缓存（供"已上线客户端总数"统计，每 30 秒刷新显示）
let lastClients = [];
function renderOnlineCount() {
  const el = $("#client-online-count");
  if (!el) return;
  const s = state.status || {};
  const on = s.online_count ?? 0;
  const off = s.offline_count ?? 0;
  const tot = s.total_online ?? (on + off);
  el.textContent = `已上线 ${on} 台 · 已下线 ${off} 台 · 合计 ${tot} 台`;
}
const fmtSize = (b) => {
  if (!b) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(1) + " " + u[i];
};
const fmtTime = (t) => {
  if (!t) return "-";
  const s = Math.floor(t);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${h}小时${m}分${sec}秒`;
};
const osTypeName = {
  ubuntu: "Ubuntu Live",
  debian_live: "Debian Live",
  redhat: "RedHat 安装器",
  debian_installer: "Debian 安装器",
  generic_linux: "Linux (通用)",
  windows: "Windows 安装",
  unknown: "未识别",
};

let state = { isos: [], status: null };
let toastTimer = null;
let uptimeBase = null;   // 最近一次成功获取的运行时长（秒）
let uptimeAt = 0;        // 获取 uptimeBase 的时刻
let uptimeFrozen = false; // 服务错误时冻结，不再递增
let logES = null;        // SSE 实时日志连接（在线时日志逐条推送）

// SSE 实时日志：新日志逐条追加到日志框，而不是整批弹出
function initLogStream() {
  if (logES) { try { logES.close(); } catch (e) {} logES = null; }
  try {
    logES = new EventSource("/api/logs/stream");
    logES.onmessage = (ev) => {
      const lb = $("#log-box");
      const data = ev.data;
      if (data.startsWith("\u001eR\u001e")) {
        // 进度日志更新：在原条目上刷新（同 key 覆盖），不新增一行
        const parts = data.split("\u001e");           // ["", "R", key, line]
        const key = parts[2];
        const line = parts[3];
        const lines = lb.textContent.split("\n");
        let idx = -1;
        if (key.startsWith("vnc:")) {
          // VNC 会话日志：key "vnc:<host>:<port>"，按 "远程控制 host:port"
          // 匹配最后一行替换，只保留 1 条最新的 VNC 日志
          const hostport = key.slice(4);
          for (let i = lines.length - 1; i >= 0; i--) {
            if (lines[i].indexOf("远程控制 " + hostport) !== -1) { idx = i; break; }
          }
        } else {
          // 下载进度：key 格式 "客户端IP:文件名"，多客户端同文件下载各自独立
          const sep = key.lastIndexOf(":");
          const ip = sep >= 0 ? key.slice(0, sep) : "";
          const name = sep >= 0 ? key.slice(sep + 1) : key;
          for (let i = lines.length - 1; i >= 0; i--) {
            if (lines[i].indexOf("下载 " + name) !== -1
                && (!ip || lines[i].indexOf("[" + ip + "]") !== -1)) { idx = i; break; }
          }
        }
        if (idx >= 0) lines[idx] = line; else lines.push(line);
        lb.textContent = sortLogLines(lines).join("\n");
        lb.scrollTop = lb.scrollHeight;
      } else if (data.startsWith("\u001eP\u001e")) {
        // 客户端上线：清掉该客户端已显示的过程日志，只保留上线/下线状态行
        const parts = data.split("\u001e");  // ["", "P", "ip1,ip2,...", macColon, macPlain]
        const ips = (parts[2] || "").split(",").filter(Boolean);
        const macColon = parts[3];
        const macPlain = parts[4];
        const lines = lb.textContent.split("\n");
        const kept = lines.filter((l) =>
          !isClientLog(l, ips, macColon, macPlain)
          || l.indexOf("已上线") !== -1 || l.indexOf("已下线") !== -1);
        lb.textContent = sortLogLines(kept).join("\n");
        lb.scrollTop = lb.scrollHeight;
      } else {
        const atBottom = lb.scrollHeight - lb.scrollTop - lb.clientHeight < 40;
        const lines = lb.textContent ? lb.textContent.split("\n") : [];
        lines.push(data);
        lb.textContent = sortLogLines(lines).join("\n");
        if (atBottom) lb.scrollTop = lb.scrollHeight;
      }
    };
    logES.onerror = () => {
      // 连接断开（如服务重启）：关闭并延迟重连，断开期间由轮询整体刷新兜底
      if (logES) { try { logES.close(); } catch (e) {} logES = null; }
      setTimeout(initLogStream, 5000);
    };
  } catch (e) {
    logES = null;
  }
}

function tsOf(line) {
  // 日志行 "[HH:MM:SS.mmm] ..." → 毫秒数（用于按时间排序）
  const m = /^\[(\d\d):(\d\d):(\d\d)\.(\d\d\d)\]/.exec(line || "");
  if (!m) return 0;
  return (+m[1] * 3600 + +m[2] * 60 + +m[3]) * 1000 + +m[4];
}
function sortLogLines(lines) {
  return lines.sort((a, b) => tsOf(a) - tsOf(b));
}

function containsIp(low, ip) {
  // 与服务端 _contains_ip 同规则：IP 后跟字母/数字视为更长地址，不算匹配
  let idx = low.indexOf(ip);
  while (idx !== -1) {
    const after = idx + ip.length;
    if (after >= low.length || !/[0-9a-z]/.test(low[after])) return true;
    idx = low.indexOf(ip, idx + 1);
  }
  return false;
}

function isClientLog(line, ips, macColon, macPlain) {
  // 与服务端 _line_is_client 同规则：MAC 或任一历史 IP 关联判断日志行属于某客户端
  if (!line) return false;
  const low = line.toLowerCase();
  if (macColon && (low.indexOf("客户端mac=" + macColon) !== -1
                   || low.indexOf("mac=" + macColon) !== -1)) return true;
  if (macPlain && low.replace(/:/g, "").indexOf(macPlain) !== -1) return true;
  for (const ip of ips || []) {
    if (!ip) continue;
    if (containsIp(low, ip)) return true;
  }
  return false;
}

function renderUptime() {
  const el = $("#st-uptime");
  if (uptimeBase === null) { el.textContent = "-"; return; }
  if (uptimeFrozen) { el.textContent = fmtTime(uptimeBase); return; }
  el.textContent = fmtTime(uptimeBase + (Date.now() - uptimeAt) / 1000);
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}

async function loadStatus() {
  try {
    state.status = await api("/api/status");
    const s = state.status;
    uptimeBase = s.uptime; uptimeAt = Date.now(); uptimeFrozen = false;
    renderUptime();
    $("#st-server").textContent = s.server_ip;
    $("#st-dhcp").textContent = (!s.dhcp_enable || s.dhcp_mode === "off") ? "关闭" : (s.dhcp_mode === "proxy" ? "代理" : "完整");
    // 缓存客户端列表供"已上线总数"统计（每 30 秒刷新显示）
    lastClients = s.clients || [];
    renderOnlineCount();
    // 客户端：编号由服务端分配（seq），已上线/已下线都显示编号
    const cl = $("#client-list");
    if (s.clients && s.clients.length) {
      const archName = {
        0: "BIOS", 6: "UEFI 32位", 7: "UEFI 64位", 8: "UEFI 64位", 9: "UEFI 64位",
        10: "UEFI ARM32", 11: "UEFI ARM64",
      };
      cl.innerHTML = s.clients.map((c) => {
        const arch = c.arch !== null && c.arch !== undefined
          ? (archName[c.arch] || `0x${Number(c.arch).toString(16)}`) : "";
        const no = c.seq ? String(c.seq).padStart(3, "0") : "";
        let status;
        if (c.state === "已上线") status = `已上线${no}号`;
        else if (c.state === "已下线") status = `已下线${no}号`;
        else if (c.state) status = c.state + (c.state.startsWith("正在") ? "…" : "");
        else status = "正在获取引导配置…";
        const vnc = (c.vnc && c.vnc.state === "online" && c.ip)
          ? `<button class="vnc-btn" title="远程控制 PE 桌面" data-vnc-host="${escapeHtml(c.ip)}" data-vnc-port="${Number(c.vnc.port) || 5900}">远程控制</button>`
          : "";
        const archSpan = arch ? `<span>${arch}</span>` : "";
        return `<div class="client-item">
          <div class="client-main"><span class="ip">${c.ip || "-"}</span>
            ${archSpan}<span>${status}</span>
            <span class="mac">${c.mac}</span>
            <span class="up-time">${c.time || "-"}</span></div>${vnc}</div>`;
      }).join("");
    } else {
      cl.innerHTML = '<p class="empty-sm">暂无记录</p>';
    }
    // 日志：SSE 在线时由 /api/logs/stream 逐条推送；断开/初次加载时轮询整体渲染
    if (!logES) {
      const logText = sortLogLines(s.logs || []).join("\n");
      const lb = $("#log-box");
      if (lb.textContent !== logText) {
        const atBottom = lb.scrollHeight - lb.scrollTop - lb.clientHeight < 24;
        lb.textContent = logText;
        if (atBottom) lb.scrollTop = lb.scrollHeight;
      }
    }
  } catch (e) {
    uptimeFrozen = true;   // 服务错误：停止时间递增，冻结显示
    renderUptime();
    toast("状态加载失败: " + e.message);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function openModal(id) { $("#" + id).classList.remove("hidden"); }
function closeModal(id) { $("#" + id).classList.add("hidden"); }

function fillConfigForm() {
  const c = state.status && state.status.server_ip ? state.status : null;
  if (!c) return;
  const cfg = window.__cfg || {};
  $("#cfg-ip").value = cfg.server_ip || "";
  $("#cfg-http").value = cfg.http_port || 80;
  $("#cfg-dhcp-mode").value = cfg.dhcp_mode || "dhcp";
  $("#cfg-pool-start").value = (cfg.ip_pool || {}).start || "";
  $("#cfg-pool-end").value = (cfg.ip_pool || {}).end || "";
  $("#cfg-gateway").value = cfg.gateway || "";
  $("#cfg-dns").value = cfg.dns || "";
  $("#cfg-title").value = (cfg.menu || {}).title || "PXE 网络引导菜单";
  const mf = cfg.mac_filter || {};
  $("#cfg-mac-mode").value = mf.mode || "off";
  $("#cfg-mac-list").value = (mf.list || []).join(", ");
  $("#cfg-vnc-pwd").value = cfg.vnc_password || "";
}

async function loadConfigForm() {
  try {
    window.__cfg = await api("/api/config");
    fillConfigForm();
  } catch (e) { toast("配置读取失败: " + e.message); }
}

async function loadBootFiles() {
  try {
    const data = await api("/api/bootfiles");
    const tbody = $("#bootfiles-table tbody");
    tbody.innerHTML = data.map((f) => `
      <tr><td><code>${f.name}</code></td>
      <td class="${f.ok ? "status-ok" : "status-miss"}">${f.ok ? "已就绪" : "缺失"}</td>
      <td>${escapeHtml(f.desc)}</td></tr>`).join("");
  } catch (e) { toast("引导文件状态读取失败: " + e.message); }
}

function bindEvents() {
  $("#btn-config").onclick = async () => { await loadConfigForm(); openModal("modal-config"); };
  $("#cfg-save").onclick = async () => {
    const patch = {
      server_ip: $("#cfg-ip").value.trim(),
      http_port: parseInt($("#cfg-http").value) || 80,
      dhcp_mode: $("#cfg-dhcp-mode").value,
      ip_pool: { start: $("#cfg-pool-start").value.trim(), end: $("#cfg-pool-end").value.trim() },
      gateway: $("#cfg-gateway").value.trim(),
      dns: $("#cfg-dns").value.trim(),
      menu: { title: $("#cfg-title").value.trim() },
      mac_filter: {
        mode: $("#cfg-mac-mode").value,
        list: $("#cfg-mac-list").value.split(",").map((s) => s.trim()).filter(Boolean),
      },
      vnc_password: $("#cfg-vnc-pwd").value.trim(),
    };
    try {
      await api("/api/config", { method: "PATCH", body: JSON.stringify(patch) });
      toast("配置已保存，正在重启服务...");
      closeModal("modal-config");
      try { await api("/api/restart", { method: "POST" }); } catch (e) { /* 服务已重启，连接断开属正常 */ }
      setTimeout(() => { location.reload(); }, 8000);
    } catch (e) { toast("保存失败: " + e.message); }
  };
  $("#cfg-cancel").onclick = () => closeModal("modal-config");
  $("#btn-bootfiles").onclick = async () => { await loadBootFiles(); openModal("modal-bootfiles"); };
  $("#bf-close").onclick = () => closeModal("modal-bootfiles");
  // 客户端列表：点击「远程控制」在新标签页打开 noVNC
  $("#client-list").addEventListener("click", (e) => {
    const btn = e.target.closest(".vnc-btn");
    if (!btn) return;
    const host = btn.dataset.vncHost;
    const port = btn.dataset.vncPort;
    const pwd = (window.__cfg && window.__cfg.vnc_password) || "";
    window.open(`/vnc/vnc.html?host=${encodeURIComponent(host)}&port=${port}&password=${encodeURIComponent(pwd)}`, "_blank");
  });
  document.querySelectorAll(".modal").forEach((m) => {
    m.addEventListener("click", (e) => { if (e.target === m) m.classList.add("hidden"); });
  });
}

async function init() {
  bindEvents();
  try { window.__cfg = await api("/api/config"); } catch (e) {} // 缓存配置（VNC 密码等）
  await loadStatus();
  initLogStream();   // 实时日志逐条推送（断开自动重连，回退轮询）
  setInterval(() => { loadStatus(); }, 5000);
  setInterval(() => { renderUptime(); }, 1000);  // 运行时长每秒递增
  setInterval(() => { renderOnlineCount(); }, 30000);  // 已上线客户端总数每 30 秒刷新
}

init();
