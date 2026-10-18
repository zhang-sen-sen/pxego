﻿# -*- coding: utf-8 -*-
# PXE 网络引导服务器 —— 桌面启动器（PowerShell WinForms）
# 运行：双击 launcher.cmd，或 powershell -ExecutionPolicy Bypass -File launcher.ps1
# 自检：powershell -ExecutionPolicy Bypass -File launcher.ps1 -SelfTest

param([switch]$SelfTest, [switch]$AutoStart)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Base = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------------- 读取配置 ----------------
$cfg = @{ ip = "192.168.1.10"; port = 8080; tftp = 69; dhcp = 67; dhcpMode = "dhcp" }
$cfgPath = Join-Path $Base "config.json"
if (Test-Path $cfgPath) {
    try {
        $d = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($d.server_ip) { $cfg.ip = [string]$d.server_ip }
        if ($d.http_port) { $cfg.port = [int]$d.http_port }
        if ($d.tftp_port) { $cfg.tftp = [int]$d.tftp_port }
        if ($d.dhcp_port) { $cfg.dhcp = [int]$d.dhcp_port }
        if ($d.dhcp_mode) { $cfg.dhcpMode = [string]$d.dhcp_mode }
    } catch {}
}
$ManageUrl = "http://$($cfg.ip):$($cfg.port)/"

# ---------------- 网卡枚举（名称 + 当前 IPv4 + MAC） ----------------
function Get-NicList {
    $list = @()
    Get-CimInstance Win32_NetworkAdapterConfiguration -ErrorAction SilentlyContinue |
        Where-Object { $_.IPEnabled } | ForEach-Object {
            $v4 = @($_.IPAddress) | Where-Object { $_ -match '^\d{1,3}(\.\d{1,3}){3}$' } | Select-Object -First 1
            if ($v4 -and $v4 -notlike "169.254*") {
                $list += [PSCustomObject]@{ Name = $_.Description; IP = $v4; MAC = $_.MACAddress }
            }
        }
    return $list
}
$script:nics = @(Get-NicList)
$script:nicApplying = $false

# 颜色
$BG    = [System.Drawing.Color]::FromArgb(20, 20, 28)
$BG2   = [System.Drawing.Color]::FromArgb(30, 30, 40)
$FG    = [System.Drawing.Color]::FromArgb(232, 232, 240)
$DIM   = [System.Drawing.Color]::FromArgb(154, 154, 174)
$GREEN = [System.Drawing.Color]::FromArgb(0, 230, 118)
$RED   = [System.Drawing.Color]::FromArgb(255, 82, 82)
$BTN   = [System.Drawing.Color]::FromArgb(38, 38, 51)

# ---------------- 窗体 ----------------
$form = New-Object System.Windows.Forms.Form
$form.Text = "PXE 网络引导服务器"
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.ClientSize = New-Object System.Drawing.Size(420, 470)
$form.BackColor = $BG
$form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)

# ---- Logo（PictureBox 自绘：圆环 + 节点 + PXE）----
$logo = New-Object System.Windows.Forms.PictureBox
$logo.Size = New-Object System.Drawing.Size(190, 150)
$logo.Location = New-Object System.Drawing.Point(115, 20)
$logo.BackColor = $BG
$logo.add_Paint({ param($s, $e)
    $g = $e.Graphics
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $pen  = New-Object System.Drawing.Pen($GREEN, 3)
    $pen2 = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(58, 58, 74), 1)
    $g.DrawEllipse($pen,  40, 20, 110, 110)
    $g.DrawEllipse($pen2, 52, 32, 86, 86)
    $brush = New-Object System.Drawing.SolidBrush($GREEN)
    $g.FillEllipse($brush, 82, 62, 16, 16)
    # 外围节点 + 连线
    foreach ($pt in @(@(70,40), @(110,40), @(90,100))) {
        $g.DrawLine($pen2, 90, 70, $pt[0], $pt[1])
        $g.DrawEllipse($pen, $pt[0]-4, $pt[1]-4, 8, 8)
    }
    # 下载箭头
    $g.DrawLine($pen2, 90, 52, 90, 78)
    $g.DrawLine($pen2, 80, 70, 90, 78)
    $g.DrawLine($pen2, 100, 70, 90, 78)
    $tf = New-Object System.Drawing.Font("Consolas", 10, [System.Drawing.FontStyle]::Bold)
    $g.DrawString("PXE", $tf, $brush, 62, 112)
    $pen.Dispose(); $pen2.Dispose(); $brush.Dispose(); $tf.Dispose()
})
$form.Controls.Add($logo)

# ---- 标题 ----
$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Text = "PXE 网络引导服务器"
$lblTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 16, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = $FG
$lblTitle.BackColor = $BG
$lblTitle.Size = New-Object System.Drawing.Size(420, 34)
$lblTitle.Location = New-Object System.Drawing.Point(0, 168)
$lblTitle.TextAlign = "MiddleCenter"
$form.Controls.Add($lblTitle)

