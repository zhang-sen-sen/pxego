本目录中的引导文件均为开源软件，来源如下：

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
