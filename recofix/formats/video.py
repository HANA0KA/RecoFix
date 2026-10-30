"""Video containers: ISO BMFF/QuickTime, RIFF/AVI, Matroska, MPEG-TS/M2TS, MPEG-PS, RED R3D."""
import struct

from .base import Handler, u16be, u32be, u32le, u64be

# ---------------------------------------------------------------- ISO BMFF / QuickTime

TOP_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"pdin",
             b"moof", b"mfra", b"sidx", b"styp", b"emsg", b"pnot", b"PICT", b"junk", b"prfl",
             b"XMP_", b"ssix", b"prft", b"jP  ", b"jp2h", b"idat", b"iinf", b"cmov"}

BRANDS = {
    b"qt  ": "mov", b"heic": "heic", b"heix": "heic", b"hevc": "heic", b"heim": "heic", b"heis": "heic",
    b"mif1": "heic", b"msf1": "heic", b"avif": "avif", b"avis": "avif", b"crx ": "cr3", b"M4A ": "m4a",
    b"M4B ": "m4b", b"M4P ": "m4p", b"M4V ": "m4v", b"M4VH": "m4v", b"M4VP": "m4v", b"F4V ": "f4v",
    b"f4v ": "f4v", b"XAVC": "mp4", b"jp2 ": "jp2", b"mjp2": "mj2", b"caqv": "mp4",
}

CODECS = {
    b"avc1": "H.264", b"avc3": "H.264", b"hvc1": "H.265/HEVC", b"hev1": "H.265/HEVC", b"dvh1": "Dolby Vision",
    b"av01": "AV1", b"vp09": "VP9", b"mp4v": "MPEG-4", b"mjpa": "Motion JPEG", b"jpeg": "Motion JPEG",
    b"apch": "ProRes 422 HQ", b"apcn": "ProRes 422", b"apcs": "ProRes 422 LT", b"apco": "ProRes 422 Proxy",
    b"ap4h": "ProRes 4444", b"ap4x": "ProRes 4444 XQ", b"aprn": "ProRes RAW", b"aprh": "ProRes RAW HQ",
    b"brxq": "Blackmagic RAW", b"brst": "Blackmagic RAW", b"brvn": "Blackmagic RAW", b"brs2": "Blackmagic RAW",
    b"brxh": "Blackmagic RAW", b"AVdn": "DNxHD", b"AVdh": "DNxHR", b"xdvc": "XDCAM HD", b"xd5c": "XDCAM HD422",
    b"dvc ": "DV", b"dvcp": "DV", b"dv5n": "DVCPRO50", b"dvhd": "DVCPRO HD", b"CFHD": "CineForm",
    b"mp4a": "AAC", b"lpcm": "PCM", b"sowt": "PCM", b"twos": "PCM", b"in24": "PCM 24", b"ipcm": "PCM",
    b"ac-3": "AC-3", b"ec-3": "E-AC-3", b"Opus": "Opus", b"alac": "ALAC", b"fLaC": "FLAC",
}

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta", b"dinf"}


def _boxes(buf, start, end):
    pos = start
    while pos + 8 <= end:
        size = u32be(buf, pos)
        typ = buf[pos + 4:pos + 8]
        hdr = 8
        if size == 1 and pos + 16 <= end:
            size = u64be(buf, pos + 8)
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def parse_moov(buf):
    meta = {}
    tracks = []

    def visit(s, e, path):
        for typ, b, end in _boxes(buf, s, e):
            if typ == b"mvhd" and end - b >= 20:
                v = buf[b]
                ts, dur = (struct.unpack_from(">IQ", buf, b + 20) if v == 1 else struct.unpack_from(">II", buf, b + 12))
                if ts:
                    meta["duracion_s"] = round(dur / ts, 2)
            elif typ == b"tkhd" and end - b >= 84:
                w, h = u32be(buf, end - 8) >> 16, u32be(buf, end - 4) >> 16
                if w and h:
                    meta.setdefault("ancho", w)
                    meta.setdefault("alto", h)
            elif typ == b"stsd" and end - b >= 16:
                tracks.append(buf[b + 12:b + 16])
            elif typ in CONTAINERS:
                visit(b, end, path + [typ])

    visit(0, len(buf), [])
    names = [CODECS.get(t, t.decode("latin-1", "replace")) for t in tracks]
    if names:
        meta["codecs"] = names
    return meta, tracks


