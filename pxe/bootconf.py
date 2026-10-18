# -*- coding: utf-8 -*-
"""
ISO 类型识别（bootconf）：
- 解析 ISO 文件系统索引，自动识别系统类型（Ubuntu/Debian/RedHat/Windows 等）与引导文件；
- 维护 boot_conf.json 覆盖配置，供用户手动修正自动识别结果；
- 维护 ISO 注册表（扫描 iso_dirs 下的 ISO 文件，供 Web/HTTP/菜单使用）。
"""
import json
import os
import time

from . import iso9660
from .config import path_of

BOOT_CONF_PATH = path_of("boot_conf.json")


def _find(idx, *patterns):
    """按前缀/精确匹配在文件索引中查找，返回第一个命中（小写路径）。"""
    for pat in patterns:
        if pat in idx:
            return pat
    return None


def _find_suffix(idx, *suffixes):
    """按后缀匹配，返回体积最大的命中（如 *.wim / *.sdi 主文件通常最大）。"""
    hits = [k for k in idx if k.endswith(suffixes)]
    if not hits:
        return None
    hits.sort(key=lambda k: idx[k]["size"], reverse=True)
    return hits[0]


def detect_iso(path):
    """自动识别单个 ISO 的类型与引导配置。

    返回 dict：os_type / kernel / initrd / args 模板 / wimboot 文件列表 / 说明。
    """
    idx = iso9660.read_iso_index(path)
    if not idx:
        return {"os_type": "unknown", "reason": "无法读取文件系统（可能是加密或非常规镜像）"}

    has = lambda p: p in idx
    starts = lambda prefix: any(k.startswith(prefix) for k in idx)

    # ---- Windows（UDF / 自定义 PE）----
    wim = _find(idx, "sources/boot.wim") or _find_suffix(idx, ".wim")
    if has("bootmgr") and wim:
        sdi = _find(idx, "boot/boot.sdi") or _find_suffix(idx, ".sdi")
        uefi_boot = _find(idx,
                          "efi/microsoft/boot/bootmgfw.efi", "efi/boot/bootx64.efi",
                          "efi/boot/bootia32.efi", "bootmgr.efi")
        bcd = _find(idx, "efi/microsoft/boot/bcd", "boot/bcd", "boot/bcd.efi")
        wimboot_files = ["bootmgr"]
        for extra in (bcd, sdi, uefi_boot, wim):
            if extra and extra not in wimboot_files:
                wimboot_files.append(extra)
        standard = wim == "sources/boot.wim"
        return {
            "os_type": "windows",
            "wimboot_files": wimboot_files,
            "wim_efi_boot": uefi_boot or "efi/microsoft/boot/bootmgfw.efi",
            "wim_bcd": bcd or "boot/bcd",
            "wim_sdi": sdi or "boot/boot.sdi",
            "wim": wim,
            "wim_size": int(idx.get(wim, {}).get("size", 0)),
            "args": "",
            "reason": ("检测到 bootmgr + boot.wim（Windows 安装镜像）" if standard
                       else "检测到 bootmgr + WIM（自定义 Windows PE，如微PE）"),
        }

    # ---- Ubuntu / 衍生版 live（casper）----
    if has("casper/vmlinuz"):
        initrd = _find(idx, "casper/initrd", "casper/initrd.img", "casper/initrd.gz")
        if initrd:
            return {
                "os_type": "ubuntu",
                "kernel": "casper/vmlinuz",
                "initrd": initrd,
                "args": "boot=casper ip=dhcp url={iso_url} quiet splash",
                "reason": "检测到 casper/vmlinuz（Ubuntu 桌面/服务器 Live）",
            }

    # ---- Debian Live ----
    if has("live/vmlinuz") and has("live/initrd.img"):
        return {
            "os_type": "debian_live",
            "kernel": "live/vmlinuz",
            "initrd": "live/initrd.img",
            "args": "boot=live fetch={iso_url} quiet splash",
            "reason": "检测到 live/vmlinuz（Debian Live）",
        }

    # ---- RedHat 系（images/pxeboot）----
    if has("images/pxeboot/vmlinuz") and has("images/pxeboot/initrd.img"):
        return {
            "os_type": "redhat",
            "kernel": "images/pxeboot/vmlinuz",
            "initrd": "images/pxeboot/initrd.img",
            "args": "ip=dhcp inst.repo={iso_url} quiet",
            "reason": "检测到 images/pxeboot（Fedora/RHEL/CentOS 安装器）",
        }

    # ---- Debian / Ubuntu netinst（install/vmlinuz）----
    if has("install/vmlinuz"):
        initrd = _find(idx, "install/initrd.gz", "install/initrd.img", "install/initrd")
        if initrd:
            return {
                "os_type": "debian_installer",
                "kernel": "install/vmlinuz",
                "initrd": initrd,
                "args": "ip=dhcp",
                "reason": "检测到 install/vmlinuz（Debian/Ubuntu 网络安装器）",
            }

    # ---- 通用：任意两层的 vmlinuz + initrd 组合 ----
    kernels = sorted(k for k in idx if k.split("/")[-1].startswith("vmlinuz"))
    inits = sorted(k for k in idx if k.split("/")[-1].startswith("initrd"))
    for kern in kernels:
        base_dir = kern.rsplit("/", 1)[0]
        for init in inits:
            if init.startswith(base_dir):
                return {
                    "os_type": "generic_linux",
                    "kernel": kern,
                    "initrd": init,
                    "args": "{iso_url}",
                    "reason": f"通用检测：{kern} + {init}",
                }

    return {"os_type": "unknown", "reason": "未识别到常见引导结构（可手动覆盖）"}


