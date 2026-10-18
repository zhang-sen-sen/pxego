# -*- coding: utf-8 -*-
"""
ISO 文件系统读取器（用于引导菜单自动检测）。

- ISO9660Reader：读取 ISO9660 主卷描述符 + Joliet 补充卷描述符 + Rock Ridge 名称扩展，
  返回 ISO 内的文件路径索引（统一小写、去掉版本号）。
- UDFReader：针对 Windows 安装镜像（UDF 文件系统）的最佳努力读取，
  仅用于探测 bootmgr / sources/boot.wim 等关键文件；任何解析异常都会安全回退。

设计目标：不解析 ISO 的全部数据，只读取目录结构，用于“这个 ISO 是什么系统、内核/initrd 在哪”。
"""
import os
import struct

SECTOR = 2048
MAX_DEPTH = 24
MAX_FILES = 200000


def _norm(name):
    """规范化 ISO 内路径：转小写、去版本号、统一分隔符。"""
    name = name.strip().replace("\\", "/")
    if ";1" in name:
        name = name.split(";1")[0]
    return name.lower()


# ---------------------------------------------------------------- ISO9660 --

class ISO9660Reader:
    """ISO9660 (+Joliet +RockRidge 名称) 文件索引读取器。"""

    def __init__(self, path):
        self.path = path
        self.files = {}   # normpath -> {"size": int}
        self.f = None
        try:
            self.f = open(path, "rb")
            self._parse()
        except Exception:
            self.files = {}
        finally:
            if self.f:
                self.f.close()

    def _read_block(self, lba):
        self.f.seek(lba * SECTOR)
        return self.f.read(SECTOR)

    # ---- 目录记录 ----
    @staticmethod
    def _parse_dir_record(buf, off):
        """解析目录记录，返回 dict；无效返回 None。"""
        if off + 33 > len(buf):
            return None
        length = buf[off]
        if length == 0 or length < 34:
            return None
        if off + length > len(buf):
            return None
        ext_attr_len = buf[off + 1]
        lba_le = struct.unpack_from("<I", buf, off + 2)[0]
        size_le = struct.unpack_from("<I", buf, off + 10)[0]
        flags = buf[off + 25]
        name_len = buf[off + 32]
        if off + 33 + name_len > len(buf):
            return None
        name = buf[off + 33: off + 33 + name_len]
        sys_use_off = off + 33 + name_len
        if sys_use_off % 2:
            sys_use_off += 1  # 名称字段按偶数字节对齐
        return {
            "length": length, "ext_attr_len": ext_attr_len,
            "lba": lba_le, "size": size_le, "flags": flags,
            "name": name, "name_len": name_len,
            "sys_use_off": sys_use_off,
        }

    def _walk(self, dir_lba, dir_size, depth, prefix, joliet, use_rr):
        """递归遍历目录；收集文件索引。"""
        if depth > MAX_DEPTH or len(self.files) > MAX_FILES:
            return
        buf = bytearray()
        remaining = dir_size
        lba = dir_lba
        while remaining > 0 and len(buf) < dir_size + SECTOR:
            buf += self._read_block(lba)
            lba += 1
            remaining -= SECTOR
        if not buf:
            return
        pos = 0
        data = bytes(buf[:dir_size]) if dir_size else bytes(buf)
        while pos < len(data) and len(self.files) <= MAX_FILES:
            rec = self._parse_dir_record(data, pos)
            if rec is None:
                # 记录无效（一般为扇区末尾零填充）：跳到下一个 2048 对齐边界
                pos = ((pos // SECTOR) + 1) * SECTOR
                continue
            name = rec["name"]
            if joliet:
                try:
                    name = name.decode("utf-16-be")
                except UnicodeDecodeError:
                    name = name.decode("ascii", "replace")
            else:
                name = name.decode("ascii", "replace")
            # Rock Ridge 长文件名（覆盖 ISO9660 8.3 短名）
            rr_name = None
            if use_rr:
                rr_name = self._rockridge_name(data, rec["sys_use_off"], rec["length"], joliet)
            display = rr_name or name
            # 跳过 "." / ".."（含 ISO 根目录的 \x00 / \x01 标记）
            if display in (".", "..", "\x00", "\x01"):
                pos += rec["length"]
                continue
            clean = _norm(display)
            if clean.endswith("."):
                clean = clean[:-1]
            full = (prefix + "/" + clean) if prefix else clean
            if rec["flags"] & 0x02:
                # 目录
                self._walk(rec["lba"], rec["size"], depth + 1, full, joliet, use_rr)
            else:
                # 文件：记录 extent（数据起始块 = 目录记录 LBA + 扩展属性长度）
                self.files[full] = {"size": rec["size"],
                                    "lba": rec["lba"] + rec["ext_attr_len"]}
            pos += rec["length"]

    def _rockridge_name(self, data, off, rec_len, joliet):
        """在 System Use 区解析 Rock Ridge 'NM' 名称扩展（SUSP 记录遍历）。"""
        if off >= rec_len:
            return None
        name_parts = []
        found_sp = False
        o = off
        while o + 4 <= rec_len:
            sig = data[o:o + 2]
            ln = data[o + 2]
            if ln < 4 or o + ln > rec_len:
                break
            if sig == b"SP":
                found_sp = True
            elif sig == b"NM" and found_sp:
                flags = data[o + 3]
                nm_name = data[o + 4:o + ln]
                if not joliet:
                    try:
                        nm_name = nm_name.decode("ascii")
                    except UnicodeDecodeError:
                        nm_name = nm_name.decode("utf-8", "replace")
                name_parts.append(nm_name)
                if not (flags & 0x01):  # 非续接记录，结束
                    break
            elif sig == b"CE":
                pass  # 续接区域：忽略
            o += ln
        if not name_parts:
            return None
        return "".join(name_parts)

    def _parse(self):
        # 主卷描述符（PVD）位于扇区 16；后续扇区找 Joliet SVD
        self.f.seek(16 * SECTOR)
        buf = self.f.read(SECTOR)
        if buf[1:6] != b"CD001":
            return
        root_off = 156
        root = self._parse_dir_record(buf, root_off)
        if root is None:
            return
        # 探测 Rock Ridge：根目录 System Use 区是否有 'SP'
        use_rr = self._rockridge_name(buf, root["sys_use_off"], root["length"], False) is not None or self._has_susp(buf, root)
        self._walk(root["lba"], root["size"], 0, "", False, use_rr)

        # Joliet：扫描 16..19 扇区的补充卷描述符
        for lba in range(16, 20):
            b2 = self._read_block(lba)
            if b2[0] == 2 and b2[1:6] == b"CD001":
                esc = b2[88:91]
                if esc in (b"%/@", b"%/C", b"%/E"):
                    jroot = self._parse_dir_record(b2, 156)
                    if jroot:
                        # Joliet 树优先覆盖同名条目
                        before = dict(self.files)
                        self._walk(jroot["lba"], jroot["size"], 0, "", True, False)
                        if self.files:
                            return

    @staticmethod
    def _has_susp(buf, root):
        o = root["sys_use_off"]
        end = root["length"]
        while o + 4 <= end:
            if buf[o:o + 2] == b"SP":
                return True
            ln = buf[o + 2] if o + 2 < end else 0
            if ln < 4:
                break
            o += ln
        return False


# ---------------------------------------------------------------- UDF ------

class UDFReader:
    """UDF 2.x 最小读取器：仅用于探测关键文件（Windows ISO）。

    采用 ECMA-167/UDF 描述符布局（大端为主，逐级做 tag 校验与位置合理性校验），
    任何一步校验失败都会安全返回空索引，不影响整体功能。
    """

    def __init__(self, path):
        self.path = path
        self.files = {}
        self.block = SECTOR
        self.le = False  # 字节序：False=UDF 标准大端；True=微软 oscdimg 小端变体
        try:
            self.f = open(path, "rb")
            self._scan()
        except Exception:
            self.files = {}
        finally:
            try:
                self.f.close()
            except Exception:
                pass

    def _u16(self, buf, off):
        return struct.unpack_from("<H" if self.le else ">H", buf, off)[0]

    def _u32(self, buf, off):
        return struct.unpack_from("<I" if self.le else ">I", buf, off)[0]

    def _u64(self, buf, off):
        return struct.unpack_from("<Q" if self.le else ">Q", buf, off)[0]

    def _read(self, block, count=1):
        self.f.seek(block * self.block)
        return self.f.read(self.block * count)

    def _read_phys(self, block, count=1):
        """按物理扇区（2048 字节）读取，用于 VDS 遍历。"""
        self.f.seek(block * SECTOR)
        return self.f.read(SECTOR * count)

    def _tag_ok(self, buf, expect_id):
        """校验 UDF 描述符 tag：标识 + 校验和 + 位置合理性。"""
        if len(buf) < 16:
            return False
        tag_id = self._u16(buf, 0)
        if tag_id != expect_id:
            return False
        if self.le:
            # 微软 oscdimg 小端变体不保证标准校验和，仅校验 tag id
            return True
        # 校验和：除 offset4 外所有字节求和 mod 256 == 0
        total = 0
        for i, b in enumerate(buf[:16]):
            if i != 4:
                total += b
        if (total + buf[4]) % 256 != 0:
            return False
        return True

    def _scan(self):
        # 1) Anchor Volume Descriptor Pointer @ LBA 256
        #    UDF 标准为大端（tag 字节 00 02）；微软 oscdimg 生成的小端变体
        #    tag 字节为 02 00，所有多字节字段按小端解析。
        avdp = self._read(256)
        tag_be = struct.unpack_from(">H", avdp, 0)[0]
        if tag_be == 0x0002:
            self.le = False
        elif tag_be == 0x0200 and struct.unpack_from("<H", avdp, 0)[0] == 0x0002:
            self.le = True
        else:
            return
        # AVDP: @16 MainVDS extentLength, @20 MainVDS location,
        #      @32 ReserveVDS extentLength, @36 ReserveVDS location
        vds_len = self._u32(avdp, 16)
        vds_block = self._u32(avdp, 20)
        if vds_block == 0 or vds_len > 4096 * self.block:
            # 尝试备用 VDS
            vds_len = self._u32(avdp, 32)
            vds_block = self._u32(avdp, 36)
            if vds_block == 0 or vds_len > 4096 * self.block:
                return

        # 2) 遍历卷描述符序列，找 Partition Descriptor 与 LVD
        #    VDS 位于物理扇区（2048），遍历固定用 2048；LVD 的 logicalBlockSize
        #    用于后续分区内读取（逻辑块），延迟到遍历结束后生效。
        part_start = None
        part_len = None
        block_size = SECTOR
        lvd_contents = None
        count = min(vds_len // SECTOR, 4096)
        for i in range(count):
            desc = self._read_phys(vds_block + i)
            if len(desc) < 16:
                break
            tag_id = self._u16(desc, 0)
            if tag_id == 0x05 and self._tag_ok(desc, 0x05):
                # Partition Descriptor (ECMA-167 4.4.7)
                part_start = self._u32(desc, 188)   # partitionStartingLocation
                part_len = self._u32(desc, 192)     # partitionLength
            elif tag_id == 0x06 and self._tag_ok(desc, 0x06):
                # Logical Volume Descriptor (ECMA-167 4.4.6)
                bs = self._u32(desc, 28)            # logicalBlockSize
                if 512 <= bs <= 8192:
                    block_size = bs
                lvd_contents = (self._u32(desc, 64), self._u32(desc, 68))  # logicalVolumeContentsUse extent
            elif tag_id == 0x08:
                break
        if part_start is None or part_len <= 0:
            return
        self.block = block_size

        # 3) 定位 File Set Descriptor
        fsd_block = None
        if lvd_contents:
            cand = lvd_contents[0]
            if 0 < cand < part_start + part_len:
                probe = self._read(cand)
                if self._tag_ok(probe, 0x0B):
                    fsd_block = cand
        if fsd_block is None:
            # 在分区起始附近扫描 FSD（UDF 常见布局：位于分区起始）
            for off in range(0, min(4096, part_len), 8):
                cand = part_start + off
                probe = self._read(cand)
                if self._tag_ok(probe, 0x0B):
                    fsd_block = cand
                    break
        if fsd_block is None:
            return

        # 4) 从 FSD 取根目录 ICB
        fsd = self._read(fsd_block)
        if not self._tag_ok(fsd, 0x0B):
            return
        root_extent = self._u32(fsd, 392)     # rootDirectoryICB (long_ad) extentLength
        root_loc = self._u32(fsd, 404)        # rootDirectoryICB location (long_ad @+12)
        if root_loc >= part_len:
            return
        self._walk_dir(root_loc, root_extent, part_start, "", depth=0, seen=set())

    def _volume_blocks(self):
        """尝试从 PVD(扇区16) 读取卷空间大小。"""
        try:
            pvd = self._read(16)
            if pvd[1:6] == b"CD001":
                # ISO9660 PVD 的卷空间大小是 both-endian：@80 大端、@84 小端
                return struct.unpack_from("<I", pvd, 84)[0]
        except Exception:
            pass
        return 2 ** 32

    def _file_extent(self, block, part_start):
        """读取 File Entry (0x10)，返回文件/目录数据 extent (绝对块号, 长度) 或 None。

        block 为分区相对块号；分配描述符中的位置同为分区相对，需加 part_start。
        仅取第一个分配描述符（常规镜像文件均为单 extent）。
        """
        buf = self._read(block, 2)
        if not self._tag_ok(buf, 0x10):
            return None
        file_type = buf[27]
        if file_type != 4 and file_type != 5:
            return None
        len_ea = self._u32(buf, 172)              # lengthOfExtendedAttributes
        len_ad = self._u32(buf, 176)              # lengthOfAllocationDescriptors
        if len_ea > 4096 or len_ad > 1 << 22:
            return None
        info_len = self._u64(buf, 56)             # informationLength (8 bytes)
        flags = self._u16(buf, 32)                # icbTag flags (2 bytes)
        ad_type = flags & 0x07
        data_start = 180 + len_ea
        data = buf[data_start: data_start + len_ad]
        if len(data) < len_ad:
            # AD 可能跨块
            extra = self._read(block + 1, 1)
            if extra:
                data = (data + extra)[: len_ad]
        if not data:
            return None
        first_ad = data[:24]
        if ad_type == 0:  # short_ad: 6 字节，位置 @4
            ad_pos = self._u32(first_ad, 4)
        elif ad_type == 1:  # long_ad: 24 字节，位置 @12
            ad_pos = self._u32(first_ad, 12)
        else:
            return None
        return part_start + ad_pos, info_len

    def _walk_dir(self, dir_loc, dir_len, part_start, prefix, depth, seen):
        if depth > MAX_DEPTH or len(self.files) > MAX_FILES or dir_loc in seen:
            return
        seen.add(dir_loc)
        fe = self._file_extent(part_start + dir_loc, part_start)
        if fe is None:
            return
        data_loc, data_len = fe
        # FE 中的数据 = 目录的 FID 列表（最多读取足够容纳 FID 区的块数）
        data = self._read(data_loc, max(1, min(64, (data_len // self.block) + 1)))
        pos = 0
        while pos + 48 <= len(data):
            if data[pos] == 0 and data[pos + 1] == 0:
                break  # 空闲区（FID tag 首字节为 0 但 tag id 高字节为 0x0C）
            tag_id = self._u16(data, pos)
            if tag_id != 0x0C:
                pos += 4
                continue
            if not self._tag_ok(data[pos:pos + 16], 0x0C):
                pos += 4
                continue
            fchars = data[pos + 18]
            fid_len = data[pos + 19]
            icb_len = self._u32(data, pos + 20)
            icb_loc = self._u32(data, pos + 32)
            len_iu = self._u16(data, pos + 40)
            if len_iu > 4096:
                pos += 4
                continue
            name_start = pos + 42 + len_iu
            name_raw = data[name_start: name_start + fid_len]
            try:
                if name_raw and name_raw[0] in (0x08, 0x10):
                    # UDF DSTRING：0x08=压缩（latin1 逐字节），0x10=UTF-16BE
                    if name_raw[0] == 0x10:
                        name = name_raw[1:].decode("utf-16-be", "replace")
                    else:
                        name = name_raw[1:].decode("latin-1", "replace")
                else:
                    name = name_raw.decode("utf-16-be", "replace")
            except Exception:
                name = name_raw.decode("ascii", "replace")
            name = name.rstrip("\x00")
            if name not in (".", "..", "\x01", ""):
                clean = _norm(name)
                full = (prefix + "/" + clean) if prefix else clean
                if fchars & 0x02:
                    self._walk_dir(icb_loc, icb_len, part_start, full, depth + 1, seen)
                else:
                    ext = self._file_extent(icb_loc, part_start)
                    if ext:
                        self.files[full] = {"size": ext[1], "lba": ext[0]}
                    else:
                        self.files[full] = {"size": icb_len}
            # 跳到下一条 FID（FID 头 42 字节 + lenIu + 文件名，对齐 4 字节）
            pos += 42 + len_iu + fid_len
            pos = (pos + 3) & ~3


# ---------------------------------------------------------------- 门面 ------

def read_iso_index(path):
    """返回 ISO 内文件路径索引 {normpath: {"size": int}}。

    优先使用 UDF 索引（Windows ISO 场景），否则回退 ISO9660/Joliet。
    """
    if not os.path.isfile(path):
        return {}
    udf = UDFReader(path)
    if udf.files:
        return udf.files
    iso = ISO9660Reader(path)
    return iso.files


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        idx = read_iso_index(p)
        print(f"{p}: {len(idx)} files")
        for k in sorted(idx)[:40]:
            print("  ", k, idx[k]["size"])