$lblSub = New-Object System.Windows.Forms.Label
$lblSub.Text = "网络引导  ·  支持 UEFI + BIOS"
$lblSub.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$lblSub.ForeColor = $DIM
$lblSub.BackColor = $BG
$lblSub.Size = New-Object System.Drawing.Size(420, 22)
$lblSub.Location = New-Object System.Drawing.Point(0, 204)
$lblSub.TextAlign = "MiddleCenter"
$form.Controls.Add($lblSub)

# ---- 状态卡 ----
$card = New-Object System.Windows.Forms.Panel
$card.Size = New-Object System.Drawing.Size(300, 82)
$card.Location = New-Object System.Drawing.Point(60, 242)
$card.BackColor = $BG2
$form.Controls.Add($card)

$lblDot = New-Object System.Windows.Forms.Label
$lblDot.Text = "●"
$lblDot.Font = New-Object System.Drawing.Font("Segoe UI", 12)
$lblDot.ForeColor = $RED
$lblDot.BackColor = $BG2
$lblDot.Size = New-Object System.Drawing.Size(20, 22)
$lblDot.Location = New-Object System.Drawing.Point(16, 8)
$card.Controls.Add($lblDot)

$lblState = New-Object System.Windows.Forms.Label
$lblState.Text = "服务未运行"
$lblState.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10)
$lblState.ForeColor = $FG
$lblState.BackColor = $BG2
$lblState.Size = New-Object System.Drawing.Size(400, 22)
$lblState.Location = New-Object System.Drawing.Point(42, 8)
$card.Controls.Add($lblState)

$lblUrl = New-Object System.Windows.Forms.Label
$lblUrl.Text = $ManageUrl
$lblUrl.Font = New-Object System.Drawing.Font("Consolas", 10)
$lblUrl.ForeColor = $GREEN
$lblUrl.BackColor = $BG2
$lblUrl.Size = New-Object System.Drawing.Size(270, 20)
$lblUrl.Location = New-Object System.Drawing.Point(16, 32)
$lblUrl.Visible = $false   # 服务未运行时隐藏地址
$card.Controls.Add($lblUrl)

$lblPorts = New-Object System.Windows.Forms.Label
$lblPorts.Text = "HTTP :$($cfg.port)   TFTP :$($cfg.tftp)   DHCP :$($cfg.dhcp)"
$lblPorts.Font = New-Object System.Drawing.Font("Consolas", 9)
$lblPorts.ForeColor = $DIM
$lblPorts.BackColor = $BG2
$lblPorts.Size = New-Object System.Drawing.Size(270, 18)
$lblPorts.Location = New-Object System.Drawing.Point(16, 56)
$card.Controls.Add($lblPorts)
$lblPorts.Visible = $false   # 端口状态行不再显示（端口卡已展示）

# ---- 网卡选择卡（下拉框） ----
$script:nicPanel = New-Object System.Windows.Forms.Panel
$script:nicPanel.BackColor = $BG2
$script:nicCount = $script:nics.Count
$script:nicH = 56
$script:nicPanel.Size = New-Object System.Drawing.Size(300, $script:nicH)
$script:nicPanel.Location = New-Object System.Drawing.Point(60, 332)
$form.Controls.Add($script:nicPanel)

$nicTitle = New-Object System.Windows.Forms.Label
$nicTitle.Text = "选择启动网卡"
$nicTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9, [System.Drawing.FontStyle]::Bold)
$nicTitle.ForeColor = $FG
$nicTitle.BackColor = $BG2
$nicTitle.Size = New-Object System.Drawing.Size(270, 20)
$nicTitle.Location = New-Object System.Drawing.Point(14, 6)
$script:nicPanel.Controls.Add($nicTitle)

$script:nicIpMap = @{}
if ($script:nicCount -gt 0) {
    $script:nicBox = New-Object System.Windows.Forms.ComboBox
    $script:nicBox.DropDownStyle = "DropDownList"
    $script:nicBox.FlatStyle = "Flat"
    $script:nicBox.BackColor = $BG2
    $script:nicBox.ForeColor = $FG
    $script:nicBox.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $script:nicBox.Size = New-Object System.Drawing.Size(284, 24)
    $script:nicBox.Location = New-Object System.Drawing.Point(14, 28)
    $script:nicBox.DropDownHeight = 280
    foreach ($n in $script:nics) {
        $short = $n.Name
        if ($short -match 'VMnet\d+') { $short = $matches[0] }
        if ($short.Length -gt 32) { $short = $short.Substring(0, 32) }
        $item = "$($n.IP)  ·  $short"
        [void]$script:nicBox.Items.Add($item)
        $script:nicIpMap[$item] = $n.IP
    }
    $script:nicBox.Add_SelectedIndexChanged({
        if (-not $script:nicApplying) {
            $ip = $script:nicIpMap[$script:nicBox.SelectedItem]
            if ($ip) { Set-Nic $ip }
        }
    })
    $script:nicPanel.Controls.Add($script:nicBox)
} else {
    $nicNone = New-Object System.Windows.Forms.Label
    $nicNone.Text = "未检测到有 IP 的网卡"
    $nicNone.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $nicNone.ForeColor = $RED
    $nicNone.BackColor = $BG2
    $nicNone.Size = New-Object System.Drawing.Size(270, 20)
    $nicNone.Location = New-Object System.Drawing.Point(14, 30)
    $script:nicPanel.Controls.Add($nicNone)
}

