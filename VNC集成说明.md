# PXEGo VNC 远程维护功能集成说明

本文件记录 PXEGo 新增的「PE 客户机 VNC 远程维护」功能：Web 管理界面一键连接 PE 桌面，浏览器内直接操作。

## 功能概述

PE 客户机网络启动进入维护系统后，自动运行 TightVNC 服务端（免密）。服务器管理界面对在线客户端显示「远程控制」按钮，点击后浏览器打开 noVNC 页面，经服务器 WebSocket 代理直达客户机 VNC 端口，即可远程操作 PE 桌面。

**认证方式：免密（None 认证）**。按部署需求（纯内网、不考虑安全）设计，无密码认证环节。

## 架构

```
浏览器(noVNC) ──WebSocket──▶ PXEGo 服务器(/vnc/ws 代理) ──TCP 5900──▶ PE 客户机(TightVNC)
管理界面(在线状态) ◀──/api/vnc── PE 启动脚本回报
```

- 服务器端：`pxe/vncproxy.py`（RFC 6455 纯标准库 WebSocket↔TCP 双向桥接）
- Web 端：`web/vnc/vnc.html` + `web/vnc/novnc-bundle.js`（noVNC 单文件打包）
- PE 端：`BOOT.WIM` 内置 TightVNC，`VTOYMNT.BAT` 启动并回报在线

## 改动清单

### 服务器端
| 文件 | 改动 |
|---|---|
| `pxe/vncproxy.py` | 新增：WebSocket↔TCP 双向代理（RFC 6455 帧编解码，零依赖） |
| `pxe/httpd.py` | 新增 `/vnc/ws` 升级端点（端口 5900–5999、Origin 同源校验、回显子协议 binary、空闲 300s 超时）、`/vnc/*` 静态服务、`/api/vnc` 在线回报接口 |
| `pxe/server.py` | 新增 `client_vnc()` 登记与客户端下线联动 |
| `pxe/config.py` | 新增 `vnc_password`（默认空，免密）/`vnc_port`（5900）配置项 |

### Web 端
| 文件 | 改动 |
|---|---|
| `web/app.js` | 客户端在线且 `vnc.state==online` 时渲染「远程控制」按钮；点击打开 noVNC 新标签 |
| `web/index.html` | 配置弹窗增加 VNC 密码输入项（免密模式下留空即可） |
| `web/style.css` | `.vnc-btn` / `.client-main` 样式 |
| `web/vnc/vnc.html` | 新增：noVNC 入口（host/port/password 从 query 读取、自动连接、缩放/全屏/重连） |
| `web/vnc/novnc-bundle.js` | noVNC v1.5.0 打包产物（由 `tools/build_novnc_bundle.py` 生成，勿手改） |

### PE 端（BOOT.WIM，已提交）
- `Program Files\TightVNC\`：tvnserver.exe、screenhooks64.dll、sas.dll、hookldr.exe（免 VC 运行库，可直接运行）
- `Windows\System32\VTOYMNT.BAT`：新增第 9 段 `:startvnc` —— reg add 写入免密配置（**HKLM 与 HKCU 双键写入**，TightVNC 优先读 HKCU，只写 HKLM 会导致「Require VNC authentication」仍勾选、连接要求密码）→ 启动 tvnserver → curl 回报 `/api/vnc?mac=..&port=5900&state=online`（带 5 次重试）

PE 端免密注册表配置（HKLM\SOFTWARE\TightVNC\Server 与 HKCU\Software\TightVNC\Server 各一份）：
```
UseVncAuthentication=0   ← 免密关键（None 认证）
AcceptConnections=1  LoopbackOnly=0  AllowLoopback=1
RfbPort=5900  QuerySetting=0  DisableTrayIcon=1
AllowEditClients=1  FileTransferEnabled=0
RemoveWallpaper=1  BlankMonitorEnabled=0  LogLevel=0
```

## 使用说明

1. 启动 PXEGo 服务器（`python main.py`，Web 端口默认 8080）。
2. 客户机 PXE 启动进 PE，VTOYMNT.BAT 会自动：发现服务器 → 挂载 Z: 盘 → 上报在线 → 启动 TightVNC → 回报 VNC 在线。
3. 打开管理界面 `http://<服务器IP>:8080/`，在线客户端行出现绿色「远程控制」按钮。
4. 点击按钮，浏览器新标签打开 noVNC 页面，自动连接并显示 PE 桌面，可直接鼠标键盘操作。
5. 缩放/全屏按钮在 noVNC 页顶部；连接断开后点「连接」重连。

