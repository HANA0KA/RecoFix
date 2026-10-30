"""ISO BMFF / QuickTime toolkit for repair and reassembly.

Reads a healthy *sample* clip's ``moov`` (codec configuration, timescale,
frame duration, audio layout), validates candidate samples inside a damaged
``mdat`` and writes a fresh ``moov``. This is the core of Recoverit's
"Advanced Repair" (sample video) and of its fragment reassembly
(``mp4::FragmentSample`` / ``MdatStreamAnalyzer`` in libscanopreator).
"""
import struct
from dataclasses import dataclass, field

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta"}
PCM = {b"lpcm": None, b"sowt": "s16le", b"twos": "s16be", b"in24": "s24be", b"in32": "s32be",
       b"fl32": "f32be", b"fl64": "f64be", b"raw ": "u8"}
PRORES = {b"apch", b"apcn", b"apcs", b"apco", b"ap4h", b"ap4x"}
AVC = {b"avc1", b"avc3"}
HEVC = {b"hvc1", b"hev1"}


def boxes(buf, start=0, end=None):
    end = len(buf) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos, pos + hdr, pos + size
        pos += size


def find(buf, path, start=0, end=None):
    """First box at ``path`` (list of types) -> (header start, payload start, end) or None."""
    for typ, s, b, e in boxes(buf, start, end):
        if typ == path[0]:
            return (s, b, e) if len(path) == 1 else find(buf, path[1:], b, e)
    return None


def box(typ, payload):
    n = 8 + len(payload)
    if n > 0xFFFFFFFF:
        return struct.pack(">I4sQ", 1, typ, n + 8) + payload
    return struct.pack(">I4s", n, typ) + payload


def full(typ, version, flags, payload):
    return box(typ, struct.pack(">I", (version << 24) | flags) + payload)


def top_level(f, size):
    """[(type, offset, header_len, size)] of a file's top-level boxes (tolerates a truncated last box)."""
    out, pos = [], 0
    while pos + 8 <= size:
        f.seek(pos)
        h = f.read(16)
        n, typ = struct.unpack_from(">I4s", h, 0)
        hdr = 8
        if n == 1 and len(h) >= 16:
            n, hdr = struct.unpack_from(">Q", h, 8)[0], 16
        elif n == 0:
            n = size - pos
        if n < hdr or not typ.isalnum() and typ not in (b"free", b"skip", b"wide", b"mdat", b"moov", b"ftyp"):
            break
        out.append((typ, pos, hdr, n))
        pos += n
    return out


@dataclass
class Track:
    handler: bytes
    codec: bytes
    stsd: bytes  # whole stsd box
    timescale: int
    delta: int = 0  # most common sample duration
    sample_size: int = 0  # constant sample size (PCM) or 0
    nal_len: int = 4
    params: list = field(default_factory=list)  # SPS/PPS/VPS NAL units (Annex B prefix not included)
    width: int = 0
    height: int = 0
    channels: int = 0
    rate: int = 0
    bits: int = 0
    pcm: str = ""  # ffmpeg raw format for PCM audio
    chunk_bytes: list = field(default_factory=list)  # chunk sizes in the sample (audio interleave)
    tkhd: bytes = b""
    hdlr: bytes = b""
    minf_header: bytes = b""  # vmhd/smhd box
    trak: tuple = None


def _stbl_tables(buf, stbl):
    s, b, e = stbl
    t = {}
    for typ, hs, pb, pe in boxes(buf, b, e):
        t[typ] = buf[pb:pe]
    return t


def _sample_sizes(t):
    stsz = t.get(b"stsz")
    if not stsz:
        return 0, []
    const, count = struct.unpack_from(">II", stsz, 4)
    if const:
        return const, []
    return 0, list(struct.unpack_from(">%dI" % count, stsz, 12)) if len(stsz) >= 12 + 4 * count else []


