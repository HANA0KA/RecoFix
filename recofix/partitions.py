"""Lost / deleted partition discovery.

Recoverit exposes this as ``MacExploreLostPartition`` in libdrstoragedevice:
when a partition table was wiped or an entry deleted, the volume's own boot
sector or superblock is still on disk. We look for those structures in every
region the current partition table does not cover, and read each volume's
size from its own header so the recovered volume can be parsed like any other.
"""
import struct

from .formats.base import u16le, u32le
from .fs import Volume, sniff

MiB = 1 << 20


def volume_size(src, off, fstype):
    """Size in bytes declared by the volume header at ``off`` (0 if unknown)."""
    b = src.read(off, 4096)
    try:
        if fstype == "exfat":
            return struct.unpack_from("<Q", b, 72)[0] << b[108]
        if fstype == "ntfs":
            return (struct.unpack_from("<Q", b, 40)[0] + 1) * u16le(b, 11)
        if fstype == "fat":
            total = u16le(b, 19) or u32le(b, 32)
            return total * u16le(b, 11)
        if fstype == "apfs":
            return struct.unpack_from("<Q", b, 40)[0] * u32le(b, 36)
        if fstype == "hfs+":
            return int.from_bytes(b[1024 + 40:1024 + 44], "big") * int.from_bytes(b[1024 + 44:1024 + 48], "big")
        if fstype == "ext":
            blocks = u32le(b, 1024 + 4)
            return blocks * (1024 << u32le(b, 1024 + 24))
    except (struct.error, IndexError):
        pass
    return 0


def _covered(vols):
    return sorted((v.offset, v.offset + v.size) for v in vols)


def _gaps(size, vols, first=MiB // 2):
    """Byte ranges of ``size`` not covered by any of ``vols``."""
    out, pos = [], 0
    for a, b in _covered(vols):
        if a > pos:
            out.append((pos, a))
        pos = max(pos, b)
    if pos < size:
        out.append((pos, size))
    # skip the partition-table area itself
    return [(max(a, first if a == 0 else a), b) for a, b in out if b - max(a, first if a == 0 else a) >= 64 * 1024]


def _candidates(a, b, ss):
    """Offsets worth probing inside [a, b): MiB alignment, legacy 63-sector tracks, and 4 KiB steps near the start."""
    seen = set()
    pos = -(-a // MiB) * MiB
    while pos < b:
        seen.add(pos)
        pos += MiB
    pos = -(-a // (63 * ss)) * 63 * ss
    while pos < b:
        seen.add(pos)
        pos += 63 * ss * 1024  # cylinder-ish groups; full legacy sweep would be millions of reads
    for pos in range(a - a % 4096, min(b, a + 4 * MiB), 4096):
        if pos >= a:
            seen.add(pos)
    return sorted(seen)


def find_lost(src, known=None, cancel=None, progress=None):
    """Return volumes found in unpartitioned space (header-validated, with size)."""
    known = list(known or [])
    ss = src.sector_size
    found = []
    gaps = _gaps(src.size, known)
    total = sum(b - a for a, b in gaps) or 1
    done = 0
    for a, b in gaps:
        skip_to = a
        for off in _candidates(a, b, ss):
            if cancel is not None and cancel.is_set():
                return found
            if off < skip_to:
                continue
            fs = sniff(src, off)
            starts = [(off, fs)]
            if fs == "?":
                # backup boot sectors: exFAT keeps one 12 sectors in, FAT32 6 sectors in
                for back, kind in ((12 * ss, "exfat"), (6 * ss, "fat")):
                    if off - back >= a and sniff(src, off - back) == kind:
                        starts = [(off - back, kind)]
                        break
                else:
                    continue
            vo, kind = starts[0]
            size = volume_size(src, vo, kind)
            if size <= 0 or vo + size > src.size + MiB:
                continue
            v = Volume(vo, min(size, src.size - vo), kind, scheme="perdida")
            v.meta["tipo"] = "partición perdida"
            found.append(v)
            skip_to = vo + size
            if progress:
                progress(min(1.0, (done + (off - a)) / total))
        done += b - a
        if progress:
            progress(done / total)
    return found
