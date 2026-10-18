# -*- coding: utf-8 -*-
"""
引导文件自动获取脚本（纯 Python 标准库，Windows/Linux 均可用）。

下载到 boot/ 目录：
  grubx64.efi     UEFI x86-64 GRUB2（Debian 官方签名版 grubnetx64.efi.signed）
  grubia32.efi    UEFI IA32 GRUB2（可选）
  grubaa64.efi    UEFI ARM64 GRUB2（可选）
  pxelinux.0       BIOS PXELINUX（syslinux 官方）
  ldlinux.c32 / menu.c32 / libutil.c32 / libcom32.c32
  memdisk         通用 ISO 引导（syslinux memdisk）
  wimboot         Windows ISO 引导（iPXE 官方）
  undionly.kpxe   iPXE BIOS（可选）
  ipxe.efi        iPXE UEFI（可选）

用法：
  python tools\\fetch_bootloader.py            # 下载全部
  python tools\\fetch_bootloader.py --skip wimboot,grubx64.efi
"""
import argparse
import io
import os
import re
import struct
import sys
import tarfile
import urllib.request

BOOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "boot")

GRUB_AMD64_POOL = "http://ftp.debian.org/debian/pool/main/g/grub-efi-amd64-signed/"
GRUB_IA32_POOL = "http://ftp.debian.org/debian/pool/main/g/grub-efi-ia32-signed/"
GRUB_ARM64_POOL = "http://ftp.debian.org/debian/pool/main/g/grub-efi-arm64-signed/"
DEB_MIRROR = "https://deb.debian.org/debian/pool/main/g/"

WIMBOOT_URL = "https://github.com/ipxe/wimboot/releases/latest/download/wimboot"
IPXE_UNDIONLY = "https://boot.ipxe.org/undionly.kpxe"
IPXE_EFI_X64 = "https://boot.ipxe.org/x86_64-efi/ipxe.efi"
IPXE_EFI_IA32 = "https://boot.ipxe.org/i386-efi/ipxe.efi"
IPXE_EFI_ARM64 = "https://boot.ipxe.org/arm64-efi/ipxe.efi"
SYSLINUX_URL = "https://www.kernel.org/pub/linux/utils/boot/syslinux/syslinux-6.03.tar.gz"

UA = {"User-Agent": "Mozilla/5.0 PXE-BootServer fetch"}


def log(msg):
    print(msg, flush=True)


def fetch(url, timeout=60):
    log(f"  下载 {url}")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def pool_latest_deb(pool_url, mirror=True):
    """从 Debian pool 目录索引中解析最新 .deb 文件名（amd64/i386/arm64 通用）。"""
    for base in ([pool_url] + ([DEB_MIRROR + pool_url.rsplit("/", 2)[-2] + "/"] if mirror else [])):
        try:
            idx = fetch(base, timeout=30).decode("utf-8", "replace")
        except Exception as e:
            log(f"  (索引获取失败 {base}: {e})")
            continue
        m = re.findall(r'href="([^"]*\.deb)"', idx)
        if m:
            return base + m[-1]
    raise RuntimeError("无法解析 Debian pool 索引")


def extract_deb_grub(deb_data, member_prefix):
    """从 .deb (ar) 中提取 data.tar.xz/gz 并解出指定路径的 grub EFI。"""
    if not deb_data.startswith(b"!<arch>\n"):
        raise RuntimeError("不是有效的 ar 归档")
    data_member = None
    pos = 8
    while pos < len(deb_data):
        name = deb_data[pos:pos + 16].decode("ascii", "replace").rstrip(" \x00/")
        size = int(deb_data[pos + 48:pos + 58].decode("ascii", "replace").strip())
        data_start = pos + 60
        data = deb_data[data_start:data_start + size]
        if name in ("data.tar.xz", "data.tar.gz", "data.tar"):
            data_member = data
            break
        pos = data_start + size + (size % 2)
    if data_member is None:
        raise RuntimeError(".deb 中未找到 data.tar 成员")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data_member), mode="r:*")
    except tarfile.TarError as e:
        raise RuntimeError(f"解包 data.tar 失败: {e}")
    for m in tf.getmembers():
        if m.name.endswith(member_prefix) and m.isfile():
            return tf.extractfile(m).read()
    return None


def check_efi(data, label, min_size=100_000):
    ok = data[:2] == b"MZ" and len(data) > min_size
    log(f"    {label}: {'OK' if ok else '警告: 文件可能无效'} ({len(data)} bytes)")
    return ok