def _chunk_bytes(t):
    """Byte size of each chunk (needed to skip interleaved audio between video samples)."""
    const, sizes = _sample_sizes(t)
    stsc = t.get(b"stsc")
    off = t.get(b"stco") or t.get(b"co64")
    if not stsc or not off:
        return []
    n_chunks = struct.unpack_from(">I", off, 4)[0]
    runs = [struct.unpack_from(">III", stsc, 8 + 12 * i) for i in range(struct.unpack_from(">I", stsc, 4)[0])]
    out, k = [], 0
    for ci in range(1, min(n_chunks, 4000) + 1):
        spc = next((r[1] for r in reversed(runs) if r[0] <= ci), 1)
        if const:
            out.append(const * spc)
        else:
            out.append(sum(sizes[k:k + spc]))
        k += spc
    return out


def _params_avc(cfg):
    nal_len = (cfg[4] & 3) + 1
    out, p = [], 6
    n = cfg[5] & 31
    for _ in range(n):
        ln = struct.unpack_from(">H", cfg, p)[0]
        out.append(cfg[p + 2:p + 2 + ln])
        p += 2 + ln
    n = cfg[p]
    p += 1
    for _ in range(n):
        ln = struct.unpack_from(">H", cfg, p)[0]
        out.append(cfg[p + 2:p + 2 + ln])
        p += 2 + ln
    return nal_len, out


def _params_hevc(cfg):
    nal_len = (cfg[21] & 3) + 1
    out, p = [], 23
    for _ in range(cfg[22]):
        p += 1
        cnt = struct.unpack_from(">H", cfg, p)[0]
        p += 2
        for _ in range(cnt):
            ln = struct.unpack_from(">H", cfg, p)[0]
            out.append(cfg[p + 2:p + 2 + ln])
            p += 2 + ln
    return nal_len, out


def read_tracks(path):
    """Tracks of a healthy sample clip."""
    with open(path, "rb") as f:
        import os
        size = os.fstat(f.fileno()).st_size
        tops = top_level(f, size)
        moov = next((t for t in tops if t[0] == b"moov"), None)
        if not moov:
            raise ValueError("el vídeo de muestra no tiene índice (moov): usa un clip que se reproduzca")
        f.seek(moov[1])
        buf = f.read(moov[3])
    tracks = []
    for typ, s, b, e in boxes(buf, 8 if struct.unpack_from(">I", buf, 0)[0] != 1 else 16):
        if typ != b"trak":
            continue
        hd = find(buf, [b"mdia", b"hdlr"], b, e)
        md = find(buf, [b"mdia", b"mdhd"], b, e)
        stbl = find(buf, [b"mdia", b"minf", b"stbl"], b, e)
        tk = find(buf, [b"tkhd"], b, e)
        if not (hd and md and stbl):
            continue
        handler = buf[hd[1] + 8:hd[1] + 12]
        v = buf[md[1]]
        timescale = struct.unpack_from(">I", buf, md[1] + (20 if v == 1 else 12))[0]
        t = _stbl_tables(buf, stbl)
        stsd_box = find(buf, [b"stsd"], stbl[1], stbl[2])
        stsd = buf[stsd_box[0]:stsd_box[2]]
        entry = stsd[16:]
        codec = entry[4:8]
        tr = Track(handler, codec, stsd, timescale, tkhd=buf[tk[0]:tk[2]] if tk else b"",
                   hdlr=buf[hd[0]:hd[2]], trak=(s, e))
        minf = find(buf, [b"mdia", b"minf"], b, e)
        for mt, ms, mb, me in boxes(buf, minf[1], minf[2]):
            if mt in (b"vmhd", b"smhd", b"gmhd", b"nmhd"):
                tr.minf_header = buf[ms:me]
        stts = t.get(b"stts")
        if stts and struct.unpack_from(">I", stts, 4)[0]:
            runs = [struct.unpack_from(">II", stts, 8 + 8 * i) for i in range(struct.unpack_from(">I", stts, 4)[0])]
            tr.delta = max(runs, key=lambda r: r[0])[1]
        tr.sample_size, _ = _sample_sizes(t)
        tr.chunk_bytes = _chunk_bytes(t)
        if handler == b"vide":
            tr.width, tr.height = struct.unpack_from(">HH", entry, 32)
            for ct, cs, cb, ce in boxes(entry, 86):
                if ct == b"avcC":
                    tr.nal_len, tr.params = _params_avc(entry[cb:ce])
                elif ct == b"hvcC":
                    tr.nal_len, tr.params = _params_hevc(entry[cb:ce])
        elif handler == b"soun":
            ver = struct.unpack_from(">H", entry, 16)[0]
            tr.channels, tr.bits = struct.unpack_from(">HH", entry, 24)
            tr.rate = struct.unpack_from(">I", entry, 32)[0] >> 16
            if ver == 2 and len(entry) >= 72:
                tr.rate = int(struct.unpack_from(">d", entry, 40)[0])
                tr.channels = struct.unpack_from(">I", entry, 48)[0]
                tr.bits = struct.unpack_from(">I", entry, 56)[0]
                flags = struct.unpack_from(">I", entry, 60)[0]
                tr.pcm = "%s%d%s" % ("f" if flags & 1 else "s", tr.bits, "be" if flags & 2 else "le")
            elif codec in PCM and PCM[codec]:
                tr.pcm = PCM[codec]
            if tr.pcm and not tr.sample_size:
                tr.sample_size = tr.channels * tr.bits // 8
        tracks.append(tr)
    return tracks