class BMFFHandler(Handler):
    key = "bmff"
    name = "MP4 / MOV / HEIC / CR3 / BRAW"
    category = "video"
    ext = "mp4"
    signatures = ((4, b"ftyp"), (4, b"moov"), (4, b"mdat"), (4, b"wide"))
    max_size = 1 << 42

    def probe(self, r, off):
        head = r.read(off, 32)
        if len(head) < 16 or head[4:8] not in TOP_BOXES:
            return None
        pos = off
        seen = []
        moov = None
        brand = b""
        incomplete = ""
        while True:
            h = r.read(pos, 16)
            if len(h) < 8:
                break
            size = u32be(h)
            typ = h[4:8]
            hdr = 8
            if typ not in TOP_BOXES:
                if not seen or not all(32 <= c < 127 for c in typ):
                    break
                # an unknown but printable top box after a valid start: accept it
            if size == 1:
                size = u64be(h, 8)
                hdr = 16
            elif size == 0:
                if typ == b"mdat":
                    incomplete = "mdat sin tamaño (grabación interrumpida)"
                size = r.size - pos
            if size < hdr:
                break
            if pos + size > r.size:
                incomplete = "la caja '%s' sobrepasa el final del medio" % typ.decode("latin-1")
                seen.append(typ)
                pos = r.size
                break
            if typ == b"ftyp":
                if seen:
                    break  # next file
                brand = r.read(pos + 8, 4)
            if typ == b"moov" and size <= 64 << 20:
                moov = (pos + hdr, size - hdr)
            seen.append(typ)
            pos += size
            if len(seen) > 100000:
                break
        if not seen or (seen[0] != b"ftyp" and len(seen) < 2):
            return None
        if seen[0] != b"ftyp" and b"moov" not in seen:
            return None
        ext = BRANDS.get(brand, "")
        if not ext:
            ext = "3gp" if brand.startswith(b"3gp") else "3g2" if brand.startswith(b"3g2") else "mp4"
        meta = {"brand": brand.decode("latin-1").strip() or "-", "cajas": [t.decode("latin-1") for t in seen[:12]]}
        tracks = []
        if moov:
            try:
                m, tracks = parse_moov(r.read(*moov))
                meta.update(m)
            except Exception:
                pass
        if any(t.startswith(b"br") for t in tracks):
            ext = "braw"
        if ext == "mov" and not moov and b"moov" not in seen:
            pass
        still = ext in ("heic", "avif", "cr3", "jp2")
        cat = "photo" if still else ("audio" if ext in ("m4a", "m4b", "m4p") else "video")
        needs = (b"meta",) if still and ext != "cr3" else (b"moov", b"moof")
        complete = not incomplete and any(n in seen for n in needs) and (b"mdat" in seen or still)
        note = incomplete
        if not any(n in seen for n in needs) and not still:
            note = (note + "; " if note else "") + "falta el índice 'moov' (necesita reparación con un clip de referencia)"
        f = self.found(off, pos - off, ext=ext, complete=complete, confidence=0.97 if complete else 0.7,
                       meta=meta, note=note)
        f.category = cat
        return f


# ---------------------------------------------------------------- RIFF (AVI/WAV/WEBP/...) and AIFF

RIFF_FORMS = {b"AVI ": ("avi", "video"), b"WAVE": ("wav", "audio"), b"WEBP": ("webp", "photo"),
              b"ACON": ("ani", "photo"), b"RMID": ("rmi", "audio"), b"CDXA": ("dat", "video"),
              b"AMV ": ("amv", "video"), b"DSDF": ("dff", "audio")}


