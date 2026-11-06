"""Minimal exFAT formatter in pure Python, so tests can build volumes in states no OS tool will produce.

Writes a real exFAT layout (boot sector, FAT, allocation bitmap, up-case table, directories with entry-set
checksums). Files are contiguous and flagged NoFatChain, like camera media. ``zero`` turns a file into the damaged
state seen on real backup disks: its entry says size 0 / first cluster 0 and its clusters are marked free, but the
bytes stay where they were written.
"""
import os
import struct

from recofix.fs.exfat import set_checksum

BPS_SHIFT = 9  # 512 B sectors
DATE = ((2026 - 1980) << 9) | (10 << 5) | 19
EOC = 0xFFFFFFFF


def _name_hash(name):
    h = 0
    for b in name.upper().encode("utf-16-le"):
        h = (((h << 15) | (h >> 1)) + b) & 0xFFFF
    return h


def _entry_set(name, is_dir, first, size, nofat):
    chars = name.encode("utf-16-le")
    names = [chars[i:i + 30].ljust(30, b"\0") for i in range(0, len(chars), 30)]
    prim = bytearray(32)
    prim[0], prim[1] = 0x85, 1 + len(names)
    struct.pack_into("<H", prim, 4, 0x10 if is_dir else 0x20)
    struct.pack_into("<HHHH", prim, 8, 0x6000, DATE, 0x6000, DATE)
    stream = bytearray(32)
    stream[0], stream[1], stream[3] = 0xC0, 0x03 if nofat else 0x01, len(name)
    struct.pack_into("<H", stream, 4, _name_hash(name))
    struct.pack_into("<Q", stream, 8, size)
    struct.pack_into("<I", stream, 20, first)
    struct.pack_into("<Q", stream, 24, size)
    out = bytearray(prim + stream)
    for n in names:
        e = bytearray(32)
        e[0] = 0xC1
        e[2:32] = n
        out += e
    struct.pack_into("<H", out, 2, set_checksum(out))
    return bytes(out)