# ------------------------------------------------------------------ 覆盖配置 --

class BootConf:
    """boot_conf.json 管理：ISO 级覆盖配置。"""

    def __init__(self, path=BOOT_CONF_PATH):
        self.path = path
        self.data = {"overrides": {}}
        self.load()

    def load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("overrides"), dict):
                    self.data = loaded
            except (OSError, json.JSONDecodeError):
                pass

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get(self, iso_name):
        return self.data["overrides"].get(iso_name) or {}

    def set(self, iso_name, override):
        self.data["overrides"][iso_name] = override
        self.save()

    def remove(self, iso_name):
        self.data["overrides"].pop(iso_name, None)
        self.save()

    def merge(self, iso_name, detected):
        """检测结果 + 用户覆盖 = 最终引导配置。"""
        ov = self.get(iso_name)
        conf = dict(detected)
        conf.update({k: v for k, v in ov.items() if v not in (None, "")})
        return conf


# ------------------------------------------------------------------ ISO 注册表 --

class IsoRegistry:
    """扫描 iso_dirs，维护 ISO 列表与检测缓存。"""

    def __init__(self, config, bootconf=None):
        self.config = config
        self.bootconf = bootconf or BootConf()
        self._cache = {}   # path -> (mtime, size, detected)
        self._scan_time = 0

    def scan(self, force=False):
        now = time.time()
        if not force and now - self._scan_time < 3:
            return
        self._scan_time = now
        for d in self.config.iso_dirs:
            if not os.path.isdir(d):
                continue
            for root, dirs, files in os.walk(d):
                for fn in sorted(files):
                    if not fn.lower().endswith((".iso", ".img")):
                        continue
                    full = os.path.join(root, fn)
                    self._ensure(full)
        # 移除已删除的
        for p in list(self._cache):
            if not os.path.isfile(p):
                del self._cache[p]

    def _ensure(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return
        cached = self._cache.get(path)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return
        detected = detect_iso(path)
        self._cache[path] = (st.st_mtime, st.st_size, detected)

    def entries(self, force=False):
        self.scan(force=force)
        out = []
        for d in self.config.iso_dirs:
            base = os.path.abspath(d)
            if not os.path.isdir(base):
                continue
            for path, (mtime, size, detected) in sorted(self._cache.items(),
                                                        key=lambda kv: kv[0].lower()):
                if not os.path.abspath(path).startswith(base):
                    continue
                rel = os.path.relpath(path, base).replace("\\", "/")
                name = os.path.basename(path)
                url = "/iso/" + rel
                group = os.path.dirname(rel)
                conf = self.bootconf.merge(name, detected)
                out.append({
                    "name": name,
                    "path": path,
                    "url": url,
                    "group": group,
                    "size": size,
                    "mtime": mtime,
                    "detected": detected,
                    "conf": conf,
                })
        # 固定 PE 条目：boot 目录 BOOT.WIM（不依赖 isos ISO / boot_files 缓存）
        pe = self.config.data.get("pe_wim") or {}
        if pe.get("enabled", True):
            wim_name = os.path.basename(pe.get("wim", "boot/BOOT.WIM"))
            wim_path = os.path.join(self.config.boot_dir, wim_name)
            if os.path.isfile(wim_path):
                out.insert(0, {
                    "name": pe.get("label", "PE 维护系统"),
                    "path": wim_path,
                    "url": "/boot/" + wim_name,
                    "group": "",
                    "size": os.path.getsize(wim_path),
                    "mtime": os.path.getmtime(wim_path),
                    "detected": {"os_type": "windows_pe",
                                 "reason": "boot 目录固定 PE（BOOT.WIM，无需 ISO）"},
                    "conf": {
                        "os_type": "windows_pe",
                        "label": pe.get("label", "PE 维护系统"),
                        "wim": pe.get("wim", "boot/BOOT.WIM"),
                        "wim_efi_boot": pe.get("efi_boot", "boot/bootmgfw.efi"),
                        "wim_bcd": pe.get("bcd", "boot/BCD"),
                        "wim_sdi": pe.get("sdi", "boot/boot.sdi"),
                    },
                })
        return out

    def resolve_iso(self, url_path):
        """将 /iso/xxx 映射到磁盘文件路径。"""
        rel = url_path.lstrip("/")
        for d in self.config.iso_dirs:
            base = os.path.abspath(d)
            cand = os.path.abspath(os.path.join(base, rel))
            if cand.startswith(base + os.sep) and os.path.isfile(cand):
                return cand
        return None


if __name__ == "__main__":
    import sys
    from .config import Config
    cfg = Config()
    reg = IsoRegistry(cfg)
    for e in reg.entries(force=True):
        print(e["name"], "->", e["conf"].get("os_type"), e["conf"].get("reason", ""))
