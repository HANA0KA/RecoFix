"""Any volume seen as aligned blocks: lets the rescue work where there is no cluster map to read.

NTFS, HFS+, APFS, ext, unrecognised or damaged filesystems, or a raw disk with no partition table. The live tree
(when a parser exists) says which blocks files own today; everything else is searched for file headers and
arbitrated with the manifest hash. Files are assumed contiguous: without an allocation map there is no chain to
follow, so the hash decides and a fragmented file is reported as not found instead of being guessed.
"""


class Flat:
    name = "sin mapa de asignación"
    root = 0

    def __init__(self, src, vol, parsed=None):
        self.src, self.parsed = src, parsed
        self.heap = vol.offset
        cs = getattr(parsed, "cs", 0) or 4096
        self.cs = cs if cs >= 512 and cs & (cs - 1) == 0 else 4096
        self.n_clusters = max(0, vol.size // self.cs)
        self.label = vol.label or getattr(parsed, "label", "") or ""
        self.kind = vol.fstype

    def cl_off(self, c):
        return self.heap + (c - 2) * self.cs

    def cluster_of(self, off):
        return 2 + (off - self.heap) // self.cs

    def walk(self, *a, **k):
        return self.parsed.walk(*a, **k) if self.parsed else []

    def chain_runs(self, first, need):
        top = self.n_clusters + 2
        if first + need > top:
            return [(first, max(0, top - first))], ["el archivo se saldría del volumen (cluster %d + %d)" % (first, need)]
        return [(first, need)], []

    def is_used(self, c):
        return None

    def used_in_runs(self, runs):
        return None

    def allocated_count(self, first, n):
        return None

    def free_count(self):
        return None
