"""Fragmented video reassembly (Recoverit "Advanced Video Recovery").

When a deleted MP4/MOV was stored in several fragments, carving from its
``ftyp`` gives a file whose ``mdat`` ends in someone else's data and whose
``moov`` is elsewhere on the disk. libscanopreator solves this with
``mp4::FragmentSample``/``matchFragmentToContainers``: the sample table in
``moov`` says where every frame must be *inside the file*, and each frame can
be recognised on disk (NAL-unit chain of the exact size, or a ProRes ``icpf``
frame header).

RecoFix does the same:

1. Find the clip's ``moov`` after its ``ftyp`` (validated: ``mvhd`` + ``trak``).
2. Map file offsets to disk offsets, starting with the fragment at ``ftyp``.
3. Walk the video samples in file order. While a frame validates where the
   current mapping predicts, the fragment continues. When it stops
   validating, search the disk (cluster-aligned) for the frame of that exact
   size: that position starts the next fragment.
4. Write the file back in order (``ftyp`` + ``mdat`` + ``moov``).
"""
import struct

from .repair import mp4
from .source import Source

SEARCH_CHUNK = 32 << 20


def _find_moov(src, start, limit):
    pos = start
    end = min(src.size, start + limit)
    while pos < end:
        buf = src.read(pos, min(SEARCH_CHUNK, end - pos) + 16)
        i = buf.find(b"moov")
        while i >= 0:
            if i >= 4 and buf[i + 8:i + 12] in (b"mvhd", b"prfl", b"cmov", b"meta", b"udta", b"iods"):
                size = struct.unpack_from(">I", buf, i - 4)[0]
                if 64 < size < (256 << 20):
                    return pos + i - 4, size
            i = buf.find(b"moov", i + 1)
        if len(buf) <= 16:
            break
        pos += len(buf) - 16
    return None


def _tables(moov):
    """[(track, [(file_offset, size)])] for each track of a parsed moov buffer."""
    out = []
    for typ, s, b, e in mp4.boxes(moov, 8):
        if typ != b"trak":
            continue
        hd = mp4.find(moov, [b"mdia", b"hdlr"], b, e)
        stbl = mp4.find(moov, [b"mdia", b"minf", b"stbl"], b, e)
        if not hd or not stbl:
            continue
        t = {}
        for ct, cs, cb, ce in mp4.boxes(moov, stbl[1], stbl[2]):
            t[ct] = moov[cb:ce]
        stsd = t.get(b"stsd", b"")
        codec = stsd[12:16] if len(stsd) >= 16 else b""
        tr = mp4.Track(moov[hd[1] + 8:hd[1] + 12], codec, b"", 0)
        if tr.handler == b"vide" and len(stsd) > 8:
            entry = stsd[8:]
            for ct, cs, cb, ce in mp4.boxes(entry, 86):
                if ct == b"avcC":
                    tr.nal_len, tr.params = mp4._params_avc(entry[cb:ce])
                elif ct == b"hvcC":
                    tr.nal_len, tr.params = mp4._params_hevc(entry[cb:ce])
        const, sizes = mp4._sample_sizes(t)
        co = t.get(b"stco")
        offs = list(struct.unpack_from(">%dI" % struct.unpack_from(">I", co, 4)[0], co, 8)) if co else []
        co64 = t.get(b"co64")
        if co64:
            offs = list(struct.unpack_from(">%dQ" % struct.unpack_from(">I", co64, 4)[0], co64, 8))
        stsc = t.get(b"stsc", b"\0" * 8)
        runs = [struct.unpack_from(">III", stsc, 8 + 12 * i) for i in range(struct.unpack_from(">I", stsc, 4)[0])]
        samples, k = [], 0
        n_samples = len(sizes) if not const else struct.unpack_from(">I", t.get(b"stsz", b"\0" * 12), 8)[0]
        for ci, off in enumerate(offs, 1):
            spc = next((r[1] for r in reversed(runs) if r[0] <= ci), 1)
            p = off
            for _ in range(spc):
                if k >= n_samples:
                    break
                sz = const or sizes[k]
                samples.append((p, sz))
                p += sz
                k += 1
        out.append((tr, samples))
    return out


class Fragments:
    """Piecewise map file offset -> disk offset."""

    def __init__(self, disk_start):
        self.parts = [(0, disk_start)]

    def disk(self, x):
        f, d = self.parts[0]
        for pf, pd in self.parts:
            if pf <= x:
                f, d = pf, pd
            else:
                break
        return d + (x - f)

    def add(self, file_off, disk_off):
        self.parts = [p for p in self.parts if p[0] < file_off] + [(file_off, disk_off)]


