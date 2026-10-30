"""Still images: JPEG, PNG, GIF, BMP, TIFF-based RAW (CR2/NEF/ARW/DNG/ORF/RW2/PEF...), RAF, PSD."""
import struct
import zlib

from .base import Handler, u16be, u16le, u32be, u32le

# ---------------------------------------------------------------- TIFF / EXIF helpers


class Tiff:
    TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}

    def __init__(self, r, base):
        self.r = r
        self.base = base
        h = r.read(base, 8)
        if h[:4] in (b"II*\x00", b"IIRO", b"IIRS", b"IIU\x00"):
            self.le = True
        elif h[:4] == b"MM\x00*":
            self.le = False
        else:
            raise ValueError("no tiff")
        self.first = self.u32(h, 4)
        self.end = 8
        self.tags = {}

    def u16(self, b, o):
        return u16le(b, o) if self.le else u16be(b, o)

    def u32(self, b, o):
        return u32le(b, o) if self.le else u32be(b, o)

    def _values(self, typ, cnt, raw, voff):
        sz = self.TYPE_SIZE.get(typ, 1) * cnt
        data = raw if sz <= 4 else self.r.read(self.base + voff, min(sz, 1 << 16))
        if sz > 4:
            self.end = max(self.end, voff + sz)
        if typ == 3:
            return [self.u16(data, 2 * i) for i in range(min(cnt, len(data) // 2))]
        if typ in (4, 13):
            return [self.u32(data, 4 * i) for i in range(min(cnt, len(data) // 4))]
        if typ == 2:
            return data[:cnt].split(b"\0")[0].decode("latin-1", "replace").strip()
        return data

    def walk(self, limit_ifds=64):
        todo = [self.first]
        seen = set()
        pairs = [(0x111, 0x117), (0x144, 0x145), (0x201, 0x202), (0x2BC, None)]
        while todo and len(seen) < limit_ifds:
            off = todo.pop()
            if off in seen or off < 8 or self.base + off >= self.r.size:
                continue
            seen.add(off)
            n_raw = self.r.read(self.base + off, 2)
            if len(n_raw) < 2:
                continue
            n = self.u16(n_raw, 0)
            if n == 0 or n > 1000:
                continue
            ifd = self.r.read(self.base + off + 2, n * 12 + 4)
            self.end = max(self.end, off + 2 + n * 12 + 4)
            vals = {}
            for i in range(n):
                e = ifd[i * 12:i * 12 + 12]
                if len(e) < 12:
                    break
                tag, typ, cnt = self.u16(e, 0), self.u16(e, 2), self.u32(e, 4)
                if typ not in self.TYPE_SIZE or cnt > 1 << 24:
                    continue
                v = self._values(typ, cnt, e[8:12], self.u32(e, 8))
                vals[tag] = v
                self.tags.setdefault(tag, v)
                if tag in (0x14A, 0x8769, 0x8825, 0xA005) and isinstance(v, list):
                    todo.extend(v)
                if tag == 0x927C and typ == 7:  # MakerNote blob
                    pass
            for a, b in pairs:
                av = vals.get(a)
                if b is None:
                    continue
                bv = vals.get(b)
                if isinstance(av, list) and isinstance(bv, list):
                    if len(av) == 1 and len(bv) == 1:
                        self.end = max(self.end, av[0] + bv[0])
                    else:
                        for o, c in zip(av, bv):
                            self.end = max(self.end, o + c)
            nxt = self.u32(ifd, n * 12) if len(ifd) >= n * 12 + 4 else 0
            if nxt:
                todo.append(nxt)
        return self.end


def exif_meta(tags):
    m = {}
    for t, k in ((0x10F, "marca"), (0x110, "modelo"), (0x9003, "fecha"), (0x132, "fecha")):
        v = tags.get(t)
        if isinstance(v, str) and v and k not in m:
            m[k] = v
    return m


# ---------------------------------------------------------------- JPEG


class JPEGHandler(Handler):
    key = "jpg"
    name = "JPEG"
    category = "photo"
    ext = "jpg"
    signatures = ((0, b"\xff\xd8\xff"),)
    max_size = 256 << 20

    def probe(self, r, off):
        pos = off + 2
        meta = {}
        segs = 0
        while True:
            h = r.read(pos, 4)
            if len(h) < 4 or h[0] != 0xFF:
                return None
            m = h[1]
            if m == 0xFF:
                pos += 1
                continue
            if m == 0xD9:
                pos += 2
                break
            if 0xD0 <= m <= 0xD7 or m == 0x01:
                pos += 2
                continue
            ln = u16be(h, 2)
            if ln < 2:
                return None
            segs += 1
            if m in (0xC0, 0xC1, 0xC2, 0xC3) and "ancho" not in meta:
                sof = r.read(pos + 4, 6)
                meta["alto"], meta["ancho"] = u16be(sof, 1), u16be(sof, 3)
            if m == 0xE1 and "marca" not in meta:
                app = r.read(pos + 4, 10)
                if app.startswith(b"Exif\0\0"):
                    try:
                        t = Tiff(r, pos + 10)
                        t.walk(4)
                        meta.update(exif_meta(t.tags))
                    except Exception:
                        pass
            pos += 2 + ln
            if m == 0xDA:
                end = self._scan_entropy(r, pos, off)
                if end < 0:
                    return self.found(off, min(r.size, pos) - off, complete=False, confidence=0.5,
                                      meta=meta, note="sin marcador de fin (imagen parcial)")
                pos = end
                nxt = r.read(pos, 2)
                if nxt == b"\xff\xd9":
                    pos += 2
                    break
                # progressive JPEG: more tables/scans follow
            if pos - off > self.max_size:
                return None
        if segs < 2:
            return None
        return self.found(off, pos - off, meta=meta, confidence=0.95)

    def _scan_entropy(self, r, pos, off):
        """Return offset of the next real marker after entropy-coded data, or -1."""
        while pos - off < self.max_size:
            buf = r.read(pos, 1 << 18)
            if len(buf) < 2:
                return -1
            i = 0
            while True:
                i = buf.find(b"\xff", i)
                if i < 0 or i + 1 >= len(buf):
                    break
                n = buf[i + 1]
                if n == 0 or 0xD0 <= n <= 0xD7 or n == 0xFF:
                    i += 1 if n == 0xFF else 2
                    continue
                return pos + i
            pos += max(1, len(buf) - 1)
        return -1


# ---------------------------------------------------------------- PNG


class PNGHandler(Handler):
    key = "png"
    name = "PNG"
    category = "photo"
    ext = "png"
    signatures = ((0, b"\x89PNG\r\n\x1a\n"),)

    def probe(self, r, off):
        pos = off + 8
        meta = {}
        first = True
        while True:
            h = r.read(pos, 8)
            if len(h) < 8:
                return None
            ln, typ = u32be(h), h[4:8]
            if ln > 1 << 31 or not typ.isalpha():
                return None
            if first:
                if typ != b"IHDR" or ln != 13:
                    return None
                body = r.read(pos + 8, 17)
                if zlib.crc32(typ + body[:13]) != u32be(body, 13):
                    return None
                meta = {"ancho": u32be(body), "alto": u32be(body, 4)}
                first = False
            pos += 12 + ln
            if typ == b"IEND":
                return self.found(off, pos - off, meta=meta, confidence=0.99)
            if pos > r.size:
                return self.found(off, r.size - off, meta=meta, complete=False, note="truncado")


# ---------------------------------------------------------------- GIF


class GIFHandler(Handler):
    key = "gif"
    name = "GIF"
    category = "photo"
    ext = "gif"
    signatures = ((0, b"GIF87a"), (0, b"GIF89a"))

    def probe(self, r, off):
        h = r.read(off, 13)
        if len(h) < 13:
            return None
        w, hh, flags = u16le(h, 6), u16le(h, 8), h[10]
        pos = off + 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)
        buf_base, buf = pos, r.read(pos, 1 << 20)

        def byte(p):
            nonlocal buf_base, buf
            if not buf_base <= p < buf_base + len(buf):
                buf_base, buf = p, r.read(p, 1 << 20)
                if not buf:
                    return None
            return buf[p - buf_base]

        def skip_sub(p):
            while True:
                n = byte(p)
                if n is None:
                    return None
                p += 1 + n
                if n == 0:
                    return p

        frames = 0
        while True:
            b = byte(pos)
            if b is None:
                return None
            if b == 0x3B:
                pos += 1
                break
            if b == 0x21:
                pos = skip_sub(pos + 2)
            elif b == 0x2C:
                d = r.read(pos, 10)
                if len(d) < 10:
                    return None
                lf = d[9]
                pos += 10 + (3 * (2 << (lf & 7)) if lf & 0x80 else 0) + 1
                pos = skip_sub(pos)
                frames += 1
            else:
                return None
            if pos is None or pos - off > 512 << 20:
                return None
        return self.found(off, pos - off, meta={"ancho": w, "alto": hh, "frames": frames}, confidence=0.97)


# ---------------------------------------------------------------- BMP


class BMPHandler(Handler):
    key = "bmp"
    name = "BMP"
    category = "photo"
    ext = "bmp"
    signatures = ((0, b"BM"),)

    def probe(self, r, off):
        h = r.read(off, 30)
        if len(h) < 30:
            return None
        size, res, data_off, dib = u32le(h, 2), u32le(h, 6), u32le(h, 10), u32le(h, 14)
        if res != 0 or dib not in (12, 40, 52, 56, 108, 124) or not 26 <= data_off < size or size > 1 << 30:
            return None
        w, hh = struct.unpack_from("<ii", h, 18)
        if not 0 < w < 65536 or not 0 < abs(hh) < 65536:
            return None
        return self.found(off, size, meta={"ancho": w, "alto": abs(hh)}, confidence=0.8)


# ---------------------------------------------------------------- TIFF & camera RAW

MAKE_EXT = {"NIKON": "nef", "SONY": "arw", "CANON": "cr2", "PENTAX": "pef", "RICOH": "pef", "OLYMPUS": "orf",
            "OM DIGITAL": "orf", "PANASONIC": "rw2", "LEICA": "rwl", "SAMSUNG": "srw", "HASSELBLAD": "3fr",
            "PHASE ONE": "iiq", "KODAK": "dcr", "MAMIYA": "mef", "EPSON": "erf", "SIGMA": "x3f"}


class TIFFHandler(Handler):
    key = "tif"
    name = "TIFF / RAW de cámara"
    category = "photo"
    ext = "tif"
    signatures = ((0, b"II*\x00"), (0, b"MM\x00*"), (0, b"IIRO"), (0, b"IIRS"), (0, b"IIU\x00"))
    max_size = 2 << 30

    def probe(self, r, off):
        try:
            t = Tiff(r, off)
            if not 8 <= t.first < 1 << 28:
                return None
            end = t.walk()
        except Exception:
            return None
        if not t.tags or end <= 8 or end > self.max_size:
            return None
        head = r.read(off, 12)
        meta = exif_meta(t.tags)
        make = (meta.get("marca") or "").upper()
        ext = "tif"
        if 0xC612 in t.tags:
            ext = "dng"
        elif head[8:10] == b"CR":
            ext = "cr2"
        elif head[:4] in (b"IIRO", b"IIRS"):
            ext = "orf"
        elif head[:4] == b"IIU\x00":
            ext = "rw2"
        else:
            for k, v in MAKE_EXT.items():
                if make.startswith(k):
                    ext = v
                    break
            if ext == "arw" and b"SR2" in r.read(off, 4096):
                ext = "sr2"
        if 0xC612 in t.tags or 0x111 in t.tags or 0x201 in t.tags or 0x144 in t.tags:
            return self.found(off, end, ext=ext, meta=meta, confidence=0.85,
                              note="tamaño calculado desde las IFD (máximo dato referenciado)")
        return None


class RAFHandler(Handler):
    key = "raf"
    name = "Fujifilm RAF"
    category = "photo"
    ext = "raf"
    signatures = ((0, b"FUJIFILMCCD-RAW "),)

    def probe(self, r, off):
        h = r.read(off, 160)
        if len(h) < 108:
            return None
        end = max(u32be(h, 84) + u32be(h, 88), u32be(h, 92) + u32be(h, 96), u32be(h, 100) + u32be(h, 104))
        if not 160 < end < 1 << 30:
            return None
        model = h[28:60].split(b"\0")[0].decode("latin-1", "replace")
        return self.found(off, end, meta={"modelo": model})


class PSDHandler(Handler):
    key = "psd"
    name = "Photoshop PSD/PSB"
    category = "photo"
    ext = "psd"
    signatures = ((0, b"8BPS\x00\x01"), (0, b"8BPS\x00\x02"))
    max_size = 8 << 30

    def probe(self, r, off):
        h = r.read(off, 26)
        if len(h) < 26:
            return None
        ver, ch, hh, w, depth = u16be(h, 4), u16be(h, 12), u32be(h, 14), u32be(h, 18), u16be(h, 22)
        if not 1 <= ch <= 56 or depth not in (1, 8, 16, 32) or not hh or not w:
            return None
        pos = off + 26
        for big in (False, False, ver == 2):  # color mode, resources, layers (8 bytes in PSB)
            n = 8 if big else 4
            b = r.read(pos, n)
            if len(b) < n:
                return None
            pos += n + int.from_bytes(b, "big")
        comp = u16be(r.read(pos, 2))
        pos += 2
        rows = hh * ch
        if comp == 0:
            pos += rows * ((w * depth + 7) // 8)
        elif comp == 1:
            cw = 4 if ver == 2 else 2
            if rows * cw > 64 << 20:
                return None
            tbl = r.read(pos, rows * cw)
            pos += rows * cw + sum(int.from_bytes(tbl[i:i + cw], "big") for i in range(0, len(tbl), cw))
        else:
            return None
        if pos - off > self.max_size:
            return None
        return self.found(off, pos - off, ext="psb" if ver == 2 else "psd",
                          meta={"ancho": w, "alto": hh, "canales": ch}, complete=pos <= r.size)


HANDLERS = [JPEGHandler(), PNGHandler(), GIFHandler(), BMPHandler(), TIFFHandler(), RAFHandler(), PSDHandler()]
