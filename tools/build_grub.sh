#!/bin/bash
# ============================================================================
# 在 Linux 上使用 grub-mkstandalone 构建自定义 GRUB 引导镜像
# 产物:
#   boot/grubx64.efi    UEFI x86-64（含 http/loopback/udf 等完整模块）
#   boot/grubia32.efi   UEFI IA32（可选）
#   boot/grub.pxe       BIOS PXE 镜像（i386-pc-pxe，可选，用于 BIOS 完整菜单）
#
# 依赖（Debian/Ubuntu）:
#   sudo apt install grub-efi-amd64-bin grub-efi-ia32-bin grub-pc-bin
# ============================================================================
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOOT="$ROOT/boot"
mkdir -p "$BOOT"

MODULES="all_video boot chain configfile efi_gop efi_net ext2 fat font gfxterm http iso9660 jpeg linux linux16 loopback minicmd net normal part_gpt part_msdos png reboot search search_fs_uuid search_label tftp udf video"

echo "==> 构建 grubx64.efi (UEFI x86-64)"
grub-mkstandalone -O x86_64-efi \
  --modules="$MODULES" \
  --output="$BOOT/grubx64.efi" \
  --fonts= --themes= || echo "警告: grub-mkstandalone 失败，请检查是否安装 grub-efi-amd64-bin"

if command -v grub-mkstandalone >/dev/null && [ -d /usr/lib/grub/i386-efi ]; then
  echo "==> 构建 grubia32.efi (UEFI IA32)"
  grub-mkstandalone -O i386-efi \
    --modules="$MODULES" \
    --output="$BOOT/grubia32.efi" \
    --fonts= --themes= || true
fi

if command -v grub-mkstandalone >/dev/null && [ -d /usr/lib/grub/i386-pc ]; then
  echo "==> 构建 grub.pxe (BIOS PXE)"
  grub-mkstandalone -O i386-pc-pxe \
    --modules="$MODULES" \
    --output="$BOOT/grub.pxe" \
    --fonts= --themes= || true
fi

echo "==> 完成。产物:"
ls -la "$BOOT"/grub*.efi "$BOOT"/grub.pxe 2>/dev/null || true