# ---- 服务端口卡（可修改 HTTP/TFTP/DHCP 三个端口 + DHCP 模式） ----
$script:portPanel = New-Object System.Windows.Forms.Panel
$script:portPanel.BackColor = $BG2
$script:portPanel.Size = New-Object System.Drawing.Size(300, 130)
$script:portPanel.Location = New-Object System.Drawing.Point(60, (332 + $script:nicH + 8))
$form.Controls.Add($script:portPanel)

$portTitle = New-Object System.Windows.Forms.Label
$portTitle.Text = "服务端口（HTTP 可改 · 其余固定）"
$portTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9, [System.Drawing.FontStyle]::Bold)
$portTitle.ForeColor = $FG
$portTitle.BackColor = $BG2
$portTitle.Size = New-Object System.Drawing.Size(272, 20)
$portTitle.Location = New-Object System.Drawing.Point(14, 6)
$script:portPanel.Controls.Add($portTitle)

function New-PortLabel($text, $x, $y) {
    $pl = New-Object System.Windows.Forms.Label
    $pl.Text = $text
    $pl.Font = New-Object System.Drawing.Font("Consolas", 9)
    $pl.ForeColor = $FG
    $pl.BackColor = $BG2
    $pl.Size = New-Object System.Drawing.Size(64, 22)
    $pl.Location = New-Object System.Drawing.Point($x, $y)
    return $pl
}
function New-PortBox($val, $x, $y, $readonly) {
    $pt = New-Object System.Windows.Forms.TextBox
    $pt.Text = [string]$val
    $pt.ReadOnly = $readonly
    $pt.Enabled = -not $readonly   # 固定端口禁用选择
    $pt.Font = New-Object System.Drawing.Font("Consolas", 9)
    $pt.ForeColor = $FG
    $pt.BackColor = $BTN
    $pt.BorderStyle = "FixedSingle"
    $pt.Size = New-Object System.Drawing.Size(56, 22)
    $pt.Location = New-Object System.Drawing.Point($x, $y)
    return $pt
}

# 行1：HTTP（可改） + RCLONE（固定 8081），同一行不换行
$script:txtHttp = New-Object System.Windows.Forms.TextBox
$script:portPanel.Controls.Add((New-PortLabel "HTTP :" 14 26))
$script:txtHttp.Text = [string]$cfg.port
$script:txtHttp.Font = New-Object System.Drawing.Font("Consolas", 9)
$script:txtHttp.ForeColor = $FG
$script:txtHttp.BackColor = $BTN
$script:txtHttp.BorderStyle = "FixedSingle"
$script:txtHttp.Size = New-Object System.Drawing.Size(56, 22)
$script:txtHttp.Location = New-Object System.Drawing.Point(80, 26)
$script:portPanel.Controls.Add($script:txtHttp)
$script:portPanel.Controls.Add((New-PortLabel "RCLONE：" 140 26))
$txtRclone = New-PortBox 8081 206 26 $true
$script:portPanel.Controls.Add($txtRclone)

# 行2：TFTP（固定）+ DHCP（固定）
$script:txtTftp = New-Object System.Windows.Forms.TextBox
$script:txtDhcp = New-Object System.Windows.Forms.TextBox
$script:portPanel.Controls.Add((New-PortLabel "TFTP :" 14 50))
$script:txtTftp.Text = [string]$cfg.tftp
$script:txtTftp.ReadOnly = $true
$script:txtTftp.Enabled = $false
$script:txtTftp.Font = New-Object System.Drawing.Font("Consolas", 9)
$script:txtTftp.ForeColor = $FG
$script:txtTftp.BackColor = $BTN
$script:txtTftp.BorderStyle = "FixedSingle"
$script:txtTftp.Size = New-Object System.Drawing.Size(56, 22)
$script:txtTftp.Location = New-Object System.Drawing.Point(80, 50)
$script:portPanel.Controls.Add($script:txtTftp)
$script:portPanel.Controls.Add((New-PortLabel "DHCP :" 140 50))
$script:txtDhcp.Text = [string]$cfg.dhcp
$script:txtDhcp.ReadOnly = $true
$script:txtDhcp.Enabled = $false
$script:txtDhcp.Font = New-Object System.Drawing.Font("Consolas", 9)
$script:txtDhcp.ForeColor = $FG
$script:txtDhcp.BackColor = $BTN
$script:txtDhcp.BorderStyle = "FixedSingle"
$script:txtDhcp.Size = New-Object System.Drawing.Size(56, 22)
$script:txtDhcp.Location = New-Object System.Drawing.Point(206, 50)
$script:portPanel.Controls.Add($script:txtDhcp)