class RIFFHandler(Handler):
    key = "riff"
    name = "RIFF (AVI, WAV, WEBP)"
    category = "video"
    signatures = ((0, b"RIFF"), (0, b"RF64"), (0, b"BW64"))
    max_size = 1 << 40

    def probe(self, r, off):
        h = r.read(off, 64)
        if len(h) < 12 or h[8:12] not in RIFF_FORMS:
            return None
        ext, cat = RIFF_FORMS[h[8:12]]
        size = u32le(h, 4) + 8
        if h[:4] in (b"RF64", b"BW64"):
            if h[12:16] != b"ds64":
                return None
            size = int.from_bytes(h[20:28], "little") + 8
        if size < 12:
            return None
        end = off + size + (size & 1)
        meta = {}
        if ext == "wav" and h[12:16] == b"fmt ":
            ch, sr = struct.unpack_from("<HI", h, 22)
            meta = {"canales": ch, "audio_hz": sr, "bits": struct.unpack_from("<H", h, 34)[0]}
        if ext == "avi":
            # OpenDML: >1 GB AVIs continue in RIFF AVIX chunks
            while True:
                n = r.read(end, 12)
                if len(n) < 12 or n[:4] != b"RIFF" or n[8:12] != b"AVIX":
                    break
                end += u32le(n, 4) + 8
            if h[12:16] == b"LIST" and h[20:24] == b"hdrl" and h[24:28] == b"avih":
                us, = struct.unpack_from("<I", h, 32)
                if us:
                    meta["fps"] = round(1e6 / us, 3)
        complete = end <= r.size
        f = self.found(off, min(end, r.size) - off, ext=ext, complete=complete,
                       note="" if complete else "truncado por el final del medio", meta=meta)
        f.category = cat
        return f


class AIFFHandler(Handler):
    key = "aiff"
    name = "AIFF / AIFC"
    category = "audio"
    ext = "aiff"
    signatures = ((0, b"FORM"),)

    def probe(self, r, off):
        h = r.read(off, 12)
        if len(h) < 12 or h[8:12] not in (b"AIFF", b"AIFC", b"8SVX"):
            return None
        size = u32be(h, 4) + 8
        ext = {b"AIFF": "aiff", b"AIFC": "aifc", b"8SVX": "8svx"}[h[8:12]]
        return self.found(off, min(size, r.size - off), ext=ext, complete=off + size <= r.size)


# ---------------------------------------------------------------- Matroska / WebM

EBML_L1 = {0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1F43B675, 0x1C53BB6B, 0x1941A469, 0x1043A770,
           0x1254C367, 0xEC, 0xBF}
CLUSTER_CHILD = {0xE7, 0xA3, 0xA0, 0xA7, 0xAB, 0xEC, 0xBF, 0x5854, 0xAF}
UNKNOWN = -1


def vint(b, p, keep_marker=False):
    if p >= len(b) or b[p] == 0:
        return None, 0
    first = b[p]
    n = 1
    while not first & (0x80 >> (n - 1)):
        n += 1
    if p + n > len(b):
        return None, 0
    v = first if keep_marker else first & (0xFF >> n)
    for i in range(1, n):
        v = (v << 8) | b[p + i]
    if not keep_marker and v == (1 << (7 * n)) - 1:
        v = UNKNOWN
    return v, n


def ebml_elem(r, pos):
    h = r.read(pos, 12)
    eid, n1 = vint(h, 0, keep_marker=True)
    if eid is None or n1 > 4:
        return None
    size, n2 = vint(h, n1)
    if size is None:
        return None
    return eid, pos + n1 + n2, size


class EBMLHandler(Handler):
    key = "mkv"
    name = "Matroska / WebM"
    category = "video"
    ext = "mkv"
    signatures = ((0, b"\x1a\x45\xdf\xa3"),)

    def probe(self, r, off):
        e = ebml_elem(r, off)
        if not e or e[0] != 0x1A45DFA3 or e[2] in (UNKNOWN,) or e[2] > 4096:
            return None
        hdr = r.read(e[1], e[2])
        ext = "webm" if b"webm" in hdr else "mkv" if b"matroska" in hdr else None
        if not ext:
            return None
        seg = ebml_elem(r, e[1] + e[2])
        if not seg or seg[0] != 0x18538067:
            return None
        if seg[2] != UNKNOWN:
            end = seg[1] + seg[2]
            return self.found(off, min(end, r.size) - off, ext=ext, complete=end <= r.size,
                              note="" if end <= r.size else "truncado por el final del medio")
        # Live recording: Segment of unknown size. Walk level-1 elements (and unknown-size clusters).
        pos = seg[1]
        while pos < r.size:
            el = ebml_elem(r, pos)
            if not el or el[0] not in EBML_L1:
                break
            if el[2] == UNKNOWN:
                if el[0] != 0x1F43B675:
                    break
                cpos = el[1]
                while cpos < r.size:
                    c = ebml_elem(r, cpos)
                    if not c or c[0] not in CLUSTER_CHILD or c[2] == UNKNOWN or c[1] + c[2] > r.size:
                        break
                    cpos = c[1] + c[2]
                pos = cpos
                continue
            if el[1] + el[2] > r.size:
                break
            pos = el[1] + el[2]
        return self.found(off, pos - off, ext=ext, complete=False, confidence=0.8,
                          note="segmento de tamaño desconocido (grabación en vivo); recortado al último elemento válido")


