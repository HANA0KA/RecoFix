"""Audio streams: MP3, AAC (ADTS), FLAC, Ogg (Vorbis/Opus/Theora/FLAC)."""
from .base import Handler, u16be, u32be

MP3_BR = {
    # key: (version bits, layer bits); layer bits 1 = Layer III, 2 = Layer II, 3 = Layer I
    (3, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (3, 3): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
}
MP3_BR_V2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
MP3_BR_V2L1 = [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256]
MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def mpa_frame(h):
    """Frame length of an MPEG audio header, or 0."""
    if len(h) < 4 or h[0] != 0xFF or h[1] & 0xE0 != 0xE0:
        return 0
    ver = (h[1] >> 3) & 3
    layer = (h[1] >> 1) & 3
    bri = h[2] >> 4
    sri = (h[2] >> 2) & 3
    pad = (h[2] >> 1) & 1
    if ver == 1 or layer == 0 or bri in (0, 15) or sri == 3:
        return 0
    if ver == 3:
        br = MP3_BR[(3, layer)][bri]
    else:
        br = (MP3_BR_V2L1 if layer == 3 else MP3_BR_V2)[bri]
    sr = MP3_SR[ver][sri]
    if layer == 3:
        return (12 * br * 1000 // sr + pad) * 4
    if layer == 1 and ver != 3:  # Layer III, MPEG-2/2.5
        return 72 * br * 1000 // sr + pad
    return 144 * br * 1000 // sr + pad


class MP3Handler(Handler):
    key = "mp3"
    name = "MP3 / MPEG audio"
    category = "audio"
    ext = "mp3"
    signatures = ((0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xfa"), (0, b"\xff\xf3"), (0, b"\xff\xf2"), (0, b"\xff\xe3"))
    max_size = 2 << 30

    def probe(self, r, off):
        pos = off
        h = r.read(off, 10)
        tag = h[:3] == b"ID3"
        if tag:
            if h[3] > 4 or any(b & 0x80 for b in h[6:10]):
                return None
            size = (h[6] << 21) | (h[7] << 14) | (h[8] << 7) | h[9]
            pos += 10 + size + (10 if h[5] & 0x10 else 0)
            # some encoders pad after the tag
            pad = r.read(pos, 4096)
            k = pad.find(b"\xff")
            if k < 0:
                return None
            pos += k
        frames = 0
        base, buf = pos, b""
        while pos < r.size and pos - off < self.max_size:
            if pos + 4 > base + len(buf):
                base, buf = pos, r.read(pos, 1 << 20)
            fl = mpa_frame(buf[pos - base:pos - base + 4])
            if not fl:
                break
            pos += fl
            frames += 1
        if frames < (2 if tag else 8):
            return None
        if r.read(pos, 3) == b"TAG":
            pos += 128
        if r.read(pos, 8) == b"APETAGEX":
            pos += 32 + int.from_bytes(r.read(pos + 12, 4), "little")
        return self.found(off, pos - off, meta={"frames": frames}, confidence=0.9 if tag else 0.75)


class ADTSHandler(Handler):
    key = "aac"
    name = "AAC (ADTS)"
    category = "audio"
    ext = "aac"
    signatures = ((0, b"\xff\xf1"), (0, b"\xff\xf9"))

    def probe(self, r, off):
        pos = off
        frames = 0
        base, buf = pos, b""
        while pos < r.size:
            if pos + 7 > base + len(buf):
                base, buf = pos, r.read(pos, 1 << 20)
            h = buf[pos - base:pos - base + 7]
            if len(h) < 7 or h[0] != 0xFF or h[1] & 0xF6 != 0xF0 or (h[2] >> 2) & 0xF > 12:
                break
            fl = ((h[3] & 3) << 11) | (h[4] << 3) | (h[5] >> 5)
            if fl < 7:
                break
            pos += fl
            frames += 1
        if frames < 16:
            return None
        return self.found(off, pos - off, meta={"frames": frames}, confidence=0.8)


def _crc8(data):
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def flac_header_ok(b):
    """Validate a FLAC frame header (sync + CRC-8)."""
    if len(b) < 6 or b[0] != 0xFF or b[1] & 0xFE != 0xF8:
        return False
    bs, sr = b[2] >> 4, b[2] & 0xF
    if bs == 0 or sr == 0xF or (b[3] >> 4) > 10 or b[3] & 1:
        return False
    n = 1
    c = b[4]
    while n < 7 and c & (0x80 >> (n - 1)) and n < 8:
        n += 1
    n = 1 if not c & 0x80 else n - 1
    p = 4 + n
    p += {6: 1, 7: 2}.get(bs, 0)
    p += {12: 1, 13: 2, 14: 2}.get(sr, 0)
    if p >= len(b):
        return False
    return _crc8(b[:p]) == b[p]


class FLACHandler(Handler):
    key = "flac"
    name = "FLAC"
    category = "audio"
    ext = "flac"
    signatures = ((0, b"fLaC"),)

    def probe(self, r, off):
        pos = off + 4
        maxframe = 0
        while True:
            h = r.read(pos, 4)
            if len(h) < 4:
                return None
            last, typ, ln = h[0] & 0x80, h[0] & 0x7F, int.from_bytes(h[1:4], "big")
            if typ == 0:
                si = r.read(pos + 4, 34)
                maxframe = int.from_bytes(si[7:10], "big")
            elif typ > 6 and typ != 127:
                return None
            pos += 4 + ln
            if last:
                break
        gap = max(maxframe, 1 << 16) * 2
        last_sync = pos
        scan = pos
        while scan < r.size:
            buf = r.read(scan, 1 << 20)
            if not buf:
                break
            i = 0
            found_any = False
            while True:
                i = buf.find(b"\xff", i)
                if i < 0 or i + 16 > len(buf):
                    break
                if buf[i + 1] & 0xFE == 0xF8 and flac_header_ok(buf[i:i + 16]):
                    last_sync = scan + i
                    found_any = True
                i += 1
            if scan + len(buf) - last_sync > gap:
                break
            scan += max(1, len(buf) - 16)
            if not found_any and scan - last_sync > gap:
                break
        end = self._frame_end(r, last_sync, maxframe or (1 << 16))
        return self.found(off, end - off, confidence=0.85)

    def _frame_end(self, r, start, maxframe):
        """End of the last frame: the position where its CRC-16 matches."""
        data = r.read(start, maxframe + 2)
        crc = 0
        for i, b in enumerate(data[:-2]):
            crc ^= b << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
            if i >= 6 and u16be(data, i + 1) == crc:
                return start + i + 3
        return start + len(data)


class OggHandler(Handler):
    key = "ogg"
    name = "Ogg (Vorbis/Opus/Theora)"
    category = "audio"
    ext = "ogg"
    signatures = ((0, b"OggS\x00\x02"),)

    def probe(self, r, off):
        first = r.read(off, 27 + 255 + 16)
        if len(first) < 28:
            return None
        nseg = first[26]
        pkt = first[27 + nseg:27 + nseg + 8]
        ext, cat = "ogg", "audio"
        if pkt.startswith(b"OpusHead"):
            ext = "opus"
        elif pkt.startswith(b"\x80theora"):
            ext, cat = "ogv", "video"
        elif pkt.startswith(b"\x7fFLAC"):
            ext = "oga"
        elif pkt.startswith(b"Speex"):
            ext = "spx"
        pos = off
        pages = 0
        while True:
            h = r.read(pos, 27)
            if len(h) < 27 or h[:4] != b"OggS" or h[4] != 0:
                break
            segs = r.read(pos + 27, h[26])
            size = 27 + h[26] + sum(segs)
            pages += 1
            pos += size
            if h[5] & 4 and r.read(pos, 4) != b"OggS":
                break
        if pages < 2:
            return None
        f = self.found(off, pos - off, ext=ext, meta={"paginas": pages}, confidence=0.9)
        f.category = cat
        return f


HANDLERS = [MP3Handler(), ADTSHandler(), FLACHandler(), OggHandler()]