# 行3：DHCP 模式（完整DHCP/代理DHCP/关闭），与 Web 管理界面一致
$script:portPanel.Controls.Add((New-PortLabel "DHCP模式" 14 74))
$script:dhcpModeBox = New-Object System.Windows.Forms.ComboBox
$script:dhcpModeBox.DropDownStyle = "DropDownList"
$script:dhcpModeBox.FlatStyle = "Flat"
$script:dhcpModeBox.BackColor = $BTN
$script:dhcpModeBox.ForeColor = $FG
$script:dhcpModeBox.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$script:dhcpModeBox.Size = New-Object System.Drawing.Size(186, 22)
$script:dhcpModeBox.Location = New-Object System.Drawing.Point(86, 74)
$script:dhcpModeItems = @(
    @{ Text = "完整DHCP（唯一服务器）"; Val = "dhcp" },
    @{ Text = "代理DHCP（与路由器共存）"; Val = "proxy" },
    @{ Text = "关闭"; Val = "off" }
)
$script:dhcpModeMap = @{}
foreach ($_mi in $script:dhcpModeItems) {
    [void]$script:dhcpModeBox.Items.Add($_mi.Text)
    $script:dhcpModeMap[$_mi.Text] = $_mi.Val
}
$script:dhcpModeBox.SelectedIndex = 0
foreach ($_k in $script:dhcpModeMap.Keys) {
    if ($script:dhcpModeMap[$_k] -eq $cfg.dhcpMode) { $script:dhcpModeBox.SelectedItem = $_k; break }
}
$script:portPanel.Controls.Add($script:dhcpModeBox)

$btnPorts = New-Object System.Windows.Forms.Button
$btnPorts.Text = "应用"
$btnPorts.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9, [System.Drawing.FontStyle]::Bold)
$btnPorts.ForeColor = $FG
$btnPorts.BackColor = $BTN
$btnPorts.FlatStyle = "Flat"
$btnPorts.FlatAppearance.BorderSize = 0
$btnPorts.Size = New-Object System.Drawing.Size(58, 26)
$btnPorts.Location = New-Object System.Drawing.Point(12, 100)
$btnPorts.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnPorts.add_Click({ Apply-Ports })
$script:portPanel.Controls.Add($btnPorts)

$btnStop = New-Object System.Windows.Forms.Button
$btnStop.Text = "停止"
$btnStop.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9, [System.Drawing.FontStyle]::Bold)
$btnStop.ForeColor = $FG
$btnStop.BackColor = $BTN
$btnStop.FlatStyle = "Flat"
$btnStop.FlatAppearance.BorderSize = 0
$btnStop.Size = New-Object System.Drawing.Size(58, 26)
$btnStop.Location = New-Object System.Drawing.Point(74, 100)
$btnStop.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnStop.add_Click({ Stop-Project })
$script:portPanel.Controls.Add($btnStop)

$btnScan = New-Object System.Windows.Forms.Button
$btnScan.Text = "端口检测"
$btnScan.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9, [System.Drawing.FontStyle]::Bold)
$btnScan.ForeColor = $FG
$btnScan.BackColor = $BTN
$btnScan.FlatStyle = "Flat"
$btnScan.FlatAppearance.BorderSize = 0
$btnScan.Size = New-Object System.Drawing.Size(74, 26)
$btnScan.Location = New-Object System.Drawing.Point(136, 100)
$btnScan.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnScan.add_Click({ Scan-Ports })
$script:portPanel.Controls.Add($btnScan)

$btnKill = New-Object System.Windows.Forms.Button
$btnKill.Text = "清理占用"
$btnKill.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9, [System.Drawing.FontStyle]::Bold)
$btnKill.ForeColor = $FG
$btnKill.BackColor = $BTN
$btnKill.FlatStyle = "Flat"
$btnKill.FlatAppearance.BorderSize = 0
$btnKill.Size = New-Object System.Drawing.Size(74, 26)
$btnKill.Location = New-Object System.Drawing.Point(214, 100)
$btnKill.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnKill.add_Click({ Kill-Occupiers })
$script:portPanel.Controls.Add($btnKill)

# 按钮行/底部随网卡区下移
$script:btnY = 332 + $script:nicH + 8 + 130 + 10
$script:footY = $script:btnY + 52

# ---- 按钮 ----
function New-Btn($text, $x, $w, $bg, $fg, $handler) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 11, [System.Drawing.FontStyle]::Bold)
    $b.ForeColor = $fg
    $b.BackColor = $bg
    $b.FlatStyle = "Flat"
    $b.FlatAppearance.BorderSize = 0
    $b.Size = New-Object System.Drawing.Size($w, 42)
    $b.Location = New-Object System.Drawing.Point($x, 0)
    $b.Cursor = [System.Windows.Forms.Cursors]::Hand
    $b.add_Click($handler)
    return $b
}