# ---------------------------------------------------------------- sample validation

def avc_nal(h):
    """(valid, is_vcl, starts_picture, is_key) for an H.264 NAL header + first payload byte."""
    if h[0] & 0x80:
        return False, False, False, False
    t = h[0] & 31
    if t == 0 or t in (22, 23) or t > 23 and t < 32:
        return t > 23, False, False, False
    if t in (1, 5):
        return True, True, bool(h[1] & 0x80), t == 5  # first_mb_in_slice == 0 -> ue(v) starts with '1'
    return True, False, t in (6, 7, 8, 9), False


def hevc_nal(h):
    if h[0] & 0x80 or (h[1] & 7) == 0:
        return False, False, False, False
    t = (h[0] >> 1) & 63
    if t > 47:
        return False, False, False, False
    if t <= 31:
        if 10 <= t <= 15 or 22 <= t <= 31:
            return False, False, False, False
        return True, True, bool(h[2] & 0x80), 16 <= t <= 21
    return True, False, t in (32, 33, 34, 35, 39), False


def _ue(bits, i):
    z = 0
    while i < len(bits) and bits[i] == "0":
        z += 1
        i += 1
    if z > 16 or i + z >= len(bits) + 1:
        return None, i
    v = int("1" + bits[i + 1:i + 1 + z], 2) - 1 if z else 0
    return v, i + 1 + z


def plausible_slice(data, pos, nal_len, kind):
    """Stronger check for a lone VCL NAL: a picture's first slice with a sane slice header."""
    hdr = 1 if kind == "avc" else 2
    raw = data[pos + nal_len:pos + nal_len + hdr + 6]
    if len(raw) < hdr + 2:
        return False
    bits = "".join("{:08b}".format(b) for b in raw[hdr:])
    if kind == "avc":
        if bits[0] != "1":
            return False
        st, i = _ue(bits, 1)
        pps, i = _ue(bits, i)
        return st is not None and st <= 9 and pps is not None and pps <= 8
    if bits[0] != "1":
        return False
    t = (raw[0] >> 1) & 63
    i = 2 if 16 <= t <= 23 else 1  # no_output_of_prior_pics_flag on IRAP
    pps, i = _ue(bits, i)
    return pps is not None and pps <= 8


def nal_chain(data, pos, nal_len, kind, limit):
    """Walk length-prefixed NAL units from ``pos``; return [(offset, length, vcl, starts_pic, key)]."""
    out = []
    check = avc_nal if kind == "avc" else hevc_nal
    end = len(data)
    while pos + nal_len + 3 <= end and len(out) < limit:
        n = int.from_bytes(data[pos:pos + nal_len], "big")
        if n < 2 or pos + nal_len + n > end or n > 64 << 20:
            break
        ok, vcl, start_pic, key = check(data[pos + nal_len:pos + nal_len + 3])
        if not ok:
            break
        out.append((pos, nal_len + n, vcl, start_pic, key))
        pos += nal_len + n
    return out