## 验证记录（2026-10-22）

- 免密链路协议级验证：RFB 3.8 握手 → 安全类型 [1,16] → 选 None → SecurityResult 00000000 成功 → ClientInit → ServerInit（桌面名 chinami-vi1llmf）✅
- 浏览器端：管理界面「远程控制」→ noVNC 新标签 → 标题含桌面名、状态「已连接」、console 无错误 ✅
- PE 启动段宿主仿真：reg add 全部成功、tvnserver 启动、`/api/vnc` 回报 `{"ok": true}` ✅
- **修复记录**：实测 PE 中「Require VNC authentication」仍勾选、连接要求密码 → 根因是 TightVNC 优先读 HKCU\Software\TightVNC\Server，而 PE 脚本只写 HKLM → 已改为 HKLM+HKCU 双键写入并重新提交（宿主机验证：仅 HKLM → [2,16] 要密码；双键 → [1,16] 免密）✅
- BOOT.WIM 已用 DISM 提交（备份：`_vnc_work\BOOT.WIM.bak`）

## 安全说明

- **当前为免密（None 认证）**：纯内网使用的前提，任何能访问管理界面的人都能远程控制在线 PE 客户机。
- 若需恢复密码认证：设置 `config.json` 的 `vnc_password`（1-8 位）并**同步修改 PE 端 VTOYMNT.BAT 注册表段**（`UseVncAuthentication=1` + 密码加密值），二者必须一致。注意 TightVNC 2.x 密码存储格式与 noVNC 算法存在兼容性问题，启用前务必实测。

## 第三方组件合规声明

| 组件 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| TightVNC | 2.8.88 | GPL-2.0（官方 MSI 解包，含 LICENSE.txt） | PE 端 VNC 服务端 |
| noVNC | 1.5.0 | MPL-2.0（lib/ 源码 + LICENSE-noVNC.txt） | 浏览器端 VNC 客户端 |

两者均为开源组件，随本项目的再分发须保留相应许可证文本（TightVNC 的 LICENSE.txt 已随 WIM 内置；noVNC 许可见 `web/vnc/LICENSE-noVNC.txt`）。

## 测试残留与清理

以下为本机联调测试产生的临时内容，不影响功能：
- 测试进程：8080（main.py 主服务）、8081（ISOS 服务）、5900（本机测试 tvnserver）
- 本机注册表 HKLM/HKCU `SOFTWARE\TightVNC\Server`（免密配置，PE 启动脚本会自行写入，无需保留）
- `_vnc_work\`：组件源、WIM 备份（BOOT.WIM.bak，703,924,666B 为改动前原始版）、验证日志
- 清理命令（如需）：`Stop-Process -Name tvnserver -Force`；删除注册表键 `reg delete "HKLM\SOFTWARE\TightVNC\Server" /f`

## 已知限制

- 本机验证使用局域网 IP（192.168.10.2）绕过 TightVNC 回环限制，生产环境服务器→客户机走局域网不受影响。
- VNC 端口固定 5900（PE 端默认）；接口支持 `?port=` 传参，如需多客户机自定义端口需同步修改 PE 脚本。
- 只支持单会话远程维护；同一客户机被多人同时连接时以最后连接者为准（TightVNC 默认行为）。
