"""Scan engine: filesystem pass, then deep signature pass over every sector.

Mirrors the pipeline Recoverit runs in its drengsrv service (CFormat filesystem
scan -> CRawScanOperator signature scan), with structure-aware length
detection from ``formats`` so carved files come out whole.
"""
import os
import threading
import time
from dataclasses import dataclass, field

from . import formats
from .formats import HANDLERS
from .fs import open_fs, volumes
from .source import Source

CHUNK = 16 << 20
MAX_PIVOT = 0x8001 + 16


@dataclass
class Item:
    id: int
    group: str  # existing | deleted | orphan | raw
    name: str
    path: str
    ext: str
    category: str
    size: int
    start: int
    extents: list = None
    resident: bytes = None
    complete: bool = True
    confidence: float = 1.0
    fmt: str = ""
    meta: dict = field(default_factory=dict)
    note: str = ""
    mtime: str = ""
    fs: str = ""

    def to_dict(self):
        return {"id": self.id, "group": self.group, "name": self.name, "path": self.path, "ext": self.ext,
                "category": self.category, "size": self.size, "start": self.start, "complete": self.complete,
                "confidence": round(self.confidence, 2), "fmt": self.fmt, "meta": self.meta, "note": self.note,
                "mtime": self.mtime, "fs": self.fs,
                "fragments": len(self.extents) if self.extents else (0 if self.resident is not None else 1)}


EXT_CATEGORY = {}
for _h in HANDLERS:
    EXT_CATEGORY.setdefault(_h.ext, _h.category)
EXT_CATEGORY.update({e: "video" for e in ("mxf", "mov", "mp4", "mts", "m2ts", "avi", "mkv", "r3d", "braw", "crm", "mpg",
                                          "vob", "wmv", "flv", "3gp", "webm", "dv", "ts", "m4v", "insv", "lrv")})
EXT_CATEGORY.update({e: "photo" for e in ("jpg", "jpeg", "png", "heic", "dng", "cr2", "cr3", "nef", "arw", "raf", "orf",
                                          "rw2", "tif", "tiff", "psd", "gif", "bmp", "webp", "dpx", "exr", "ari")})
EXT_CATEGORY.update({e: "audio" for e in ("wav", "mp3", "aac", "m4a", "flac", "aif", "aiff", "ogg", "opus", "bwf")})
EXT_CATEGORY.update({e: "document" for e in ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "xml",
                                             "mhl", "pages", "numbers", "key", "csv", "odt", "ods", "odp")})
EXT_CATEGORY.update({e: "archive" for e in ("zip", "rar", "7z", "gz", "tar", "dmg", "iso", "bz2", "xz")})


def category_of(name):
    return EXT_CATEGORY.get(name.rsplit(".", 1)[-1].lower() if "." in name else "", "other")


def _build_pivots(handlers):
    """Group signatures by the absolute offset of their first non-zero byte.

    For each group we take a strided slice of the buffer (one byte per aligned
    sector) and filter it in C with ``translate``; only sectors whose pivot byte
    could start a signature reach Python.
    """
    groups = {}
    for h in handlers:
        for sig_off, magic in h.signatures:
            k = next((i for i, c in enumerate(magic) if c), 0)
            groups.setdefault(sig_off + k, {}).setdefault(magic[k], []).append((h, sig_off, magic))
    out = []
    for pivot, by_byte in groups.items():
        table = bytes(1 if i in by_byte else 0 for i in range(256))
        out.append((pivot, table, by_byte))
    return out


