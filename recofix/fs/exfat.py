"""exFAT: live tree, deleted entries, and orphaned directory entry sets found in free space.

Camera media (ARRI, RED, Sony, Blackmagic, Canon...) is almost always exFAT and
clips are written contiguously, flagged NoFatChain. That makes a deleted or
reformatted clip recoverable from just its directory entry set: first cluster
and exact byte size. Entry sets carry a checksum, so orphan sets found by
scanning raw clusters are validated before being trusted.
"""
import struct

from . import FileEntry, dos_time


def set_checksum(entries):
    c = 0
    for i, b in enumerate(entries):
        if i in (2, 3):
            continue
        c = (((c << 15) | (c >> 1)) + b) & 0xFFFF
    return c


def parse_entry_sets(data, allow_deleted=True, require_checksum=False):
    """Yield dicts for every file/dir entry set in a run of 32-byte directory entries."""
    i = 0
    n = len(data)
    while i + 64 <= n:
        t = data[i]
        if t in (0x85, 0x05) and (allow_deleted or t == 0x85):
            sc = data[i + 1]
            if 2 <= sc <= 18 and i + 32 * (sc + 1) <= n:
                block = data[i:i + 32 * (sc + 1)]
                if all(block[32 * j] & 0x7F in (0x40, 0x41, 0x60, 0x61) for j in range(1, sc + 1)) \
                        and block[32] & 0x7F == 0x40:
                    # the checksum covers the in-use bits as they were before deletion
                    orig = bytearray(block)
                    for j in range(sc + 1):
                        orig[32 * j] |= 0x80
                    ok = set_checksum(orig) == struct.unpack_from("<H", block, 2)[0]
                    if ok or not require_checksum:
                        e = _decode(block, t == 0x05)
                        if e:
                            e["checksum_ok"] = ok
                            e["pos"] = i
                            yield e
                    i += 32 * (sc + 1)
                    continue
        i += 32


def _decode(block, deleted):
    sc = block[1]
    attr = struct.unpack_from("<H", block, 4)[0]
    ct, cd, mt, md = struct.unpack_from("<HHHH", block, 8)
    s = block[32:64]
    flags, nl = s[1], s[3]
    vdl = struct.unpack_from("<Q", s, 8)[0]
    first = struct.unpack_from("<I", s, 20)[0]
    size = struct.unpack_from("<Q", s, 24)[0]
    name = ""
    for j in range(2, sc + 1):
        e = block[32 * j:32 * j + 32]
        if e[0] & 0x7F == 0x41:
            name += e[2:32].decode("utf-16-le", "replace")
    name = name[:nl]
    if not name or "\0" in name:
        return None
    return {"name": name, "deleted": deleted, "dir": bool(attr & 0x10), "nofat": bool(flags & 2),
            "first": first, "size": size, "vdl": vdl, "mtime": dos_time(mt, md)}


class ExFAT:
    name = "exFAT"

    def __init__(self, src, base):
        self.src = src
        self.base = base
        bs = src.read(base, 512)
        if bs[3:11] != b"EXFAT   ":
            raise ValueError("no es exFAT")
        self.fat_off = struct.unpack_from("<I", bs, 80)[0]
        self.heap_off = struct.unpack_from("<I", bs, 88)[0]
        self.n_clusters = struct.unpack_from("<I", bs, 92)[0]
        self.root = struct.unpack_from("<I", bs, 96)[0]
        self.bps = 1 << bs[108]
        self.cs = self.bps << bs[109]
        self.heap = base + self.heap_off * self.bps
        self.label = ""
        self.bitmap = None

    def cl_off(self, c):
        return self.heap + (c - 2) * self.cs

    def fat_chain(self, first, limit):
        out, c = [], first
        fat = self.base + self.fat_off * self.bps
        while 2 <= c < self.n_clusters + 2 and len(out) < limit:
            out.append(c)
            c = struct.unpack("<I", self.src.read(fat + c * 4, 4))[0]
        return out

    def extents(self, first, size, nofat, deleted):
        """Byte extents (absolute) for a stream; deleted chains are assumed contiguous."""
        if size == 0 or first < 2:
            return [], ""
        need = -(-size // self.cs)
        if nofat or deleted:
            note = "" if nofat else "cadena FAT perdida: se asume contiguo"
            if first + need > self.n_clusters + 2:
                return [], "fuera del volumen"
            return [(self.cl_off(first), size)], note
        chain = self.fat_chain(first, need)
        ext = []
        for c in chain:
            o = self.cl_off(c)
            if ext and ext[-1][0] + ext[-1][1] == o:
                ext[-1] = (ext[-1][0], ext[-1][1] + self.cs)
            else:
                ext.append((o, self.cs))
        total, trimmed = 0, []
        for o, ln in ext:
            ln = min(ln, size - total)
            if ln <= 0:
                break
            trimmed.append((o, ln))
            total += ln
        return trimmed, "" if total == size else "cadena FAT incompleta"

    def read_dir(self, first, size, nofat):
        if nofat and size:
            return self.src.read(self.cl_off(first), size)
        return b"".join(self.src.read(self.cl_off(c), self.cs) for c in self.fat_chain(first, 1 << 16))

    def walk(self, on_entry=None, max_entries=2_000_000):
        out = []
        seen = set()
        stack = [("", self.root, 0, False, False)]
        while stack and len(out) < max_entries:
            path, first, size, nofat, deleted_parent = stack.pop()
            if first in seen or not 2 <= first < self.n_clusters + 2:
                continue
            seen.add(first)
            data = self.read_dir(first, size, nofat) if not deleted_parent else \
                self.src.read(self.cl_off(first), min(size or self.cs, 64 << 20))
            if not path:
                for i in range(0, len(data), 32):
                    if data[i] == 0x83:
                        self.label = data[i + 2:i + 2 + 2 * data[i + 1]].decode("utf-16-le", "replace")
            for e in parse_entry_sets(data):
                full = path + "/" + e["name"]
                deleted = e["deleted"] or deleted_parent
                if e["dir"]:
                    stack.append((full, e["first"], e["size"], e["nofat"], deleted))
                    continue
                ext, note = self.extents(e["first"], e["size"], e["nofat"], deleted)
                fe = FileEntry(full, e["size"], deleted, ext, e["mtime"], note, origin="fs", fs="exFAT")
                out.append(fe)
                if on_entry:
                    on_entry(fe)
        self._dirs_seen = seen
        return out

    def orphans_in(self, buf, buf_off):
        """Validated entry sets inside raw bytes read from ``buf_off`` (absolute).

        Called by the deep scan on every cluster it reads, to find directories
        of deleted folders or of a reformatted card.
        """
        out = []
        rel = (buf_off - self.heap) % self.cs
        start = (self.cs - rel) % self.cs
        for cpos in range(start, len(buf), self.cs):
            head = buf[cpos]
            if head not in (0x85, 0x05, 0x83, 0x81, 0x82, 0x03, 0x01, 0x02):
                continue
            c = 2 + (buf_off + cpos - self.heap) // self.cs
            if c in getattr(self, "_dirs_seen", ()):
                continue
            for e in parse_entry_sets(buf[cpos:cpos + self.cs], require_checksum=True):
                if e["dir"] or not e["size"]:
                    continue
                ext, note = self.extents(e["first"], e["size"], e["nofat"], True)
                out.append(FileEntry("/_huerfanos_cluster%d/%s" % (c, e["name"]), e["size"], True, ext, e["mtime"],
                                     note or "entrada encontrada en espacio libre", origin="orphan", fs="exFAT"))
        return out