class ExfatImage:
    def __init__(self, path, clusters=32768, cluster_size=4096):
        self.path, self.n, self.cs = path, clusters, cluster_size
        fat_len = -(-(clusters + 2) * 4 // 512)
        self.fat_off = 24
        self.heap_off = (self.fat_off + fat_len + 127) // 128 * 128
        self.heap = self.heap_off * 512
        self.f = open(path, "w+b")
        self.f.truncate(self.heap + clusters * self.cs)
        self.next = 5  # 2 = bitmap, 3 = up-case, 4 = root
        self.dirs = {"": {"cluster": 4, "kids": []}}
        self.files = {}

    # ---------------------------------------------------------------- allocation / data

    def _alloc(self, count):
        c = self.next
        assert c + count <= self.n + 2, "imagen llena"
        self.next += count
        return c

    def _write(self, cluster, data):
        self.f.seek(self.heap + (cluster - 2) * self.cs)
        self.f.write(data)

    def skip(self, clusters):
        """Leave ``clusters`` untouched (free, all zero) before the next allocation."""
        self._alloc(clusters)

    def remnant(self, data):
        """Bytes sitting in free space with no directory entry (what a deleted file leaves). Returns its cluster."""
        c = self._alloc(-(-len(data) // self.cs))
        self._write(c, data)
        return c

    # ---------------------------------------------------------------- tree

    def add_dir(self, path):
        parent, name = os.path.split(path)
        c = self._alloc(1)
        self.dirs[path] = {"cluster": c, "kids": []}
        self.dirs[parent]["kids"].append({"name": name, "dir": True, "first": c, "size": self.cs, "zeroed": False})

    def add_file(self, path, data, at=None):
        parent, name = os.path.split(path)
        first = at if at is not None else self._alloc(-(-len(data) // self.cs))
        self._write(first, data)
        kid = {"name": name, "dir": False, "first": first, "size": len(data), "zeroed": False}
        self.dirs[parent]["kids"].append(kid)
        self.files[path] = kid
        return first

    def reserve(self, clusters):
        """Allocate ``clusters`` and return the first one (to interleave other files between fragments)."""
        return self._alloc(clusters)

    def add_fragmented(self, path, data, segments):
        """A file stored in several runs, ``segments`` = [(first_cluster, count)], with a real FAT chain.

        Zeroing it keeps the chain in the FAT, which is what a failed directory update leaves on real disks.
        """
        chain = [c for first, count in segments for c in range(first, first + count)]
        assert len(chain) * self.cs >= len(data), "segmentos demasiado cortos"
        for i, c in enumerate(chain):
            self._write(c, data[i * self.cs:(i + 1) * self.cs])
        parent, name = os.path.split(path)
        kid = {"name": name, "dir": False, "first": chain[0], "size": len(data), "zeroed": False,
               "chain": chain[:-(-len(data) // self.cs)]}
        self.dirs[parent]["kids"].append(kid)
        self.files[path] = kid
        return chain[0]

    def zero(self, path):
        self.files[path]["zeroed"] = True

    # ---------------------------------------------------------------- write structures

    def save(self):
        used = {2, 3, 4} | {d["cluster"] for d in self.dirs.values()}
        for k in self.files.values():
            if not k["zeroed"]:
                used |= set(k.get("chain") or range(k["first"], k["first"] + -(-k["size"] // self.cs)))
        bitmap = bytearray(-(-self.n // 8))
        for c in used:
            bitmap[(c - 2) >> 3] |= 1 << ((c - 2) & 7)
        self._write(2, bytes(bitmap))
        self._write(3, struct.pack("<128H", *range(128)))  # identity up-case for the first 128 code points
        fat = [0] * (self.n + 2)
        fat[0], fat[1] = 0xFFFFFFF8, EOC
        for c in (2, 3, 4) + tuple(d["cluster"] for d in self.dirs.values()):
            fat[c] = EOC
        for k in self.files.values():
            ch = k.get("chain")
            if ch:
                for a, b in zip(ch, ch[1:]):
                    fat[a] = b
                fat[ch[-1]] = EOC
        self.f.seek(self.fat_off * 512)
        self.f.write(struct.pack("<%dI" % len(fat), *fat))
        for path, d in self.dirs.items():
            raw = b""
            if path == "":
                label = "TEST".encode("utf-16-le")
                raw += bytes([0x83, 4]) + label.ljust(30, b"\0")
                bm = bytearray(32)
                bm[0] = 0x81
                struct.pack_into("<I", bm, 20, 2)
                struct.pack_into("<Q", bm, 24, len(bitmap))
                up = bytearray(32)
                up[0] = 0x82
                struct.pack_into("<I", up, 20, 3)
                struct.pack_into("<Q", up, 24, 256)
                raw += bytes(bm) + bytes(up)
            for k in d["kids"]:
                z = k["zeroed"]
                raw += _entry_set(k["name"], k["dir"], 0 if z else k["first"], 0 if z else k["size"],
                                  not z and not k.get("chain"))
            assert len(raw) <= self.cs, "directorio demasiado grande para un cluster"
            self._write(d["cluster"], raw.ljust(self.cs, b"\0"))
        boot = bytearray(512)
        boot[0:3], boot[3:11] = b"\xeb\x76\x90", b"EXFAT   "
        struct.pack_into("<QQ", boot, 64, 0, self.heap_off + self.n * (self.cs // 512))
        struct.pack_into("<IIII", boot, 80, self.fat_off, self.heap_off - self.fat_off, self.heap_off, self.n)
        struct.pack_into("<IIHH", boot, 96, 4, 0x12345678, 0x0100, 0)
        boot[108], boot[109], boot[110], boot[112] = BPS_SHIFT, (self.cs // 512).bit_length() - 1, 1, 0xFF
        boot[510:512] = b"\x55\xaa"
        self.f.seek(0)
        self.f.write(boot)
        self.f.close()
        return self.path