$script:svcPid = $null
$script:rclonePid = $null
$script:lastErr = ""

# 服务存活探测（快速 TCP，500ms 超时，避免阻塞界面）
function Test-Service {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $c.BeginConnect("127.0.0.1", $cfg.port, $null, $null)
        $ok = $ar.AsyncWaitHandle.WaitOne(500)
        if ($ok) { $c.EndConnect($ar) }
        return $ok
    } catch { return $false }
    finally { $c.Close() }
}

function Update-State {
    $running = Test-Service
    if ($running) {
        $lblDot.ForeColor = $GREEN
        $lblState.Text = "服务运行中：HTTP/TFTP/DHCP/RCLONE"
        $lblUrl.Text = $script:ManageUrl
        $lblUrl.Visible = $true
        $btnOpen.Text = "  启动并打开  "
    } else {
        $lblDot.ForeColor = $RED
        $lblState.Text = "服务未运行"
        $lblUrl.Visible = $false
        $btnOpen.Text = "  启动并打开  "
    }
    # 服务运行中禁用网卡选择/DHCP模式选择（避免运行中切换），停止后可切换
    if ($script:nicBox) { $script:nicBox.Enabled = -not $running }
    if ($script:dhcpModeBox) { $script:dhcpModeBox.Enabled = -not $running }
}

# 解析可用 python.exe：优先项目内置运行时，其次系统 PATH（跳过 Alias/WindowsApps stub）
function Resolve-PythonExe {
    $embedded = Join-Path $Base "runtime\python\python.exe"
    if (Test-Path $embedded) { return $embedded }
    $g = Get-Command python -All -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandType -eq "Application" -and $_.Source -and
                        $_.Source -notlike "*WindowsApps*" } |
         Select-Object -First 1
    if ($g) { return $g.Source }
    return $null
}
$script:pythonExe = Resolve-PythonExe

