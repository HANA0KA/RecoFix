"""Common types for format handlers.

A handler knows the signatures that can start a file of its format and, given a
reader positioned anywhere, decides whether a real file starts there and how
long it is. Lengths come from the format's own structure (box sizes, KLV
lengths, end markers), never from a fixed guess, so recovered files are exact.
"""
from dataclasses import dataclass, field

CATEGORIES = ("video", "photo", "audio", "document", "archive", "other")


@dataclass
class Found:
    fmt: str
    ext: str
    category: str
    start: int
    length: int
    complete: bool = True
    confidence: float = 0.9
    name: str = ""
    meta: dict = field(default_factory=dict)
    note: str = ""

    def to_dict(self):
        return {
            "fmt": self.fmt, "ext": self.ext, "category": self.category,
            "start": self.start, "length": self.length, "complete": self.complete,
            "confidence": round(self.confidence, 2), "name": self.name,
            "meta": self.meta, "note": self.note,
        }


class Handler:
    key = ""
    name = ""
    category = "other"
    ext = ""
    # (offset of the magic inside the file, magic bytes)
    signatures = ()
    max_size = 1 << 40

    def probe(self, r, off):
        """Return Found if a file of this format starts at ``off`` in reader ``r``."""
        raise NotImplementedError

    def found(self, off, length, **kw):
        kw.setdefault("ext", self.ext)
        return Found(self.key, kw.pop("ext"), self.category, off, length, **kw)


def u16le(b, o=0):
    return int.from_bytes(b[o:o + 2], "little")


def u32le(b, o=0):
    return int.from_bytes(b[o:o + 4], "little")


def u16be(b, o=0):
    return int.from_bytes(b[o:o + 2], "big")


def u32be(b, o=0):
    return int.from_bytes(b[o:o + 4], "big")


def u64be(b, o=0):
    return int.from_bytes(b[o:o + 8], "big")


def find_marker(r, off, marker, limit, chunk=1 << 20):
    """Offset of the first ``marker`` at or after ``off`` within ``limit`` bytes, or -1."""
    pos = off
    end = min(r.size, off + limit)
    keep = len(marker) - 1
    while pos < end:
        buf = r.read(pos, min(chunk, end - pos) + keep)
        i = buf.find(marker)
        if i >= 0:
            return pos + i
        if len(buf) <= keep:
            break
        pos += len(buf) - keep
    return -1
