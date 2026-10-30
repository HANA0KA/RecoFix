"""Documents and archives: PDF, ZIP family (Office, OpenDocument, iWork, APK, EPUB...), OLE
(doc/xls/ppt/msg), SQLite, RTF, RAR, 7z, gzip, bzip2, xz, tar, ISO 9660."""
import bz2
import lzma
import re
import struct
import zlib

from .base import Handler, find_marker, u16le, u32be, u32le

# ---------------------------------------------------------------- PDF

PDF_CONT = re.compile(rb"\s*(\d+\s+\d+\s+obj|xref|trailer|startxref|%)")


class PDFHandler(Handler):
    key = "pdf"
    name = "PDF / Illustrator"
    category = "document"
    ext = "pdf"
    signatures = ((0, b"%PDF-"),)
    max_size = 2 << 30

    def probe(self, r, off):
        if not r.read(off + 5, 3)[:1].isdigit():
            return None
        pos = off + 8
        end = -1
        while True:
            i = find_marker(r, pos, b"%%EOF", self.max_size - (pos - off))
            if i < 0:
                break
            end = i + 5
            tail = r.read(end, 2)
            end += len(tail) - len(tail.lstrip(b"\r\n"))
            # incremental update? continue only if more PDF syntax follows immediately
            if not PDF_CONT.match(r.read(end, 64)):
                break
            pos = end
        if end < 0:
            return self.found(off, min(r.size - off, 64 << 20), complete=False, confidence=0.4,
                              note="sin %%EOF: PDF parcial")
        head = r.read(off, 2048)
        ext = "ai" if b"Illustrator" in head else "pdf"
        return self.found(off, end - off, ext=ext, confidence=0.95)


# ---------------------------------------------------------------- ZIP family

ODF = {b"application/vnd.oasis.opendocument.text": "odt",
       b"application/vnd.oasis.opendocument.spreadsheet": "ods",
       b"application/vnd.oasis.opendocument.presentation": "odp",
       b"application/vnd.oasis.opendocument.graphics": "odg",
       b"application/epub+zip": "epub"}


def zip_kind(names, mimetype):
    for k, v in ODF.items():
        if mimetype.startswith(k):
            return v
    s = set(names)
    if "[Content_Types].xml" in s:
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("ppt/") for n in names):
            return "pptx"
        if any(n.startswith("visio/") for n in names):
            return "vsdx"
        if any(n.startswith("3D/") for n in names):
            return "3mf"
    if "AndroidManifest.xml" in s:
        return "apk"
    if any(n.startswith("Payload/") and n.endswith(".app/") for n in names):
        return "ipa"
    if "Index/Document.iwa" in s:
        if any("Tables" in n for n in names) and not any("Slide" in n for n in names):
            return "numbers"
        if any("Slide" in n for n in names):
            return "key"
        return "pages"
    if "META-INF/MANIFEST.MF" in s:
        return "jar"
    if "doc.kml" in s:
        return "kmz"
    if "SketchUp Model" in s or any(n.endswith(".skp") for n in names[:3]):
        return "skp"
    return "zip"