function Start-Service {
    if (Test-Service) {
        # 主服务已运行：若 8081 后端未监听则补启动（供 PE 内 Z 盘映射）
        $r8081 = @(Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue).Count
        if ($r8081 -eq 0 -and $script:pythonExe) {
            $logDir2 = Join-Path $Base "tools"
            New-Item -ItemType Directory -Force $logDir2 | Out-Null
            try {
                $rp = Start-Process -FilePath $script:pythonExe -ArgumentList @("http_server.py", "8081") `
                     -WorkingDirectory $Base -WindowStyle Hidden -PassThru `
                     -RedirectStandardOutput (Join-Path $logDir2 "rclone-svc.log") `
                     -RedirectStandardError (Join-Path $logDir2 "rclone-svc.err")
                $script:rclonePid = $rp.Id
            } catch {}
        }
        return $true
    }
    if (-not $script:pythonExe) { $script:lastErr = "未找到可用的 Python"; return $false }
    $logDir = Join-Path $Base "tools"
    New-Item -ItemType Directory -Force $logDir | Out-Null
    $outLog = Join-Path $logDir "service.log"
    $errLog = Join-Path $logDir "service.err"
    Remove-Item $outLog, $errLog -Force -ErrorAction SilentlyContinue
    try {
        $p = Start-Process -FilePath $script:pythonExe -ArgumentList @("main.py", "--no-browser", "--quiet") `
             -WorkingDirectory $Base -WindowStyle Hidden -PassThru `
             -RedirectStandardOutput $outLog -RedirectStandardError $errLog
        $script:svcPid = $p.Id
        # 同时启动 RCLONE 后端（8081），供 PE 内 Z 盘映射使用
        $rOut = Join-Path $logDir "rclone-svc.log"
        $rErr = Join-Path $logDir "rclone-svc.err"
        try {
            $rp = Start-Process -FilePath $script:pythonExe -ArgumentList @("http_server.py", "8081") `
                 -WorkingDirectory $Base -WindowStyle Hidden -PassThru `
                 -RedirectStandardOutput $rOut -RedirectStandardError $rErr
            $script:rclonePid = $rp.Id
        } catch {}
    } catch {
        $script:lastErr = "启动进程失败：$($_.Exception.Message)"
        return $false
    }
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Service) { return $true }
        if ($p.HasExited) {
            $err = ""
            if (Test-Path $errLog) { $err = (Get-Content $errLog -Raw -Encoding UTF8 -ErrorAction SilentlyContinue).Trim() }
            $script:lastErr = "服务进程退出(code=$($p.ExitCode))：$err"
            return $false
        }
    }
    $script:lastErr = "服务启动超时（未在 15 秒内就绪）"
    return $false
}

function Stop-Service {
    if ($script:svcPid) {
        try { Stop-Process -Id $script:svcPid -Force -ErrorAction SilentlyContinue } catch {}
        $script:svcPid = $null
    }
    if ($script:rclonePid) {
        try { Stop-Process -Id $script:rclonePid -Force -ErrorAction SilentlyContinue } catch {}
        $script:rclonePid = $null
    }
}

# 停止本项目：按 67/69/HTTP/8081 端口找到 main.py / http_server.py 进程并停止
function Stop-Project {
    $pids = @()
    foreach ($c in @(@{P = "UDP"; Port = 67}, @{P = "UDP"; Port = 69}, @{P = "TCP"; Port = $cfg.port}, @{P = "TCP"; Port = 8081})) {
        if ($c.P -eq "UDP") {
            $pids += @(Get-NetUDPEndpoint -LocalPort $c.Port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
        } else {
            $pids += @(Get-NetTCPConnection -LocalPort $c.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
        }
    }
    $killed = 0
    foreach ($id in ($pids | Select-Object -Unique)) {
        if (Test-SvcProcess $id) {
            try { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; $killed++ } catch {}
        }
    }
    $script:svcPid = $null
    $script:rclonePid = $null
    Update-State
    $lblState.Text = if ($killed -gt 0) { "服务已停止" } else { "服务未运行" }
    $lblDot.ForeColor = $RED
}

# 停止所有占用 HTTP 端口（默认 8080）的监听进程
function Stop-PortUsers {
    $pids = Get-NetTCPConnection -LocalPort $cfg.port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($id in $pids) {
        try { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue } catch {}
    }
    return @($pids).Count
}

# 切换启动网卡：写 config.json 的 server_ip；服务在运行则重启生效
# 同时把 DHCP 地址池同步到新网卡子网（.100-.200），网关置空避免下发错误路由
function Add-IpInt($ipStr, $n) {
    $b = [System.Net.IPAddress]::Parse($ipStr).GetAddressBytes()
    [Array]::Reverse($b)
    $v = [System.BitConverter]::ToUInt32($b, 0) + $n
    $b2 = [System.BitConverter]::GetBytes([uint32]$v)
    [Array]::Reverse($b2)
    return [System.Net.IPAddress]::new($b2).ToString()
}

function Get-SubnetPool($ip, $maskStr) {
    $ipb = [System.Net.IPAddress]::Parse($ip).GetAddressBytes()
    $mkb = [System.Net.IPAddress]::Parse($maskStr).GetAddressBytes()
    $netb = New-Object byte[] 4
    for ($i = 0; $i -lt 4; $i++) { $netb[$i] = $ipb[$i] -band $mkb[$i] }
    $netIp = [System.Net.IPAddress]::new($netb).ToString()
    $prefix = 0
    foreach ($byte in $mkb) { for ($i = 7; $i -ge 0; $i--) { if (($byte -shr $i) -band 1) { $prefix++ } } }
    $hosts = [math]::Pow(2, 32 - $prefix) - 2
    $startN = [int][math]::Min(100, $hosts - 2)
    $endN = [int][math]::Min(200, $hosts - 2)
    if ($endN -lt $startN) { $startN = 10; $endN = [int][math]::Min(200, $hosts - 2); if ($endN -lt $startN) { $startN = 2; $endN = [int]$hosts } }
    return @((Add-IpInt $netIp $startN), (Add-IpInt $netIp $endN))
}

function Set-Nic($ip) {
    if (-not $ip -or $ip -eq $cfg.ip) { return }
    try {
        $d = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch { $d = [PSCustomObject]@{} }
    $d | Add-Member -NotePropertyName server_ip -NotePropertyValue $ip -Force
    # 地址池同步到新网卡子网
    $maskStr = if ($d.netmask) { [string]$d.netmask } else { "255.255.255.0" }
    $pool = Get-SubnetPool $ip $maskStr
    $d | Add-Member -NotePropertyName ip_pool -NotePropertyValue ([PSCustomObject]@{ start = $pool[0]; end = $pool[1] }) -Force
    $d | Add-Member -NotePropertyName gateway -NotePropertyValue "" -Force
    $json = $d | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding $false))
    $cfg.ip = $ip
    $script:ManageUrl = "http://$($cfg.ip):$($cfg.port)/"
    $lblUrl.Text = $script:ManageUrl
    # 只修改配置，不自动启动服务（点"打开链接"手动启动）
    if (Test-Service) { $lblState.Text = "已切换网卡 $ip，地址池 $($pool[0])-$($pool[1])（服务运行中，重启后生效）" }
    else { $lblState.Text = "已切换网卡 $ip，地址池 $($pool[0])-$($pool[1])（点启动并打开）" }
    $lblDot.ForeColor = $DIM
}

# 应用：HTTP 端口 + DHCP 模式 → 写回 config.json（TFTP/DHCP 为 PXE 协议固定端口，固定写 69/67）
function Apply-Ports {
    $hp = 0
    if (-not [int]::TryParse($script:txtHttp.Text.Trim(), [ref]$hp)) { $lblState.Text = "HTTP 端口须为整数"; $lblDot.ForeColor = $RED; return }
    if ($hp -lt 1 -or $hp -gt 65535) { $lblState.Text = "HTTP 端口须在 1-65535 之间"; $lblDot.ForeColor = $RED; return }
    # DHCP 模式随"应用"一并写入（与下拉框当前选择同步）
    $sel = $script:dhcpModeBox.SelectedItem
    $mode = if ($sel -and $script:dhcpModeMap.ContainsKey($sel)) { $script:dhcpModeMap[$sel] } else { $cfg.dhcpMode }
    $modeChanged = ($mode -ne $cfg.dhcpMode)
    try { $d = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $d = [PSCustomObject]@{} }
    $d | Add-Member -NotePropertyName http_port -NotePropertyValue $hp -Force
    $d | Add-Member -NotePropertyName tftp_port -NotePropertyValue 69 -Force
    $d | Add-Member -NotePropertyName dhcp_port -NotePropertyValue 67 -Force
    if ($modeChanged) { $d | Add-Member -NotePropertyName dhcp_mode -NotePropertyValue $mode -Force }
    $json = $d | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding $false))
    $cfg.port = $hp; $cfg.tftp = 69; $cfg.dhcp = 67
    if ($modeChanged) { $cfg.dhcpMode = $mode }
    $script:ManageUrl = "http://$($cfg.ip):$($cfg.port)/"
    $lblUrl.Text = $script:ManageUrl
    $lblPorts.Text = "HTTP :$($cfg.port)   TFTP :$($cfg.tftp)   DHCP :$($cfg.dhcp)"
    $changed = @("HTTP 端口已更新为 $hp")
    if ($modeChanged) { $changed += "DHCP 模式已切换为 $sel" }
    $msg = $changed -join "，"
    # 只修改配置，不自动启动服务（点"打开链接"手动启动）
    if (Test-Service) { $lblState.Text = "$msg（服务运行中，重启后生效）" }
    else { $lblState.Text = "$msg（点启动并打开）" }
    $lblDot.ForeColor = $DIM
}

# 判断进程是否为本服务（main.py / http_server.py 后端 8081），本服务不列为占用者
function Test-SvcProcess($procId) {
    if ($procId -eq $script:svcPid) { return $true }
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue).CommandLine
        if ($cmd -and ($cmd -match "main\.py" -or $cmd -match "http_server\.py")) { return $true }
    } catch {}
    return $false
}

# 端口占用检测：检查 DHCP(67)/TFTP(69)/HTTP(当前端口)/RCLONE后端(8081) 的占用情况
function Scan-Ports {
    $lines = @()
    $script:occupiers = @()
    $checks = @(
        @{ Proto = "UDP"; Port = 67;  Name = "DHCP"; AlwaysKill = $true },
        @{ Proto = "UDP"; Port = 69;  Name = "TFTP"; AlwaysKill = $true },
        @{ Proto = "TCP"; Port = $cfg.port; Name = "HTTP"; AlwaysKill = $true },
        @{ Proto = "TCP"; Port = 8081; Name = "RCLONE"; AlwaysKill = $true }
    )
    foreach ($c in $checks) {
        $pids = @()
        if ($c.Proto -eq "UDP") {
            $pids = @(Get-NetUDPEndpoint -LocalPort $c.Port -ErrorAction SilentlyContinue |
                      Select-Object -ExpandProperty OwningProcess -Unique)
        } else {
            $pids = @(Get-NetTCPConnection -LocalPort $c.Port -State Listen -ErrorAction SilentlyContinue |
                      Select-Object -ExpandProperty OwningProcess -Unique)
        }
        if ($pids.Count -eq 0) {
            $lines += "$($c.Name)（$($c.Proto) $($c.Port)）：空闲"
        } else {
            $desc = @()
            foreach ($procId in $pids) {
                if ((-not $c.AlwaysKill) -and (Test-SvcProcess $procId)) {
                    $desc += "$procId（本服务）"
                } else {
                    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
                    $nm = if ($proc) { $proc.ProcessName } else { "未知" }
                    $desc += "$procId（$nm）"
                    $script:occupiers += $procId
                }
            }
            $lines += "$($c.Name)（$($c.Proto) $($c.Port)）：被 $($desc -join '、') 占用"
        }
    }
    $script:occupiers = @($script:occupiers | Select-Object -Unique)
    [System.Windows.Forms.MessageBox]::Show(($lines -join "`n"), "端口占用检测",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
}

# 一键清理占用 67/69/HTTP 端口的进程（本服务除外），需确认
function Kill-Occupiers {
    if (-not $script:occupiers -or $script:occupiers.Count -eq 0) {
        [System.Windows.Forms.MessageBox]::Show("没有检测到需要清理的占用进程。`n请先点端口检测。", "清理占用",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
        return
    }
    $names = @($script:occupiers | ForEach-Object {
        $proc = Get-Process -Id $_ -ErrorAction SilentlyContinue
        if ($proc) { "$_（$($proc.ProcessName)）" } else { "$_（已退出）" }
    }) -join "`n"
    $r = [System.Windows.Forms.MessageBox]::Show("将强制停止以下占用进程：`n$names`n`n确定？", "确认清理",
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    foreach ($procId in $script:occupiers) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    $script:occupiers = @()
    $lblState.Text = "已清理占用进程，请点打开链接重启服务"
    $lblDot.ForeColor = $DIM
}
if ($SelfTest) {
    $before = Test-Service
    $started = Start-Service
    Start-Sleep -Milliseconds 500
    $after = Test-Service
    $listeners = @(Get-NetTCPConnection -LocalPort $cfg.port -State Listen -ErrorAction SilentlyContinue).Count
    $stopped = Stop-PortUsers
    Stop-Service
    Write-Output "selftest: py=$script:pythonExe before=$before started=$started after_start=$after listeners=$listeners stopped=$stopped lastErr=$script:lastErr url=$ManageUrl"
    exit 0
}

# 打开链接（同步启动：快速 TCP 探测，1-2 秒即响应，最长 5 秒）
$btnOpen = New-Btn "  启动并打开  " 40 160 $GREEN ([System.Drawing.Color]::FromArgb(6, 43, 20)) {
    if (Test-Service) { Start-Process $ManageUrl; return }
    if (-not $script:pythonExe) {
        $lblState.Text = "未找到可用的 Python，无法启动服务"
        $lblDot.ForeColor = $RED
        return
    }
    $lblState.Text = "正在启动服务..."
    $lblDot.ForeColor = $DIM
    $lblState.Refresh()
    $ok = Start-Service
    if ($ok) {
        Update-State
        Start-Process $ManageUrl
    } else {
        $lblState.Text = "启动失败：$($script:lastErr)"
        $lblDot.ForeColor = $RED
    }
}

# 最小化
$btnMin = New-Btn "  最小化  " 210 72 $BTN $FG { $form.WindowState = "Minimized" }
$btnMin.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10)

# 退出：停止自己启动的服务 + 停止所有占用 8080 端口的进程
$btnExit = New-Btn "  退出  " 292 88 ([System.Drawing.Color]::FromArgb(58, 35, 35)) ([System.Drawing.Color]::White) {
    Stop-Service
    $n = Stop-PortUsers
    $lblState.Text = "已停止 $n 个占用进程"
    $form.Close()
}
$btnExit.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10)

$row = New-Object System.Windows.Forms.Panel
$row.Size = New-Object System.Drawing.Size(420, 42)
$row.Location = New-Object System.Drawing.Point(0, $script:btnY)
$row.BackColor = $BG
$row.Controls.Add($btnOpen)
$row.Controls.Add($btnMin)
$row.Controls.Add($btnExit)
$form.Controls.Add($row)

# 底部
$lblFoot = New-Object System.Windows.Forms.Label
$lblFoot.Text = "www.pxe-boot.local"
$lblFoot.Font = New-Object System.Drawing.Font("Consolas", 8)
$lblFoot.ForeColor = [System.Drawing.Color]::FromArgb(85, 85, 102)
$lblFoot.BackColor = $BG
$lblFoot.Size = New-Object System.Drawing.Size(420, 18)
$lblFoot.Location = New-Object System.Drawing.Point(0, $script:footY)
$lblFoot.TextAlign = "MiddleCenter"
$form.Controls.Add($lblFoot)

# 窗口高度随网卡区动态调整
$finalH = [int]$script:footY + 30
$form.ClientSize = New-Object System.Drawing.Size -ArgumentList @(420, $finalH)

# 定时刷新状态（TCP 探测快速失败，不卡界面）
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 3000
$timer.add_Tick({ Update-State })
$timer.Start()

$form.Add_Shown({
    # 强制定位到主屏幕中心（多显示器时避免落到副屏）
    $scr = [System.Windows.Forms.Screen]::PrimaryScreen
    $x = $scr.Bounds.X + [math]::Max(0, ($scr.Bounds.Width - $form.Width) / 2)
    $y = $scr.Bounds.Y + [math]::Max(0, ($scr.Bounds.Height - $form.Height) / 2)
    $form.Location = New-Object System.Drawing.Point([int]$x, [int]$y)
    # 默认选中当前 server_ip 所在网卡；没有则选第一个
    if ($script:nicCount -gt 0) {
        $script:nicApplying = $true
        $idx = 0
        for ($i = 0; $i -lt $script:nicBox.Items.Count; $i++) {
            if ($script:nicIpMap[$script:nicBox.Items[$i]] -eq $cfg.ip) { $idx = $i; break }
        }
        $script:nicBox.SelectedIndex = $idx
        $script:nicApplying = $false
    }
    Update-State
    if ($AutoStart) { $btnOpen.PerformClick() }
})
[System.Windows.Forms.Application]::Run($form)
