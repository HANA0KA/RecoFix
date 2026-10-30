"""Executables, fonts, MIDI/DSF/CAF audio, DPX film frames, XML/HTML text, and a generic signature table.

Generic entries have no structural length: the carver ends them at the next
detected file start or at ``max_size`` and marks them as estimated.
"""
import struct

from .base import Handler, u16be, u16le, u32be, u32le


class PEHandler(Handler):
    key = "exe"
    name = "Windows EXE/DLL"
    category = "other"
    ext = "exe"
    signatures = ((0, b"MZ"),)

    def probe(self, r, off):
        h = r.read(off, 64)
        if len(h) < 64:
            return None
        pe = u32le(h, 60)
        if not 64 <= pe < 4096:
            return None
        p = r.read(off + pe, 24)
        if p[:4] != b"PE\0\0":
            return None
        nsec, opt = u16le(p, 6), u16le(p, 20)
        chars = u16le(p, 22)
        secs = r.read(off + pe + 24 + opt, 40 * nsec)
        end = pe + 24 + opt + 40 * nsec
        for i in range(nsec):
            raw_size, raw_ptr = u32le(secs, 40 * i + 16), u32le(secs, 40 * i + 20)
            end = max(end, raw_ptr + raw_size)
        # Authenticode signature lives past the last section (data directory 4)
        o = r.read(off + pe + 24, opt)
        magic = u16le(o, 0) if o else 0
        dd = 96 if magic == 0x10B else 112 if magic == 0x20B else None
        if dd and len(o) >= dd + 40:
            so, ss = u32le(o, dd + 32), u32le(o, dd + 36)
            if so and ss:
                end = max(end, so + ss)
        return self.found(off, end, ext="dll" if chars & 0x2000 else "exe", confidence=0.85)


class MachOHandler(Handler):
    key = "macho"
    name = "Mach-O"
    category = "other"
    ext = "macho"
    signatures = ((0, b"\xcf\xfa\xed\xfe"), (0, b"\xca\xfe\xba\xbe"))

    def probe(self, r, off):
        h = r.read(off, 32)
        if h[:4] == b"\xca\xfe\xba\xbe":
            n = u32be(h, 4)
            if not 1 <= n <= 8:
                return None  # Java class files share this magic
            arch = r.read(off + 8, 20 * n)
            end = max(u32be(arch, 20 * i + 8) + u32be(arch, 20 * i + 12) for i in range(n))
            return self.found(off, end, ext="macho", confidence=0.8)
        ncmds, sz = u32le(h, 16), u32le(h, 20)
        if not 0 < ncmds < 512 or sz > 1 << 20:
            return None
        cmds = r.read(off + 32, sz)
        p, end = 0, 32 + sz
        for _ in range(ncmds):
            if p + 8 > len(cmds):
                break
            cmd, csz = u32le(cmds, p), u32le(cmds, p + 4)
            if cmd == 0x19 and p + 56 <= len(cmds):  # LC_SEGMENT_64
                fo, fs = struct.unpack_from("<QQ", cmds, p + 40)
                end = max(end, fo + fs)
            elif cmd == 0x1D:  # LC_CODE_SIGNATURE
                end = max(end, u32le(cmds, p + 8) + u32le(cmds, p + 12))
            if csz < 8:
                break
            p += csz
        return self.found(off, end, confidence=0.8)


class ELFHandler(Handler):
    key = "elf"
    name = "ELF"
    category = "other"
    ext = "elf"
    signatures = ((0, b"\x7fELF"),)

    def probe(self, r, off):
        h = r.read(off, 64)
        if len(h) < 52 or h[4] not in (1, 2) or h[5] not in (1, 2):
            return None
        le = h[5] == 1
        if h[4] == 2:
            shoff = struct.unpack_from("<Q" if le else ">Q", h, 40)[0]
            shent, shnum = struct.unpack_from("<HH" if le else ">HH", h, 58)
        else:
            shoff = struct.unpack_from("<I" if le else ">I", h, 32)[0]
            shent, shnum = struct.unpack_from("<HH" if le else ">HH", h, 46)
        if not shoff or shnum > 4096:
            return None
        return self.found(off, shoff + shent * shnum, confidence=0.75)