class ZIPHandler(Handler):
    key = "zip"
    name = "ZIP / Office / iWork / APK"
    category = "archive"
    ext = "zip"
    signatures = ((0, b"PK\x03\x04"),)
    max_size = 1 << 40

    def probe(self, r, off):
        h = r.read(off, 30)
        if len(h) < 30 or u16le(h, 4) > 63 or u16le(h, 26) == 0 or u16le(h, 26) > 1024:
            return None
        pos = off + 30
        while True:
            i = find_marker(r, pos, b"PK\x05\x06", min(self.max_size, 4 << 30) - (pos - off))
            if i < 0:
                return None
            e = r.read(i, 22)
            if len(e) < 22:
                return None
            cd_size, cd_off, clen = u32le(e, 12), u32le(e, 16), u16le(e, 20)
            if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
                loc = r.read(i - 20, 20)
                if loc[:4] == b"PK\x06\x07":
                    z64 = off + int.from_bytes(loc[8:16], "little")
                    z = r.read(z64, 56)
                    if z[:4] == b"PK\x06\x06":
                        cd_size = int.from_bytes(z[40:48], "little")
                        cd_off = int.from_bytes(z[48:56], "little")
                        if cd_off + cd_size == z64 - off:
                            return self._done(r, off, i + 22 + clen, off + cd_off, cd_size)
            elif cd_off + cd_size == i - off:
                return self._done(r, off, i + 22 + clen, off + cd_off, cd_size)
            pos = i + 4

    def _done(self, r, off, end, cd, cd_size):
        names = []
        cdb = r.read(cd, min(cd_size, 4 << 20))
        p = 0
        while p + 46 <= len(cdb) and cdb[p:p + 4] == b"PK\x01\x02" and len(names) < 5000:
            nl, xl, cl = u16le(cdb, p + 28), u16le(cdb, p + 30), u16le(cdb, p + 32)
            names.append(cdb[p + 46:p + 46 + nl].decode("utf-8", "replace"))
            p += 46 + nl + xl + cl
        mt = b""
        if names and names[0] == "mimetype":
            lh = r.read(off, 30)
            mt = r.read(off + 30 + u16le(lh, 26) + u16le(lh, 28), 64)
        ext = zip_kind(names, mt)
        f = self.found(off, end - off, ext=ext, meta={"entradas": len(names)}, confidence=0.97)
        f.category = "document" if ext not in ("zip", "jar", "apk", "ipa", "kmz") else "archive"
        return f


# ---------------------------------------------------------------- OLE compound file

FREESECT, ENDOFCHAIN = 0xFFFFFFFF, 0xFFFFFFFE


