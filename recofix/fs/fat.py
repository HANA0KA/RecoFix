"""FAT12/16/32 with long file names and deleted (0xE5) entries."""
import struct

from . import FileEntry, dos_time


def _lfn_part(e):
    return (e[1:11] + e[14:26] + e[28:32]).decode("utf-16-le", "replace")


def _short_name(e):
    base = e[0:8].decode("latin-1").rstrip()
    ext = e[8:11].decode("latin-1").rstrip()
    return base + ("." + ext if ext else "")


class FAT:
    name = "FAT"

    def __init__(self, src, base):
        self.src = src
        self.base = base
        bs = src.read(base, 512)
        self.bps = struct.unpack_from("<H", bs, 11)[0]
        self.spc = bs[13]
        reserved = struct.unpack_from("<H", bs, 14)[0]
        nfats = bs[16]
        root_entries = struct.unpack_from("<H", bs, 17)[0]
        total = struct.unpack_from("<H", bs, 19)[0] or struct.unpack_from("<I", bs, 32)[0]
        fatsz = struct.unpack_from("<H", bs, 22)[0] or struct.unpack_from("<I", bs, 36)[0]
        self.cs = self.bps * self.spc
        self.fat_off = base + reserved * self.bps
        root_secs = (root_entries * 32 + self.bps - 1) // self.bps
        self.root_off = self.fat_off + nfats * fatsz * self.bps
        self.data_off = self.root_off + root_secs * self.bps
        self.root_size = root_secs * self.bps
        data_secs = total - (reserved + nfats * fatsz + root_secs)
        self.n_clusters = data_secs // self.spc
        self.kind = 12 if self.n_clusters < 4085 else 16 if self.n_clusters < 65525 else 32
        self.root_cluster = struct.unpack_from("<I", bs, 44)[0] if self.kind == 32 else 0
        self.name = "FAT%d" % self.kind
        lab = bs[71:82] if self.kind == 32 else bs[43:54]
        self.label = lab.decode("latin-1").strip()
        self._fat = None

    def fat(self):
        if self._fat is None:
            size = {12: (self.n_clusters + 2) * 3 // 2 + 1, 16: (self.n_clusters + 2) * 2, 32: (self.n_clusters + 2) * 4}[self.kind]
            self._fat = self.src.read(self.fat_off, size)
        return self._fat

    def next(self, c):
        f = self.fat()
        if self.kind == 32:
            return struct.unpack_from("<I", f, c * 4)[0] & 0x0FFFFFFF
        if self.kind == 16:
            return struct.unpack_from("<H", f, c * 2)[0]
        v = struct.unpack_from("<H", f, c * 3 // 2)[0]
        return v >> 4 if c & 1 else v & 0xFFF

    def eoc(self, c):
        return c >= {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[self.kind] or c < 2

    def cl_off(self, c):
        return self.data_off + (c - 2) * self.cs

    def chain(self, first, limit):
        out, c = [], first
        while not self.eoc(c) and c < self.n_clusters + 2 and len(out) < limit:
            out.append(c)
            c = self.next(c)
        return out

    def extents(self, first, size, deleted):
        if size == 0 or first < 2:
            return [], ""
        need = -(-size // self.cs)
        if deleted:
            if first + need > self.n_clusters + 2:
                return [], "fuera del volumen"
            return [(self.cl_off(first), size)], "FAT borrada: se asume contiguo"
        ext, total = [], 0
        for c in self.chain(first, need):
            o, ln = self.cl_off(c), min(self.cs, size - total)
            if ext and ext[-1][0] + ext[-1][1] == o:
                ext[-1] = (ext[-1][0], ext[-1][1] + ln)
            else:
                ext.append((o, ln))
            total += ln
        return ext, "" if total == size else "cadena FAT incompleta"

    def entries(self, data):
        lfn = []
        for i in range(0, len(data) - 31, 32):
            e = data[i:i + 32]
            if e[0] == 0:
                break
            attr = e[11]
            if attr == 0x0F:
                lfn.append(e)
                continue
            deleted = e[0] == 0xE5
            if attr & 0x08 or e[0:2] in (b".\x20", b"..") or e[0] == 0x2E:
                lfn = []
                continue
            if lfn:
                # LFN entries precede their short entry in reverse order
                name = "".join(_lfn_part(x) for x in reversed(lfn)).split("\0")[0].rstrip("￿")
            else:
                name = _short_name(e)
                if deleted:
                    name = "_" + name[1:]
            lfn = []
            first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16 if self.kind == 32 else 0)
            size = struct.unpack_from("<I", e, 28)[0]
            mt, md = struct.unpack_from("<HH", e, 22)
            yield name, deleted, bool(attr & 0x10), first, size, dos_time(mt, md)

    def walk(self, on_entry=None, max_entries=2_000_000):
        out = []
        seen = set()
        if self.kind == 32:
            stack = [("", self.root_cluster, False)]
        else:
            stack = [("", None, False)]
        while stack and len(out) < max_entries:
            path, first, deleted_parent = stack.pop()
            if first is None:
                data = self.src.read(self.root_off, self.root_size)
            else:
                if first in seen or not 2 <= first < self.n_clusters + 2:
                    continue
                seen.add(first)
                if deleted_parent:
                    data = self.src.read(self.cl_off(first), self.cs * 8)
                else:
                    data = b"".join(self.src.read(self.cl_off(c), self.cs) for c in self.chain(first, 1 << 16))
            for name, deleted, is_dir, cl, size, mt in self.entries(data):
                full = path + "/" + name
                deleted = deleted or deleted_parent
                if is_dir:
                    stack.append((full, cl, deleted))
                    continue
                ext, note = self.extents(cl, size, deleted)
                fe = FileEntry(full, size, deleted, ext, mt, note, origin="fs", fs=self.name)
                out.append(fe)
                if on_entry:
                    on_entry(fe)
        return out
