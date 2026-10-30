"""Partition tables and filesystem detection."""
import struct
import uuid
from dataclasses import dataclass, field

from ..formats.base import u16le, u32le


@dataclass
class FileEntry:
    path: str
    size: int
    deleted: bool
    extents: list  # [(absolute offset in source, length)]; offset None = sparse (zeros)
    mtime: str = ""
    note: str = ""
    resident: bytes = None  # tiny files stored inside the metadata (NTFS)
    origin: str = ""  # "fs" for live directory tree, "orphan" for entries found in free space
    fs: str = ""

    def to_dict(self):
        return {"path": self.path, "size": self.size, "deleted": self.deleted, "mtime": self.mtime,
                "note": self.note, "origin": self.origin, "fs": self.fs,
                "start": self.extents[0][0] if self.extents and self.extents[0][0] is not None else None,
                "fragments": len(self.extents)}


@dataclass
class Volume:
    offset: int
    size: int
    fstype: str
    label: str = ""
    scheme: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self):
        return {"offset": self.offset, "size": self.size, "fstype": self.fstype, "label": self.label,
                "scheme": self.scheme}


def sniff(src, off):
    """Filesystem type of the volume starting at ``off``."""
    b = src.read(off, 4096)
    if len(b) < 1536:
        return "?"
    if b[3:11] == b"EXFAT   ":
        return "exfat"
    if b[3:11] == b"NTFS    ":
        return "ntfs"
    if b[3:11] == b"ReFS\x00\x00\x00\x00":
        return "refs"
    if b[32:36] == b"NXSB":
        return "apfs"
    if b[1024:1026] in (b"H+", b"HX"):
        return "hfs+"
    if b[1080:1082] == b"\x53\xef":
        return "ext"
    if b[510:512] == b"\x55\xaa" and b[0] in (0xEB, 0xE9) and u16le(b, 11) in (512, 1024, 2048, 4096):
        if b[82:87] == b"FAT32" or b[54:59] in (b"FAT12", b"FAT16", b"FAT  ") or (u16le(b, 22) == 0 and u32le(b, 36)):
            return "fat"
    return "?"


GPT_TYPES = {
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "Microsoft basic data",
    "7C3457EF-0000-11AA-AA11-00306543ECAC": "APFS",
    "48465300-0000-11AA-AA11-00306543ECAC": "HFS+",
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EFI",
    "0FC63DAF-8483-4772-8E79-3D69D8477DE4": "Linux",
}


def volumes(src):
    """List volumes in ``src``: partitions of a whole disk, or the source itself if it is a volume."""
    ss = src.sector_size
    out = []
    own = sniff(src, 0)
    if own not in ("?",):
        return [Volume(0, src.size, own, scheme="volumen")]
    gpt = src.read(ss, 92)
    if gpt[:8] == b"EFI PART":
        lba, count, esz = struct.unpack_from("<QII", gpt, 72)
        tbl = src.read(lba * ss, min(count, 256) * esz)
        for i in range(min(count, 256)):
            e = tbl[i * esz:(i + 1) * esz]
            if len(e) < 128 or e[:16] == b"\0" * 16:
                continue
            first, last = struct.unpack_from("<QQ", e, 32)
            name = e[56:128].decode("utf-16-le", "replace").rstrip("\0")
            v = Volume(first * ss, (last - first + 1) * ss, sniff(src, first * ss), name, "GPT")
            v.meta["tipo"] = GPT_TYPES.get(str(uuid.UUID(bytes_le=e[:16])).upper(), "")
            out.append(v)
        return out
    mbr = src.read(0, 512)
    if mbr[510:512] == b"\x55\xaa":
        for i in range(4):
            e = mbr[446 + 16 * i:462 + 16 * i]
            typ, start, cnt = e[4], u32le(e, 8), u32le(e, 12)
            if typ and cnt and typ != 0xEE:
                out.append(Volume(start * ss, cnt * ss, sniff(src, start * ss), "", "MBR", {"tipo": "0x%02X" % typ}))
    return out


def open_fs(src, vol):
    """Return a parser for the volume, or None if the filesystem is not parsed (carving still works)."""
    from . import exfat, fat, ntfs
    cls = {"exfat": exfat.ExFAT, "fat": fat.FAT, "ntfs": ntfs.NTFS}.get(vol.fstype)
    return cls(src, vol.offset) if cls else None


def dos_time(t, d):
    if not d:
        return ""
    return "%04d-%02d-%02d %02d:%02d:%02d" % (1980 + (d >> 9), (d >> 5) & 15, d & 31, t >> 11, (t >> 5) & 63, (t & 31) * 2)