class OLEHandler(Handler):
    key = "ole"
    name = "Office 97-2003 / MSG / MSI"
    category = "document"
    ext = "doc"
    signatures = ((0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),)

    def probe(self, r, off):
        h = r.read(off, 512)
        if len(h) < 512 or u16le(h, 28) != 0xFFFE:
            return None
        shift = u16le(h, 30)
        if shift not in (9, 12):
            return None
        ss = 1 << shift
        nfat, dir_start, difat_start, ndifat = u32le(h, 44), u32le(h, 48), u32le(h, 68), u32le(h, 72)
        if nfat > 1 << 20:
            return None
        fat_secs = [u32le(h, 76 + 4 * i) for i in range(109)]
        d = difat_start
        for _ in range(min(ndifat, 1 << 16)):
            if d >= ENDOFCHAIN - 4:
                break
            blk = r.read(off + ss + d * ss, ss)
            fat_secs += [u32le(blk, 4 * i) for i in range(ss // 4 - 1)]
            d = u32le(blk, ss - 4)
        fat_secs = [s for s in fat_secs if s < ENDOFCHAIN - 4][:nfat]
        fat = []
        maxsec = -1
        for fs in fat_secs:
            blk = r.read(off + ss + fs * ss, ss)
            if len(blk) < ss:
                return None
            vals = struct.unpack("<%dI" % (ss // 4), blk)
            base = len(fat)
            fat.extend(vals)
            for j in range(len(vals) - 1, -1, -1):
                if vals[j] != FREESECT:
                    maxsec = max(maxsec, base + j)
                    break
        if maxsec < 0:
            return None
        end = ss + (maxsec + 1) * ss
        ext = self._kind(r, off, ss, dir_start, fat)
        return self.found(off, end, ext=ext, confidence=0.95)

    def _kind(self, r, off, ss, sec, fat):
        names = set()
        for _ in range(64):
            if sec >= len(fat) or sec >= ENDOFCHAIN - 4:
                break
            blk = r.read(off + ss + sec * ss, ss)
            for i in range(0, len(blk), 128):
                nl = u16le(blk, i + 64)
                if 2 <= nl <= 64:
                    names.add(blk[i:i + nl - 2].decode("utf-16-le", "replace"))
            sec = fat[sec]
        if "WordDocument" in names:
            return "doc"
        if "Workbook" in names or "Book" in names:
            return "xls"
        if "PowerPoint Document" in names:
            return "ppt"
        if any(n.startswith("__substg1.0_") for n in names):
            return "msg"
        if "VisioDocument" in names:
            return "vsd"
        if "Contents" in names and "Quill" in names:
            return "pub"
        if any(n and ord(n[0]) >= 0x3800 for n in names):
            return "msi"
        return "ole"


# ---------------------------------------------------------------- SQLite


class SQLiteHandler(Handler):
    key = "sqlite"
    name = "SQLite"
    category = "document"
    ext = "sqlite"
    signatures = ((0, b"SQLite format 3\x00"),)

    def probe(self, r, off):
        h = r.read(off, 100)
        if len(h) < 100:
            return None
        ps = struct.unpack_from(">H", h, 16)[0]
        ps = 65536 if ps == 1 else ps
        if ps < 512 or ps & (ps - 1):
            return None
        pages = u32be(h, 28)
        if not pages or u32be(h, 92) != u32be(h, 24):
            return None
        return self.found(off, ps * pages, confidence=0.95)


# ---------------------------------------------------------------- RTF


class RTFHandler(Handler):
    key = "rtf"
    name = "RTF"
    category = "document"
    ext = "rtf"
    signatures = ((0, b"{\\rtf1"),)

    def probe(self, r, off):
        depth, pos = 0, off
        while pos - off < 512 << 20:
            buf = r.read(pos, 1 << 20)
            if not buf:
                break
            i = 0
            n = len(buf)
            while i < n:
                c = buf[i]
                if c == 0x5C:
                    i += 2
                    continue
                if c == 0x7B:
                    depth += 1
                elif c == 0x7D:
                    depth -= 1
                    if depth == 0:
                        return self.found(off, pos + i + 1 - off, confidence=0.9)
                elif c == 0:
                    return None
                i += 1
            pos += n
        return None


# ---------------------------------------------------------------- RAR 4 / 5


def _rvint(b, p):
    v = s = 0
    while p < len(b):
        c = b[p]
        v |= (c & 0x7F) << s
        p += 1
        if not c & 0x80:
            return v, p
        s += 7
        if s > 63:
            break
    return None, p


class RARHandler(Handler):
    key = "rar"
    name = "RAR"
    category = "archive"
    ext = "rar"
    signatures = ((0, b"Rar!\x1a\x07"),)

    def probe(self, r, off):
        h = r.read(off, 8)
        if h[6:7] == b"\x00":
            return self._v4(r, off + 7, off)
        if h[6:8] == b"\x01\x00":
            return self._v5(r, off + 8, off)
        return None

    def _v4(self, r, pos, off):
        while pos < r.size:
            h = r.read(pos, 11)
            if len(h) < 7:
                break
            typ, flags, size = h[2], u16le(h, 3), u16le(h, 5)
            if not 0x72 <= typ <= 0x7B or size < 7:
                return None
            add = u32le(h, 7) if flags & 0x8000 and len(h) >= 11 else 0
            pos += size + add
            if typ == 0x7B:
                return self.found(off, pos - off)
        return self.found(off, min(pos, r.size) - off, complete=False, confidence=0.5, note="sin bloque final")

    def _v5(self, r, pos, off):
        while pos < r.size:
            h = r.read(pos, 64)
            hsize, p = _rvint(h, 4)  # after CRC32
            if hsize is None or hsize == 0 or hsize > 2 << 20:
                return None
            hdr_start = p
            typ, p = _rvint(h, p)
            flags, p = _rvint(h, p)
            if typ is None or flags is None:
                return None
            data = 0
            if flags & 1:
                _, p = _rvint(h, p)
            if flags & 2:
                data, p = _rvint(h, p)
            pos += hdr_start + hsize + (data or 0)
            if typ == 5:
                return self.found(off, pos - off)
            if typ not in (1, 2, 3, 4):
                return None
        return self.found(off, min(pos, r.size) - off, complete=False, confidence=0.5, note="sin bloque final")


# ---------------------------------------------------------------- 7z


class SevenZHandler(Handler):
    key = "7z"
    name = "7-Zip"
    category = "archive"
    ext = "7z"
    signatures = ((0, b"7z\xbc\xaf\x27\x1c"),)

    def probe(self, r, off):
        h = r.read(off, 32)
        if len(h) < 32 or zlib.crc32(h[12:32]) != u32le(h, 8):
            return None
        nxt_off, nxt_size = struct.unpack_from("<QQ", h, 12)
        return self.found(off, 32 + nxt_off + nxt_size, confidence=0.99)


# ---------------------------------------------------------------- streams with no length: decompress to find the end


class _Stream(Handler):
    category = "archive"
    max_size = 4 << 30

    def _decomp(self):
        raise NotImplementedError

    def probe(self, r, off):
        d = self._decomp()
        pos = off
        try:
            while pos - off < self.max_size:
                buf = r.read(pos, 1 << 16)  # small chunks bound the decompressed output per step
                if not buf:
                    break
                d.decompress(buf)
                if d.eof:
                    return self.found(off, pos + len(buf) - len(d.unused_data) - off, confidence=0.98)
                pos += len(buf)
        except (zlib.error, OSError, EOFError, lzma.LZMAError, ValueError):
            return None
        return None


class GZHandler(_Stream):
    key = "gz"
    name = "gzip"
    ext = "gz"
    signatures = ((0, b"\x1f\x8b\x08"),)

    def _decomp(self):
        return zlib.decompressobj(31)

    def probe(self, r, off):
        f = super().probe(r, off)
        if f:
            h = r.read(off, 512)
            if h[3] & 8:  # FNAME
                name = h[10:].split(b"\0")[0].decode("latin-1", "replace")
                f.name = name + ".gz" if name else ""
                if name.endswith(".tar"):
                    f.ext = "tar.gz"
        return f


class BZ2Handler(_Stream):
    key = "bz2"
    name = "bzip2"
    ext = "bz2"
    signatures = ((0, b"BZh"),)

    def _decomp(self):
        return bz2.BZ2Decompressor()

    def probe(self, r, off):
        h = r.read(off, 10)
        if not (h[3:4].isdigit() and h[4:10] == b"1AY&SY"):
            return None
        return super().probe(r, off)


class XZHandler(_Stream):
    key = "xz"
    name = "xz"
    ext = "xz"
    signatures = ((0, b"\xfd7zXZ\x00"),)

    def _decomp(self):
        return lzma.LZMADecompressor(lzma.FORMAT_XZ)


# ---------------------------------------------------------------- tar, ISO 9660


class TARHandler(Handler):
    key = "tar"
    name = "TAR"
    category = "archive"
    ext = "tar"
    signatures = ((257, b"ustar"),)

    def probe(self, r, off):
        pos = off
        n = 0
        while True:
            h = r.read(pos, 512)
            if len(h) < 512:
                break
            if h == b"\0" * 512:
                pos += 1024
                # tar writers pad the archive to a 10240-byte record with zeros
                rec = -(-(pos - off) // 10240) * 10240 + off
                if r.read(pos, rec - pos) == b"\0" * (rec - pos):
                    pos = rec
                break
            if h[257:262] != b"ustar":
                if n:
                    break
                return None
            try:
                size = int(h[124:136].strip(b"\0 ") or b"0", 8)
            except ValueError:
                return None
            pos += 512 + -(-size // 512) * 512
            n += 1
        return self.found(off, min(pos, r.size) - off, meta={"entradas": n}, confidence=0.9) if n else None


class ISOHandler(Handler):
    key = "iso"
    name = "Imagen ISO 9660"
    category = "archive"
    ext = "iso"
    signatures = ((0x8001, b"CD001\x01"),)

    def probe(self, r, off):
        pvd = r.read(off + 0x8000, 2048)
        if len(pvd) < 2048 or pvd[0] != 1:
            return None
        blocks, bs = u32le(pvd, 80), u16le(pvd, 128)
        if bs not in (512, 1024, 2048) or not blocks:
            return None
        label = pvd[40:72].decode("latin-1").strip()
        return self.found(off, blocks * bs, meta={"etiqueta": label}, confidence=0.95)


HANDLERS = [PDFHandler(), ZIPHandler(), OLEHandler(), SQLiteHandler(), RTFHandler(), RARHandler(),
            SevenZHandler(), GZHandler(), BZ2Handler(), XZHandler(), TARHandler(), ISOHandler()]