def _validate(src, disk, size, track):
    head = src.read(disk, min(size, 1 << 20) + 8)
    if len(head) < 8:
        return False
    if track.codec in mp4.PRORES:
        return head[4:8] == b"icpf" and int.from_bytes(head[:4], "big") == size
    if size > len(head):
        head = src.read(disk, size)
    return bool(mp4.valid_sample(head, 0, size, track))


def _search(src, x, size, track, near, cluster, window, align_base):
    """Disk offset whose bytes are the sample at file offset ``x`` (cluster-consistent), or None."""
    phase = (align_base + x) % cluster
    for lo, hi in ((near, near + window), (max(0, near - window), near)):
        pos = lo + (phase - lo) % cluster
        while pos < min(hi, src.size):
            buf = src.read(pos, min(SEARCH_CHUNK, hi - pos) + 16)
            if not buf:
                break
            for i in range(0, len(buf) - 8, cluster):
                if track.codec in mp4.PRORES:
                    if buf[i + 4:i + 8] != b"icpf" or int.from_bytes(buf[i:i + 4], "big") != size:
                        continue
                else:
                    ln = int.from_bytes(buf[i:i + track.nal_len], "big")
                    if not (1 < ln <= size - track.nal_len) or buf[i + track.nal_len] & 0x80:
                        continue
                if _validate(src, pos + i, size, track):
                    return pos + i
            pos += max(cluster, len(buf) - len(buf) % cluster)
    return None


def reassemble(source, start, out_path, cluster=32768, window=2 << 30, progress=None, cancel=None):
    """Rebuild a fragmented MP4/MOV that starts (``ftyp``) at disk offset ``start``."""
    with Source(source) as src:
        head = src.read(start, 64)
        if head[4:8] != b"ftyp":
            raise ValueError("no hay cabecera ftyp en el offset indicado")
        pos = 0  # file offset; boxes before mdat (ftyp, free, wide, uuid) are small and sit in the first fragment
        while True:
            h = src.read(start + pos, 16)
            size, typ = struct.unpack_from(">I4s", h, 0)
            hdr = 8
            if size == 1:
                size, hdr = struct.unpack_from(">Q", h, 8)[0], 16
            if typ == b"mdat":
                break
            if typ not in (b"ftyp", b"free", b"skip", b"wide", b"uuid", b"pnot", b"PICT") or size < 8 or pos > (1 << 20):
                raise ValueError("estructura no soportada antes de mdat (%r)" % typ)
            pos += size
        mdat_size, mdat_hdr = size, hdr
        file_moov = pos + mdat_size
        found = _find_moov(src, start + pos + mdat_hdr, max(window, mdat_size + window))
        if not found:
            raise ValueError("no se encontró el índice moov del clip")
        moov_disk, moov_size = found
        moov = src.read(moov_disk, moov_size)
        tracks = _tables(moov)
        video = [(t, s) for t, s in tracks if t.handler == b"vide" and (t.codec in mp4.PRORES or t.params)]
        if not video:
            raise ValueError("códec de vídeo no soportado para reensamblado")
        track, samples = video[0]
        frag = Fragments(start)
        checked = ok = 0
        verified = 0  # file offset up to which the current mapping is proven by frame contents
        header_only = track.codec in mp4.PRORES  # ProRes frames are checked by header, not end to end

        def verified_end(x, size):
            return x + 8 if header_only else x + size

        for idx, (x, size) in enumerate(sorted(samples)):
            if cancel is not None and cancel.is_set():
                break
            checked += 1
            d = frag.disk(x)
            if _validate(src, d, size, track):
                ok += 1
                verified = verified_end(x, size)
            else:
                hit = _search(src, x, size, track, d, cluster, window, start)
                if hit is not None:
                    # fragments start on cluster boundaries: the first one after the last proven byte
                    b = verified + (-(start + verified)) % cluster
                    if b > x:
                        b = x - (start + x) % cluster
                    frag.add(b, hit - (x - b))
                    ok += 1
                    verified = verified_end(x, size)
            if progress and idx % 50 == 0:
                progress(idx / max(1, len(samples)))
        with open(out_path, "wb") as w:
            pos = 0
            while pos < file_moov:
                bounds = [p[0] for p in frag.parts if p[0] > pos] or [file_moov]
                nxt = min(file_moov, min(bounds), pos + (8 << 20))
                w.write(src.read(frag.disk(pos), nxt - pos))
                pos = nxt
            w.write(moov)
        return {"ok": ok == checked and checked > 0, "frames_ok": ok, "frames": checked,
                "fragments": len(frag.parts), "moov_offset": moov_disk, "out": out_path,
                "map": [{"file": f, "disk": d} for f, d in frag.parts]}