def main():
    ap = argparse.ArgumentParser(description="下载 PXE 引导文件")
    ap.add_argument("--skip", default="", help="逗号分隔的跳过列表，如 grubx64.efi,wimboot")
    ap.add_argument("--only", default="", help="只下载逗号分隔的指定文件")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    os.makedirs(BOOT_DIR, exist_ok=True)
    skip = set(args.skip.split(",")) if args.skip else set()
    only = set(args.only.split(",")) if args.only else None
    wanted = lambda name: (only is None or name in only) and name not in skip

    grub_inner = {
        "grubx64.efi": "x86_64-efi-signed/grubnetx64.efi.signed",
        "grubia32.efi": "i386-efi-signed/grubnetia32.efi.signed",
        "grubaa64.efi": "arm64-efi-signed/grubnetaa64.efi.signed",
    }
    grub_pool = {
        "grubx64.efi": GRUB_AMD64_POOL,
        "grubia32.efi": GRUB_IA32_POOL,
        "grubaa64.efi": GRUB_ARM64_POOL,
    }

    # ---- GRUB EFI（从 Debian .deb 提取签名版）----
    for name, inner in grub_inner.items():
        if not wanted(name):
            continue
        log(f"[1/7] 获取 {name} (Debian 签名 GRUB)")
        try:
            deb_url = pool_latest_deb(grub_pool[name])
            deb = fetch(deb_url, timeout=args.timeout)
            efidata = extract_deb_grub(deb, inner)
            if efidata is None:
                raise RuntimeError(f"未在 .deb 中找到 {inner}")
            if not check_efi(efidata, name):
                continue
            with open(os.path.join(BOOT_DIR, name), "wb") as f:
                f.write(efidata)
            log(f"    已保存 {name}（来源: {deb_url}）")
        except Exception as e:
            log(f"    失败: {e}（可改用 tools/build_grub.sh 在 Linux 上自行构建）")

    # ---- wimboot ----
    if wanted("wimboot"):
        log("[2/7] 获取 wimboot (iPXE 官方)")
        try:
            data = fetch(WIMBOOT_URL, timeout=args.timeout)
            # wimboot 是混合二进制（BIOS+UEFI），体积较小，仅做基本校验
            ok = len(data) > 4096 and (data[:2] == b"MZ" or data[:4] == b"\x7fELF")
            log(f"    wimboot: {'OK' if ok else '警告: 文件可能无效'} ({len(data)} bytes)")
            with open(os.path.join(BOOT_DIR, "wimboot"), "wb") as f:
                f.write(data)
            log("    已保存 wimboot")
        except Exception as e:
            log(f"    失败: {e}")

    # ---- iPXE：BIOS 链式引导（undionly.kpxe）与 UEFI 引导（ipxe*.efi，Windows 路径必需）----
    ipxe_files = [
        ("undionly.kpxe", IPXE_UNDIONLY, 40_000, b"\xea"),          # NBP（0xEA 跳转头）
        ("ipxe.efi", IPXE_EFI_X64, 100_000, b"MZ"),
        ("ipxeia32.efi", IPXE_EFI_IA32, 100_000, b"MZ"),
        ("ipxea64.efi", IPXE_EFI_ARM64, 100_000, b"MZ"),
    ]
    for name, url, min_size, magic in ipxe_files:
        if not wanted(name):
            continue
        log(f"[3/7] 获取 {name} (iPXE 官方)")
        try:
            data = fetch(url, timeout=args.timeout)
            ok = len(data) > min_size and data[: len(magic)] == magic
            log(f"    {name}: {'OK' if ok else '警告: 文件可能无效'} ({len(data)} bytes)")
            with open(os.path.join(BOOT_DIR, name), "wb") as f:
                f.write(data)
        except Exception as e:
            log(f"    失败: {e}")

    # ---- syslinux（pxelinux / memdisk）----
    if any(wanted(n) for n in ("pxelinux.0", "memdisk", "ldlinux.c32",
                               "menu.c32", "libutil.c32", "libcom32.c32")):
        log("[4/7] 获取 syslinux 6.03 (kernel.org)")
        try:
            data = fetch(SYSLINUX_URL, timeout=args.timeout)
            tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
            targets = {}
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                base = m.name.rsplit("/", 1)[-1]
                if base in ("pxelinux.0", "ldlinux.c32", "menu.c32", "libutil.c32",
                            "libcom32.c32", "memdisk"):
                    # 优先 BIOS 构建（pxelinux 需要 bios 版本，不能用 efi32/efi64 的）
                    is_bios = m.name.startswith(("syslinux-6.03/bios/", "syslinux-6.03/memdisk/"))
                    cur = targets.get(base)
                    if cur is None or is_bios:
                        targets[base] = m.name
            for base, member in targets.items():
                if not wanted(base):
                    continue
                content = tf.extractfile(member).read()
                with open(os.path.join(BOOT_DIR, base), "wb") as f:
                    f.write(content)
                log(f"    已保存 {base} ({member})")
        except Exception as e:
            log(f"    失败: {e}")

    log("完成。缺失的可选文件不影响 UEFI 主路径。")

    # 生成来源说明
    readme = """本目录中的引导文件均为开源软件，来源如下：

  grubx64.efi / grubia32.efi / grubaa64.efi
      Debian 官方签名 GRUB2（grub-efi-*-signed 包内 grubnet*.efi.signed）
      来源: http://ftp.debian.org/debian/pool/main/g/grub-efi-*-signed/
      GPL-3.0 许可（GRUB2）

  pxelinux.0 / ldlinux.c32 / menu.c32 / libutil.c32 / libcom32.c32 / memdisk
      SYSLINUX/PXELINUX 6.03
      来源: https://www.kernel.org/pub/linux/utils/boot/syslinux/syslinux-6.03.tar.gz
      GPL-2.0 许可

  wimboot
      iPXE wimboot（混合 BIOS+UEFI，支持 Secure Boot）
      来源: https://github.com/ipxe/wimboot/releases/latest
      GPL-2.0 许可

  undionly.kpxe / ipxe.efi
      iPXE 官方预编译引导固件
      来源: https://boot.ipxe.org/
      GPL-2.0 许可

说明：grubx64.efi 为 Debian 签名版本，Secure Boot 开启时需额外配置 shim；
如需自定义 GRUB 模块组合（含 BIOS grub.pxe），请使用 tools/build_grub.sh 在 Linux 构建。
"""
    with open(os.path.join(BOOT_DIR, "README.txt"), "w", encoding="utf-8") as f:
        f.write(readme)
    log("boot/README.txt 已写入来源说明")


if __name__ == "__main__":
    main()