def valid_sample(data, pos, size, track):
    """True if ``size`` bytes at ``pos`` look like one sample of ``track`` (used for reassembly)."""
    if track.codec in PRORES:
        return data[pos + 4:pos + 8] == b"icpf" and int.from_bytes(data[pos:pos + 4], "big") == size
    kind = "avc" if track.codec in AVC else "hevc" if track.codec in HEVC else None
    if not kind:
        return None
    total = 0
    for off, ln, *_ in nal_chain(data, pos, track.nal_len, kind, 4096):
        total += ln
        if total >= size:
            break
    return total == size


# ---------------------------------------------------------------- moov writer

def _u32s(vals):
    return b"".join(struct.pack(">I", v) for v in vals)


def stbl_for(track, samples, chunks=None):
    """stbl for ``samples`` = [(offset, size, key)] (one sample per chunk unless ``chunks`` given).

    ``chunks`` = [(offset, n_samples)] for constant-size PCM audio.
    """
    if chunks is not None:
        n_samples = sum(c[1] for c in chunks)
        stts = full(b"stts", 0, 0, struct.pack(">III", 1, n_samples, 1))
        runs, prev = [], None
        for i, (off, k) in enumerate(chunks, 1):
            if k != prev:
                runs.append((i, k, 1))
                prev = k
        stsc = full(b"stsc", 0, 0, struct.pack(">I", len(runs)) + b"".join(struct.pack(">III", *r) for r in runs))
        stsz = full(b"stsz", 0, 0, struct.pack(">II", track.sample_size, n_samples))
        offs = [c[0] for c in chunks]
        stss = b""
    else:
        stts = full(b"stts", 0, 0, struct.pack(">III", 1, len(samples), track.delta or 1))
        stsc = full(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, 1, 1))
        stsz = full(b"stsz", 0, 0, struct.pack(">II", 0, len(samples)) + _u32s(s[1] for s in samples))
        offs = [s[0] for s in samples]
        keys = [i + 1 for i, s in enumerate(samples) if s[2]]
        stss = full(b"stss", 0, 0, struct.pack(">I", len(keys)) + _u32s(keys)) if keys and len(keys) < len(samples) else b""
    if offs and max(offs) > 0xFFFFFFFF:
        co = full(b"co64", 0, 0, struct.pack(">I", len(offs)) + b"".join(struct.pack(">Q", o) for o in offs))
    else:
        co = full(b"stco", 0, 0, struct.pack(">I", len(offs)) + _u32s(offs))
    return box(b"stbl", track.stsd + stts + stss + stsc + stsz + co)


def trak_for(track, track_id, stbl, duration, movie_timescale):
    tkhd = bytearray(track.tkhd) if track.tkhd else bytearray(full(b"tkhd", 0, 3, b"\0" * 80))
    v = tkhd[8]
    mdur = duration * movie_timescale // max(1, track.timescale)
    if v == 1:
        struct.pack_into(">I", tkhd, 28, track_id)
        struct.pack_into(">Q", tkhd, 36, mdur)
    else:
        struct.pack_into(">I", tkhd, 20, track_id)
        struct.pack_into(">I", tkhd, 28, min(mdur, 0xFFFFFFFF))
    mdhd = full(b"mdhd", 1, 0, struct.pack(">QQIQHH", 0, 0, track.timescale, duration, 0x55C4, 0))
    dinf = box(b"dinf", full(b"dref", 0, 0, struct.pack(">I", 1) + full(b"url ", 0, 1, b"")))
    head = track.minf_header or (full(b"vmhd", 0, 1, b"\0" * 8) if track.handler == b"vide" else full(b"smhd", 0, 0, b"\0" * 4))
    minf = box(b"minf", head + dinf + stbl)
    return box(b"trak", bytes(tkhd) + box(b"mdia", mdhd + track.hdlr + minf))


def moov(traks_and_durations, timescale=1000):
    """``traks_and_durations`` = [(trak bytes, duration in movie timescale)]."""
    dur = max((d for _, d in traks_and_durations), default=0)
    mvhd = full(b"mvhd", 1, 0, struct.pack(">QQIQ", 0, 0, timescale, dur) + struct.pack(">IH", 0x00010000, 0x0100)
                + b"\0" * 10 + struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + b"\0" * 24
                + struct.pack(">I", len(traks_and_durations) + 1))
    return box(b"moov", mvhd + b"".join(t for t, _ in traks_and_durations))
