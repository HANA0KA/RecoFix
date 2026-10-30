"""Read-only access to block devices and disk images.

Every source is opened with O_RDONLY. Raw devices on macOS (/dev/rdiskN) only
accept sector-aligned reads, so all reads are widened to sector boundaries.
Unreadable sectors are replaced with zeros and recorded in ``bad_ranges`` so a
damaged disk never aborts a scan.
"""
import fcntl
import os
import stat
import struct
import threading

DKIOCGETBLOCKSIZE = 0x40046418  # _IOR('d', 24, uint32_t)
DKIOCGETBLOCKCOUNT = 0x40086419  # _IOR('d', 25, uint64_t)


class Source:
    """A read-only, sector-aligned, bad-sector tolerant byte source."""

    def __init__(self, path, sector_size=None):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)
        st = os.fstat(self.fd)
        self.is_device = stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode)
        if self.is_device:
            bs = struct.unpack("<I", fcntl.ioctl(self.fd, DKIOCGETBLOCKSIZE, b"\0" * 4))[0]
            cnt = struct.unpack("<Q", fcntl.ioctl(self.fd, DKIOCGETBLOCKCOUNT, b"\0" * 8))[0]
            self.sector_size = sector_size or bs
            self.size = bs * cnt
        else:
            self.sector_size = sector_size or 512
            self.size = st.st_size
        self.bad_ranges = []
        self._lock = threading.Lock()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _pread_aligned(self, off, n):
        try:
            return os.pread(self.fd, n, off)
        except OSError:
            # Fall back to sector-by-sector reads so one bad sector costs 512 bytes, not the chunk.
            ss = self.sector_size
            out = bytearray()
            for s in range(off, off + n, ss):
                try:
                    out += os.pread(self.fd, ss, s)
                except OSError:
                    out += b"\0" * ss
                    with self._lock:
                        self.bad_ranges.append((s, ss))
            return bytes(out)

    def read(self, off, n):
        """Read ``n`` bytes at ``off``; short only at end of source."""
        if off < 0 or n <= 0 or off >= self.size:
            return b""
        n = min(n, self.size - off)
        if not self.is_device:
            return self._pread_aligned(off, n)
        ss = self.sector_size
        a = off - off % ss
        end = -(-(off + n) // ss) * ss
        data = self._pread_aligned(a, end - a)
        return data[off - a:off - a + n]


class View:
    """A window into another source (a partition, or a file's extent)."""

    def __init__(self, base, offset, size):
        self.base = base
        self.offset = offset
        self.size = max(0, min(size, base.size - offset))
        self.sector_size = base.sector_size
        self.path = getattr(base, "path", "")

    def read(self, off, n):
        if off < 0 or off >= self.size:
            return b""
        return self.base.read(self.offset + off, min(n, self.size - off))


class CachedReader:
    """Small LRU block cache for parsers that do many small reads (KLV/box walks)."""

    def __init__(self, src, block=1 << 20, blocks=8):
        self.src = src
        self.size = src.size
        self.block = block
        self.blocks = blocks
        self._cache = {}
        self._order = []

    def _get(self, idx):
        b = self._cache.get(idx)
        if b is None:
            b = self.src.read(idx * self.block, self.block)
            self._cache[idx] = b
            self._order.append(idx)
            if len(self._order) > self.blocks:
                self._cache.pop(self._order.pop(0), None)
        return b

    def read(self, off, n):
        if off < 0 or n <= 0 or off >= self.size:
            return b""
        n = min(n, self.size - off)
        if n > self.block * 2:
            return self.src.read(off, n)
        out = bytearray()
        while n > 0:
            idx, rel = divmod(off, self.block)
            blk = self._get(idx)
            piece = blk[rel:rel + n]
            if not piece:
                break
            out += piece
            off += len(piece)
            n -= len(piece)
        return bytes(out)
