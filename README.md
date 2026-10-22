<div align="center">

![PXEGo](PXEGO-banner.png)

# PXEGo

**Windows 平台的 PXE 网络引导服务器 · 零依赖 · 一键启动**

![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Boot](https://img.shields.io/badge/boot-UEFI%20%2B%20BIOS-lightgrey)
![DHCP](https://img.shields.io/badge/DHCP-Full%20%2F%20Proxy%20%2F%20Off-orange)

内置 Python 运行时与 Web 管理界面，双击启动即可在局域网内提供
**DHCP / TFTP / HTTP** 引导服务；PE 启动后自动探测服务器并映射 **Z 盘**，
把 ISOS 目录与常用工具直接呈现在 PE 桌面。

</div>

---

## ✨ 功能特性

- **零依赖一键启动**：内置 Python 运行时，无需安装任何环境；`launcher.cmd` 双击即用
- **Web 管理界面**：浏览器打开 `http://<服务器IP>:8080` 即可配置服务器、上传 ISO、查看客户端与日志
- **三种 DHCP 模式**：完整 DHCP（唯一服务器）/ 代理 DHCP（与现有路由器共存）/ 关闭
- **UEFI + BIOS 双引导**：GRUB2 / iPXE / Syslinux / wimboot 引导链
- **PE 自动映射 Z 盘**：PE 进入桌面后自动探测服务器（UDP 广播），挂载 ISOS 与常用工具目录
- **常用工具桌面直达**：Z 盘中的"常用工具"文件夹自动生成 PE 桌面快捷方式
- **客户端上线统计**：实时统计已上线客户端总数与上下线日志
- **VNC 远程维护**：PE 进入桌面后自动启动 TightVNC（免密），Web 界面一键「远程控制」即可在浏览器中操作客户机（noVNC，纯内网使用）
- **端口冲突自检**：启动器内置端口占用检测与一键清理

## 🚀 快速开始

1. 将本项目解压到任意目录（路径建议不要包含中文与空格）
2. 双击 **`launcher.cmd`**，在启动器窗口中选择网卡与端口
3. 点击 **「启动并打开」**，浏览器自动打开 Web 管理界面
4. 将引导镜像放入 **`ISOS`** 目录，或在 Web 界面中上传
5. 目标机器 BIOS 设为 **网络启动（PXE）**，开机即可引导

> 首次使用：Windows 防火墙若弹出提示，请允许访问（局域网/专用网络）。
> 完整 DHCP 模式下请确保局域网内无其他 DHCP 服务器，避免地址冲突。

## 🖥️ Web 管理界面

| 模块 | 说明 |
|---|---|
| 服务器配置 | 网卡 IP、HTTP 端口、DHCP 模式、地址池、引导文件等 |
| 客户端 / 租约 | 已上线客户端总数（每 30 秒刷新）、租约列表、上下线日志 |
| 最近日志 | 实时滚动日志（含客户端 IP、引导文件下载进度 1% 刷新） |
| ISO 管理 | 上传 / 重新扫描 ISOS 目录（已按需精简） |

## 🧩 服务端口

| 服务 | 协议 / 端口 | 说明 |
|---|---|---|
| HTTP（Web 管理 + 引导文件） | TCP `8080`（可改） | Web 界面与 HTTP 引导 |
| TFTP | UDP `69` | 引导文件传输 |
| DHCP | UDP `67` | 地址分配 / PXE 应答 |
| RCLONE / 发现服务 | TCP+UDP `8081` | PE 端挂载发现与数据传输 |

> **VNC 远程维护**：PE 客户机内 TightVNC 监听 `5900`（客户机侧，服务器不监听该端口）；
> 浏览器经 HTTP 端口的 `/vnc/ws` WebSocket 代理中转控制，免密连接，仅适合隔离内网。

## 🔧 DHCP 模式说明

| 模式 | 适用场景 |
|---|---|
| 完整 DHCP（唯一服务器） | 局域网内无其他 DHCP，本机作为唯一地址分配服务器 |
| 代理 DHCP（与路由器共存） | 与现有路由器 DHCP 共存，仅应答 PXE 引导请求 |
| 关闭 | 由其他 DHCP 服务器完成引导，本项目不参与地址分配 |

## 📁 目录结构（精简）

```
PXEGo/
├── launcher.cmd          # 双击启动入口（自动提权）
├── launcher.ps1          # 图形启动器（端口/DHCP模式/占用清理）
├── config.json           # 服务器配置
├── main.py               # 服务主进程（DHCP/TFTP/HTTP）
├── http_server.py        # RCLONE 发现与数据服务
├── ftp_server.py         # 可选 FTP 服务
├── pxe/                  # 核心库（DHCP/TFTP/HTTP/引导链）
├── web/                  # Web 管理界面
├── boot/                 # 引导文件（GRUB2/iPXE/Syslinux/wimboot）
├── runtime/              # 内置 Python 运行时
├── tools/                # 辅助脚本
├── ISOS/                 # 引导镜像目录（用户自备）
└── THIRD_PARTY_NOTICES.txt # 第三方组件许可声明
```

## 📜 许可

本项目主程序采用 **MIT License**，详见 [LICENSE](LICENSE)。

> ⚠️ 注意：`boot\` 目录下的引导程序（GRUB2、iPXE、Syslinux、wimboot）为
> **GPL** 许可的独立可执行文件，与本项目为聚合分发关系，其各自许可与源码
> 见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)。`BOOT.WIM` 中的
> Windows PE 由您自备的安装介质构建，请遵守微软许可条款。