# ---------------------------------------------------------------- MPEG transport stream (TS, M2TS/MTS AVCHD)

class TSHandler(Handler):
    key = "ts"
    name = "MPEG-TS / M2TS (AVCHD)"
    category = "video"
    ext = "ts"
    signatures = ((0, b"\x47"), (4, b"\x47"))

    def probe(self, r, off):
        for stride, lead, ext in ((192, 4, "m2ts"), (188, 0, "ts")):
            b = r.read(off, stride * 64)
            if len(b) < stride * 64:
                continue
            if all(b[i * stride + lead] == 0x47 for i in range(64)):
                pid = ((b[lead + 1] & 0x1F) << 8) | b[lead + 2]
                if pid not in (0, 0x11, 0x1F, 0x100, 0x1000, 0x1001):
                    continue
                break
        else:
            return None
        pos = off
        chunk = stride * 8192
        while pos < r.size:
            b = r.read(pos, chunk)
            n = len(b) // stride
            bad = next((i for i in range(n) if b[i * stride + lead] != 0x47), None)
            if bad is not None:
                pos += bad * stride
                break
            pos += n * stride
            if n == 0:
                break
        return self.found(off, pos - off, ext=ext, complete=True, confidence=0.8,
                          note="fin determinado por pérdida de sincronía de paquetes")


# ---------------------------------------------------------------- MPEG program stream (VOB, MPG, MOD, TOD)

class PSHandler(Handler):
    key = "mpg"
    name = "MPEG-PS (VOB/MPG/MOD)"
    category = "video"
    ext = "mpg"
    signatures = ((0, b"\x00\x00\x01\xba"),)

    def probe(self, r, off):
        pos = off
        packs = 0
        ended = False
        chunk_base, chunk = -1, b""
        while True:
            if pos + 64 > chunk_base + len(chunk) or pos < chunk_base:
                chunk_base, chunk = pos, r.read(pos, 1 << 20)
            p = pos - chunk_base
            h = chunk[p:p + 14]
            if len(h) < 4 or h[:3] != b"\x00\x00\x01":
                break
            sid = h[3]
            if sid == 0xBA:
                if len(h) < 14:
                    break
                if h[4] & 0xC0 == 0x40:
                    pos += 14 + (h[13] & 7)
                elif h[4] & 0xF0 == 0x20:
                    pos += 12
                else:
                    break
                packs += 1
            elif sid == 0xB9:
                pos += 4
                ended = True
                # VOB sets often keep going after an end code; continue if another pack follows
                if r.read(pos, 4) != b"\x00\x00\x01\xba":
                    break
            elif sid >= 0xBB:
                pos += 6 + u16be(h, 4)
            else:
                break
        if packs < 4:
            return None
        ext = "vob" if packs and r.read(off + 14, 4) == b"\x00\x00\x01\xbb" and ended else "mpg"
        return self.found(off, pos - off, ext=ext, complete=True, confidence=0.85)


# ---------------------------------------------------------------- RED R3D

class R3DHandler(Handler):
    key = "r3d"
    name = "RED R3D"
    category = "video"
    ext = "r3d"
    signatures = ((4, b"RED1"), (4, b"RED2"))

    def probe(self, r, off):
        pos = off
        n = 0
        while pos < r.size:
            h = r.read(pos, 8)
            if len(h) < 8:
                break
            size, typ = u32be(h), h[4:8]
            if size < 8 or not (typ[:2] in (b"RE", b"RD") and all(48 <= c < 123 for c in typ)):
                break
            if typ in (b"RED1", b"RED2") and n:
                break  # next clip segment
            if pos + size > r.size:
                break
            pos += size
            n += 1
        if n < 2:
            return None
        return self.found(off, pos - off, complete=True, confidence=0.9, meta={"atomos": n},
                          note="los clips R3D largos se dividen en _001.R3D, _002.R3D…")


HANDLERS = [BMFFHandler(), RIFFHandler(), AIFFHandler(), EBMLHandler(), TSHandler(), PSHandler(), R3DHandler()]