class Scan:
    """One scan session.

    Options mirror Recoverit's ``ScanRequset`` (see docs/REVERSE_ENGINEERING.md):
    ``deep`` (quick = filesystem only), ``categories``/``exts`` (raw_file_types),
    ``scope`` (scan_path: only entries under this folder of the volume),
    ``lost_partitions`` (search unpartitioned space for deleted volumes) and
    ``resume_from`` (continue a deep pass from a saved session).
    """

    def __init__(self, path, deep=True, categories=None, align=None, volume_index=None, log=None,
                 exts=None, scope=None, lost_partitions=True, resume_from=0, label="", mode="disk"):
        self.path = path
        self.deep = deep
        self.categories = set(categories) if categories else None
        self.exts = {e.lower().lstrip(".") for e in exts} if exts else None
        self.scope = "/" + scope.strip("/") if scope and scope.strip("/") else None
        self.lost_partitions = lost_partitions
        self.resume_from = resume_from
        self.label = label or os.path.basename(path)
        self.mode = mode
        self.align = align
        self.volume_index = volume_index
        self.paused = threading.Event()
        self.current_path = ""
        self.lost = []
        self._src = None
        self.items = []
        self.volumes = []
        self.phase = "preparando"
        self.progress = 0.0
        self.bytes_done = 0
        self.bytes_total = 0
        self.speed = 0.0
        self.started = time.time()
        self.finished = None
        self.error = ""
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.log = log or (lambda m: None)
        self.bad_sectors = 0
        self._starts = {}

    # ------------------------------------------------------------------ bookkeeping

    def _add(self, **kw):
        cat = kw.get("category")
        if self.categories and cat not in self.categories:
            return None
        if self.exts and kw.get("ext") not in self.exts:
            return None
        if self.scope and kw.get("group") != "raw" and not (kw.get("path") or "").startswith(self.scope + "/"):
            return None
        with self.lock:
            it = Item(id=len(self.items), **kw)
            self.items.append(it)
            return it

    def status(self):
        counts = {}
        for it in self.items:
            counts[it.group] = counts.get(it.group, 0) + 1
        el = (self.finished or time.time()) - self.started
        eta = (self.bytes_total - self.bytes_done) / self.speed if self.speed > 0 and self.phase == "profundo" else None
        bad = len(self._src.bad_ranges) if self._src is not None else self.bad_sectors
        return {"phase": self.phase, "progress": round(self.progress, 4), "bytes_done": self.bytes_done,
                "bytes_total": self.bytes_total, "speed": self.speed, "eta": eta, "elapsed": el,
                "found": len(self.items), "groups": counts, "error": self.error, "done": self.finished is not None,
                "bad_sectors": bad, "volumes": [v.to_dict() for v in self.volumes], "paused": self.paused.is_set(),
                "current_path": self.current_path, "lost_partitions": [v.to_dict() for v in self.lost],
                "deep": self.deep, "mode": self.mode, "label": self.label, "source": self.path,
                "resume_offset": getattr(self, "resume_offset", 0)}

    # ------------------------------------------------------------------ run

    def run(self):
        try:
            with Source(self.path) as src:
                self._src = src
                self.src_size = src.size
                self._run(src)
                self.bad_sectors = len(src.bad_ranges)
                self.bad_ranges = list(src.bad_ranges)
                self._src = None
        except Exception as e:  # surfaced in the UI; the scan thread must not die silently
            import traceback
            self.error = "%s: %s" % (type(e).__name__, e)
            self.log(traceback.format_exc())
        finally:
            self.finished = time.time()
            if self.phase != "cancelado":
                self.phase = "error" if self.error else "terminado"
            self.progress = 1.0 if not self.error else self.progress

    def _run(self, src):
        self.phase = "sistemas de archivos"
        self.volumes = volumes(src)
        if not self.volumes:
            from .fs import Volume
            self.volumes = [Volume(0, src.size, "?", scheme="sin tabla")]
        targets = self.volumes if self.volume_index is None else [self.volumes[self.volume_index]]
        if self.lost_partitions and self.volume_index is None and not (
                len(self.volumes) == 1 and self.volumes[0].scheme == "volumen"):
            self.phase = "buscando particiones perdidas"
            try:
                from .partitions import find_lost
                self.lost = find_lost(src, [v for v in self.volumes if v.fstype != "?" or v.scheme != "sin tabla"],
                                      self.cancel, lambda p: setattr(self, "progress", p * 0.05))
            except Exception as e:
                self.log("búsqueda de particiones: %s" % e)
                self.lost = []
            if self.lost:
                self.volumes = [v for v in self.volumes if v.scheme != "sin tabla"] + self.lost
            targets = targets + self.lost
            self.phase = "sistemas de archivos"
        fs_parsers = []
        for v in targets:
            try:
                fs = open_fs(src, v)
            except Exception as e:
                self.log("no se pudo abrir %s: %s" % (v.fstype, e))
                fs = None
            if not fs:
                continue
            v.label = v.label or getattr(fs, "label", "")
            fs_parsers.append((v, fs))
            try:
                entries = [] if self.resume_from else fs.walk()  # resumed sessions already hold these
            except Exception as e:
                self.log("error recorriendo %s: %s" % (fs.name, e))
                entries = []
            v.label = v.label or getattr(fs, "label", "")
            for fe in entries:
                if self.cancel.is_set():
                    self.phase = "cancelado"
                    return
                if v.scheme == "perdida":
                    fe.path = "/[%s perdida @%d MB]%s" % (v.fstype.upper(), v.offset >> 20, fe.path)
                self._add_entry(src, fe)
        if not self.deep:
            return
        self.phase = "profundo"
        if self.volume_index is not None:
            start, end = targets[0].offset, targets[0].offset + targets[0].size
        else:
            start, end = 0, src.size
        align = self.align or 512
        if self.resume_from:
            start = max(start, min(end, self.resume_from))
        self._deep(src, start, end, align, fs_parsers)
        self._finish_generic(src)

    def _add_entry(self, src, fe):
        name = fe.path.rsplit("/", 1)[-1]
        self.current_path = fe.path
        ext = name.rsplit(".", 1)[-1].lower() if "." in name.lstrip(".") else ""
        appledouble = name.startswith("._")
        group = "orphan" if fe.origin == "orphan" else "deleted" if fe.deleted else "existing"
        note = fe.note
        start = fe.extents[0][0] if fe.extents and fe.extents[0][0] is not None else -1
        fmt = ""
        if appledouble:
            note = (note + "; " if note else "") + "metadatos macOS (AppleDouble)"
        elif fe.deleted and start >= 0 and fe.size >= 16:
            # header-only check (first 4 MB) so phase 1 stays fast even with hundreds of large clips
            f = formats.probe_any(_HeadOnly(src, start, min(fe.size, 4 << 20)), 0)
            if f:
                fmt = f.fmt
                note = (note + "; " if note else "") + "cabecera válida (%s)" % f.ext.upper()
            else:
                head = src.read(start, 16)
                if head == b"\0" * 16:
                    note = (note + "; " if note else "") + "inicio a ceros: probablemente sobrescrito"
                elif ext in ("mxf", "mov", "mp4", "jpg", "png", "pdf", "zip", "r3d", "braw"):
                    note = (note + "; " if note else "") + "la cabecera no corresponde: posible sobrescritura"
        it = self._add(group=group, name=name, path=fe.path, ext=ext,
                       category="system" if appledouble else category_of(name), size=fe.size,
                       start=start, extents=fe.extents, resident=fe.resident, complete=not fe.note,
                       confidence=1.0 if not fe.deleted else 0.8, fmt=fmt, note=note, mtime=fe.mtime, fs=fe.fs)
        if it and start >= 0:
            self._starts[start] = it

    def _deep(self, src, start, end, align, fs_parsers):
        handlers = [h for h in HANDLERS if not self.categories or h.category in self.categories
                    or h.key in ("bmff", "riff", "zip")]
        pivots = _build_pivots(handlers)
        exfats = [fs for v, fs in fs_parsers if fs.name == "exFAT"]
        self.bytes_total = end - start
        pos = start - start % align
        skip_until = pos
        t0, done0 = time.time(), 0
        orphan_seen = set()
        while pos < end:
            while self.paused.is_set() and not self.cancel.is_set():
                self.phase = "en pausa"
                time.sleep(0.2)
            if self.cancel.is_set():
                self.phase = "cancelado"
                return
            self.phase = "profundo"
            self.resume_offset = pos
            self.current_path = "sector %d" % (pos // 512)
            n = min(CHUNK, end - pos)
            buf = src.read(pos, n + MAX_PIVOT)
            if not buf:
                break
            cands = set()
            for pivot, table, by_byte in pivots:
                strided = buf[pivot:n + pivot:align]
                hits = strided.translate(table)
                i = hits.find(1)
                while i >= 0:
                    cands.add(i)
                    i = hits.find(1, i + 1)
            for i in sorted(cands):
                off = pos + i * align
                if off < skip_until or off >= end:
                    continue
                rel = i * align
                f = self._probe_at(src, buf, rel, off, pivots)
                if f:
                    skip_until = max(skip_until, self._record(f))
            for fs in exfats:
                for fe in fs.orphans_in(buf[:n], pos):
                    k = (fe.path.rsplit("/", 1)[-1], fe.size, fe.extents[0][0] if fe.extents else None)
                    known = self._starts.get(k[2])
                    if known and known.group == "raw":
                        # carved earlier without a name: the orphan entry tells us what it was called
                        known.group, known.name, known.path = "orphan", k[0], fe.path
                        known.mtime, known.extents, known.size = fe.mtime, fe.extents, fe.size
                        known.note = (fe.note + "; " + known.note).strip("; ")
                        orphan_seen.add(k)
                        continue
                    if k in orphan_seen or (known and known.size == fe.size):
                        continue
                    orphan_seen.add(k)
                    self._add_entry(src, fe)
            pos += n
            self.bytes_done = pos - start
            self.progress = self.bytes_done / max(1, self.bytes_total)
            dt = time.time() - t0
            if dt > 1:
                self.speed = (self.bytes_done - done0) / dt
                t0, done0 = time.time(), self.bytes_done

    def _probe_at(self, src, buf, rel, off, pivots):
        best = None
        tried = set()
        for pivot, table, by_byte in pivots:
            b = buf[rel + pivot] if rel + pivot < len(buf) else None
            for h, sig_off, magic in by_byte.get(b, ()):
                if h.key in tried or buf[rel + sig_off:rel + sig_off + len(magic)] != magic:
                    continue
                tried.add(h.key)
                try:
                    f = h.probe(src, off)
                except Exception:
                    f = None
                if f and (best is None or f.confidence > best.confidence):
                    best = f
        return best

    def _record(self, f):
        """Store a carved file; return the offset scanning may skip to."""
        known = self._starts.get(f.start)
        if known:
            # same data the filesystem already gave us: enrich it, don't duplicate
            known.meta.update(f.meta)
            known.fmt = known.fmt or f.fmt
            if known.group != "existing" and f.complete and f.length != known.size:
                known.note = (known.note + "; " if known.note else "") + \
                    "la estructura del formato mide %d bytes (el sistema de archivos dice %d)" % (f.length, known.size)
            if f.note and known.group != "existing":
                known.note = (known.note + "; " if known.note else "") + f.note
            return f.start + (f.length if f.complete and f.confidence >= 0.9 else 0)
        name = f.name or "%s_%012X.%s" % (f.ext.upper(), f.start, f.ext)
        it = self._add(group="raw", name=name, path="", ext=f.ext, category=f.category, size=f.length,
                       start=f.start, complete=f.complete, confidence=f.confidence, fmt=f.fmt, meta=f.meta,
                       note=f.note)
        if it:
            self._starts[f.start] = it
        if f.complete and f.confidence >= 0.9 and f.length > 0:
            return f.start + f.length
        return f.start

    def _finish_generic(self, src):
        """Formats without structural length end at the next file start (or their max size)."""
        starts = sorted(self._starts)
        import bisect
        for it in self.items:
            if it.group == "raw" and it.size == 0:
                h = formats.BY_KEY.get(it.fmt)
                k = bisect.bisect_right(starts, it.start)
                nxt = starts[k] if k < len(starts) else src.size
                it.size = max(0, min(nxt - it.start, h.max_size if h else 64 << 20, src.size - it.start))


class _HeadOnly:
    """Reader limited to one file's first extent, so header checks can't run past it."""

    def __init__(self, src, start, size):
        self.src, self.start, self.size = src, start, size

    def read(self, off, n):
        if off >= self.size:
            return b""
        return self.src.read(self.start + off, min(n, self.size - off))


def run_in_thread(scan):
    t = threading.Thread(target=scan.run, daemon=True)
    t.start()
    return t


def quick_info(path):
    with Source(path) as s:
        return {"size": s.size, "sector": s.sector_size, "device": s.is_device,
                "volumes": [v.to_dict() for v in volumes(s)], "name": os.path.basename(path)}