class SfntHandler(Handler):
    key = "ttf"
    name = "Fuentes TTF/OTF"
    category = "other"
    ext = "ttf"
    signatures = ((0, b"\x00\x01\x00\x00"), (0, b"OTTO"), (0, b"true"))

    def probe(self, r, off):
        h = r.read(off, 12)
        n = u16be(h, 4)
        if not 4 <= n <= 64:
            return None
        tbl = r.read(off + 12, 16 * n)
        end = 12 + 16 * n
        for i in range(n):
            tag = tbl[16 * i:16 * i + 4]
            if not all(32 <= c < 127 for c in tag):
                return None
            end = max(end, u32be(tbl, 16 * i + 8) + u32be(tbl, 16 * i + 12))
        if end > 64 << 20:
            return None
        return self.found(off, -(-end // 4) * 4, ext="otf" if h[:4] == b"OTTO" else "ttf", confidence=0.8)


class WOFFHandler(Handler):
    key = "woff"
    name = "WOFF"
    category = "other"
    ext = "woff"
    signatures = ((0, b"wOFF"), (0, b"wOF2"))

    def probe(self, r, off):
        h = r.read(off, 12)
        ln = u32be(h, 8)
        if not 44 <= ln < 64 << 20:
            return None
        return self.found(off, ln, ext="woff2" if h[3:4] == b"2" else "woff", confidence=0.9)


class MIDIHandler(Handler):
    key = "mid"
    name = "MIDI"
    category = "audio"
    ext = "mid"
    signatures = ((0, b"MThd\x00\x00\x00\x06"),)

    def probe(self, r, off):
        ntr = u16be(r.read(off + 10, 2))
        pos = off + 14
        for _ in range(ntr):
            h = r.read(pos, 8)
            if h[:4] != b"MTrk":
                return None
            pos += 8 + u32be(h, 4)
        return self.found(off, pos - off, confidence=0.95)


class DSFHandler(Handler):
    key = "dsf"
    name = "DSD (DSF)"
    category = "audio"
    ext = "dsf"
    signatures = ((0, b"DSD \x1c\x00\x00\x00\x00\x00\x00\x00"),)

    def probe(self, r, off):
        return self.found(off, struct.unpack("<Q", r.read(off + 12, 8))[0], confidence=0.95)


class CAFHandler(Handler):
    key = "caf"
    name = "Core Audio (CAF)"
    category = "audio"
    ext = "caf"
    signatures = ((0, b"caff\x00\x01"),)

    def probe(self, r, off):
        pos = off + 8
        while pos < r.size:
            h = r.read(pos, 12)
            if len(h) < 12 or not h[:4].isalpha():
                break
            size = struct.unpack(">q", h[4:12])[0]
            if size == -1:  # data chunk running to end of file
                return self.found(off, r.size - off, complete=False, confidence=0.5, note="chunk de datos sin tamaño")
            pos += 12 + size
        return self.found(off, pos - off, confidence=0.9)


class DPXHandler(Handler):
    key = "dpx"
    name = "DPX (escaneo de cine)"
    category = "photo"
    ext = "dpx"
    signatures = ((0, b"SDPX"), (0, b"XPDS"))

    def probe(self, r, off):
        h = r.read(off, 24)
        size = u32be(h, 16) if h[:4] == b"SDPX" else u32le(h, 16)
        if not 768 < size < 1 << 31:
            return None
        return self.found(off, size, confidence=0.95)


class TextHandler(Handler):
    """XML/HTML sidecars (camera XML, MHL, FCPXML, SVG...). Ends at the first NUL or binary byte."""
    key = "xml"
    name = "XML / HTML / MHL"
    category = "document"
    ext = "xml"
    signatures = ((0, b"<?xml"), (0, b"<!DOCTYPE html"), (0, b"<!doctype html"), (0, b"<html"))
    max_size = 64 << 20

    def probe(self, r, off):
        pos = off
        while pos - off < self.max_size:
            buf = r.read(pos, 1 << 16)
            if not buf:
                break
            bad = next((i for i, c in enumerate(buf) if c == 0 or (c < 9) or (13 < c < 32 and c != 27)), None)
            if bad is not None:
                pos += bad
                break
            pos += len(buf)
        head = r.read(off, 4096)
        low = head.lower()
        ext = "xml"
        if b"<hashlist" in low:
            ext = "mhl"
        elif b"<svg" in low:
            ext = "svg"
        elif b"<fcpxml" in low:
            ext = "fcpxml"
        elif b"<html" in low or b"<!doctype html" in low:
            ext = "html"
        elif b"x:xmpmeta" in low:
            ext = "xmp"
        text = r.read(off, pos - off)
        # trim to the last closing angle bracket so slack bytes are not included
        k = text.rfind(b">")
        if k < 16:
            return None
        return self.found(off, k + 1 + (1 if text[k + 1:k + 2] == b"\n" else 0), ext=ext, confidence=0.7)


GENERIC = [
    # (ext, category, offset, magic, max_size)
    ("exr", "photo", 0, b"\x76\x2f\x31\x01", 512 << 20),
    ("ari", "photo", 0, b"ARRI\x12\x34\x56\x78", 64 << 20),
    ("ape", "audio", 0, b"MAC ", 2 << 30),
    ("wv", "audio", 0, b"wvpk", 2 << 30),
    ("amr", "audio", 0, b"#!AMR\n", 64 << 20),
    ("djvu", "document", 0, b"AT&TFORM", 256 << 20),
    ("indd", "document", 0, b"\x06\x06\xed\xf5\xd8\x1d\x46\xe5\xbd\x31\xef\xe7\xfe\x74\xb7\x1d", 2 << 30),
    ("eps", "document", 0, b"%!PS-Adobe-", 512 << 20),
    ("swf", "video", 0, b"FWS", 256 << 20),
    ("flv", "video", 0, b"FLV\x01", 8 << 30),
    ("rm", "video", 0, b".RMF\x00\x00\x00", 4 << 30),
    ("asf", "video", 0, b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c", 8 << 30),
    ("class", "other", 0, b"\xca\xfe\xba\xbe\x00\x00", 16 << 20),
    ("dmg", "archive", 0, b"koly", 0),
    ("kdbx", "document", 0, b"\x03\xd9\xa2\x9a\x67\xfb\x4b\xb5", 256 << 20),
    ("sketch", "document", 0, b"SQLite format 3\x00", 0),
]


class GenericHandler(Handler):
    def __init__(self, ext, category, offset, magic, max_size):
        self.key = "gen_" + ext
        self.name = ext.upper()
        self.category = category
        self.ext = ext
        self.signatures = ((offset, magic),)
        self.max_size = max_size

    def probe(self, r, off):
        if self.ext == "asf":
            # ASF header object carries the total file size in the File Properties object
            h = r.read(off, 4096)
            i = h.find(b"\xa1\xdc\xab\x8c\x47\xa9\xcf\x11\x8e\xe4\x00\xc0\x0c\x20\x53\x65")
            if i > 0:
                size = struct.unpack_from("<Q", h, i + 40)[0]
                if 0 < size < self.max_size:
                    return self.found(off, size, ext="wmv" if b"\xc0\xef\x19\xbc" in h else "wma", confidence=0.9)
        if self.ext == "swf":
            h = r.read(off, 8)
            size = u32le(h, 4)
            if not 1 <= h[3] <= 50 or not 21 < size < self.max_size:
                return None
            return self.found(off, size, confidence=0.7)
        if self.ext == "flv":
            h = r.read(off, 9)
            if h[4] & ~5 or u32be(h, 5) != 9:
                return None
        return self.found(off, 0, complete=False, confidence=0.4,
                          note="tamaño estimado (formato sin longitud estructural)", meta={"estimado": True})


HANDLERS = [PEHandler(), MachOHandler(), ELFHandler(), SfntHandler(), WOFFHandler(), MIDIHandler(),
            DSFHandler(), CAFHandler(), DPXHandler(), TextHandler()] + \
    [GenericHandler(*g) for g in GENERIC if g[4]]
