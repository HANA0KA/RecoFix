"""MXF (SMPTE ST 377-1) recovery.

Recoverit links BBC libMXF/libMXF++ for this; we parse the same structures
directly:

* Header partition pack -> version, KAG, FooterPartition offset, OP, essence containers.
* If the header points at a valid footer, the file ends after the footer
  partition (metadata, index) and the Random Index Pack (RIP).
* If not (camera lost power, open/incomplete header), every KLV triplet is
  walked from the header until the data stops being valid MXF, the next clip's
  header starts, or a footer/RIP closes the file.
* ``finalize`` turns a truncated clip into a structurally valid MXF by
  appending a footer partition and a RIP and patching FooterPartition in the
  copied partition packs (the source is never touched).
"""
import struct

from .base import Found, Handler, u64be

UL = b"\x06\x0e\x2b\x34"
PP_PREFIX = bytes.fromhex("060e2b34020501010d01020101")  # + kind, status, 00
RIP_KEY = bytes.fromhex("060e2b34020501010d01020101110100")
PRIMER_KEY = bytes.fromhex("060e2b34020501010d01020101050100")
FILL_PREFIX = bytes.fromhex("060e2b3401010101030102100100")

KIND = {0x02: "header", 0x03: "body", 0x04: "footer"}
STATUS = {0x01: "open incomplete", 0x02: "closed incomplete", 0x03: "open complete", 0x04: "closed complete"}

ESSENCE = {
    0x01: "MPEG-2 D-10 (IMX)", 0x02: "DV", 0x04: "MPEG-2 (XDCAM)", 0x05: "Sin comprimir",
    0x06: "Audio WAVE/BWF", 0x07: "Audio AES3", 0x0C: "JPEG 2000", 0x10: "AVC/H.264 (XAVC, AVC-Intra)",
    0x11: "VC-3/DNxHD", 0x1C: "Apple ProRes", 0x7F: "Generic Container (multiple)",
}
WRAPPING = {0x01: "frame", 0x02: "clip", 0x7F: "custom"}

CODING = [
    (bytes.fromhex("0401020203 06"), "Apple ProRes"),
    (bytes.fromhex("0401020203 01"), "JPEG 2000"),
    (bytes.fromhex("0401020271"), "VC-3/DNxHD"),
    (bytes.fromhex("0401020201 31"), "H.264/AVC"),
    (bytes.fromhex("0401020201 32"), "H.265/HEVC"),
    (bytes.fromhex("0401020201"), "MPEG-2"),
    (bytes.fromhex("0401020202"), "DV"),
    (bytes.fromhex("04010201"), "Sin comprimir"),
]

PP_FMT = ">HHIQQQQQIQI"  # major minor kag this prev footer hbc ibc indexsid bodyoffset bodysid
PP_FIXED = struct.calcsize(PP_FMT)  # 64, then OP UL(16), then essence container batch


def read_klv(r, off):
    """(key, value_offset, value_length) or None if no valid KLV starts at ``off``."""
    h = r.read(off, 25)
    if len(h) < 17 or not h.startswith(UL) or not 1 <= h[4] <= 0x0E:
        return None
    b = h[16]
    if b < 0x80:
        return h[:16], off + 17, b
    n = b & 0x7F
    if n == 0 or n > 8 or len(h) < 17 + n:
        return None
    return h[:16], off + 17 + n, int.from_bytes(h[17:17 + n], "big")


def ber(n, size=None):
    size = size or max(4, (n.bit_length() + 7) // 8)
    return bytes([0x80 | size]) + n.to_bytes(size, "big")


def is_pp(key, kind=None):
    return key.startswith(PP_PREFIX) and key[13] in KIND and (kind is None or key[13] == kind)


def parse_pp(v):
    if len(v) < PP_FIXED + 16:
        return None
    f = struct.unpack_from(PP_FMT, v, 0)
    pp = dict(zip(("major", "minor", "kag", "this", "prev", "footer", "hbc", "ibc",
                   "index_sid", "body_offset", "body_sid"), f))
    pp["op"] = v[PP_FIXED:PP_FIXED + 16]
    ecs = []
    if len(v) >= PP_FIXED + 24:
        cnt, ilen = struct.unpack_from(">II", v, PP_FIXED + 16)
        if ilen == 16 and cnt < 64:
            p = PP_FIXED + 24
            ecs = [v[p + 16 * i:p + 16 * i + 16] for i in range(cnt) if p + 16 * i + 16 <= len(v)]
    pp["ecs"] = ecs
    return pp


def op_name(op):
    if not op.startswith(UL) or op[8:12] != bytes.fromhex("0d010201"):
        return "?"
    if op[12] == 0x10:
        return "OP-Atom"
    if 1 <= op[12] <= 3 and 1 <= op[13] <= 3:
        return "OP%d%s" % (op[12], "abc"[op[13] - 1])
    return "?"


def ec_name(ul):
    if ul[8:13] == bytes.fromhex("0d01030102"):
        n = ESSENCE.get(ul[13], "Essence 0x%02X" % ul[13])
        if ul[13] not in (0x01, 0x06, 0x07, 0x7F):
            w = WRAPPING.get(ul[14])
            if w:
                n += " (%s-wrapped)" % w
        return n
    return ul.hex()


def coding_name(ul):
    tail = ul[8:]
    for pre, n in CODING:
        if tail.startswith(pre):
            return n
    return ul.hex()


def _utf16(b):
    return b.decode("utf-16-be", "replace").rstrip("\0").strip()


def _rational(b):
    if len(b) >= 8:
        n, d = struct.unpack(">ii", b[:8])
        if d:
            return n / d
    return None


def _collect(meta, it):
    """Merge the interesting items of one local set into ``meta``."""
    for tag, k in ((0x3C01, "fabricante"), (0x3C02, "producto"), (0x3C04, "version_sw")):
        if tag in it:
            meta.setdefault(k, _utf16(it[tag]))
    if it.get(0x4402):
        name = _utf16(it[0x4402])
        if name:
            meta.setdefault("nombre_paquete", name)
    if len(it.get(0x1501, b"")) == 8:
        meta.setdefault("_tc_start", u64be(it[0x1501]))
        meta.setdefault("_tc_base", int.from_bytes(it.get(0x1502, b"\0"), "big"))
        meta.setdefault("_tc_drop", (it.get(0x1503) or b"\0")[0])
    w = it.get(0x3209) or it.get(0x3203)
    h = it.get(0x3208) or it.get(0x3202)
    if w and h and len(w) == 4 and len(h) == 4 and "ancho" not in meta:
        # picture descriptor: its SampleRate is the frame rate
        meta["ancho"] = int.from_bytes(w, "big")
        meta["alto"] = int.from_bytes(h, "big")
        fps = _rational(it.get(0x3001, b""))
        if fps:
            meta["fps"] = round(fps, 3)
        if len(it.get(0x3002, b"")) == 8:
            meta["_frames"] = u64be(it[0x3002])
        if len(it.get(0x3201, b"")) == 16:
            meta["codec"] = coding_name(it[0x3201])
    if 0x3D03 in it:
        sr = _rational(it[0x3D03])
        if sr:
            meta.setdefault("audio_hz", int(sr))
        if len(it.get(0x3D07, b"")) == 4 and 0x3F01 not in it:  # skip MultipleDescriptor totals
            meta["audio_canales"] = meta.get("audio_canales", 0) + int.from_bytes(it[0x3D07], "big")
        if len(it.get(0x3D01, b"")) == 4:
            meta.setdefault("audio_bits", int.from_bytes(it[0x3D01], "big"))


def parse_header_metadata(r, start, end):
    """Pull human-useful facts out of the local sets between ``start`` and ``end``."""
    meta = {}
    pos = start
    while pos < end:
        k = read_klv(r, pos)
        if not k:
            break
        key, vo, vl = k
        if is_pp(key) or vl > 16 << 20:
            break
        if key[5] == 0x53 and vl:
            v = r.read(vo, vl)
            items = {}
            i = 0
            while i + 4 <= len(v):
                tag, ln = struct.unpack_from(">HH", v, i)
                items.setdefault(tag, v[i + 4:i + 4 + ln])
                i += 4 + ln
            _collect(meta, items)
        pos = vo + vl
    tc = meta.pop("_tc_start", None)
    base = meta.pop("_tc_base", None)
    drop = meta.pop("_tc_drop", 0)
    if tc is not None and base:
        f = tc % base
        s = tc // base
        meta["timecode"] = "%02d:%02d:%02d%s%02d" % (s // 3600 % 24, s // 60 % 60, s % 60, ";" if drop else ":", f)
    frames = meta.pop("_frames", None)
    if frames is not None:
        meta["frames"] = frames
        if meta.get("fps"):
            meta["duracion_s"] = round(frames / meta["fps"], 2)
    return meta


def walk(r, start, first, limit):
    """Walk KLVs from ``first``; return (end, partitions, closed, reason).

    ``partitions`` is [(offset_rel_to_start, key, pp)] for RIP rebuilding.
    """
    pos = first
    parts = []
    stop = min(r.size, start + limit)
    while pos < stop:
        k = read_klv(r, pos)
        if not k:
            return pos, parts, False, "datos no-MXF tras %d bytes" % (pos - start)
        key, vo, vl = k
        end = vo + vl
        if end > stop:
            return pos, parts, False, "KLV truncado (fin del medio o tamaño límite)"
        if key == RIP_KEY:
            return end, parts, True, ""
        if is_pp(key):
            if key[13] == 0x02:
                return pos, parts, False, "empieza otro clip MXF"
            pp = parse_pp(r.read(vo, min(vl, 4096)))
            if pp is None or pp["this"] != pos - start:
                return pos, parts, False, "partición inconsistente (otro fichero o fragmentación)"
            parts.append((pos - start, key, pp))
        pos = end
    return pos, parts, False, "límite de tamaño"


class MXFHandler(Handler):
    key = "mxf"
    name = "Material eXchange Format"
    category = "video"
    ext = "mxf"
    signatures = ((0, PP_PREFIX + b"\x02"),)
    max_size = 1 << 42

    def probe(self, r, off):
        k = read_klv(r, off)
        if not k or not is_pp(k[0], 0x02):
            return None
        key, vo, vl = k
        pp = parse_pp(r.read(vo, min(vl, 4096)))
        if not pp or pp["this"] != 0 or pp["major"] != 1 or pp["kag"] > 1 << 20:
            return None
        hdr_end = vo + vl
        meta = {
            "op": op_name(pp["op"]),
            "estado_cabecera": STATUS.get(key[14], "?"),
            "contenedores": [ec_name(e) for e in pp["ecs"]],
        }
        md_end = hdr_end + pp["hbc"] if pp["hbc"] else min(r.size, hdr_end + (8 << 20))
        try:
            meta.update(parse_header_metadata(r, hdr_end, md_end))
        except Exception:  # metadata is a bonus; a damaged set must not lose the clip
            pass

        end, closed, note, parts = None, False, "", None
        if pp["footer"]:
            fk = read_klv(r, off + pp["footer"])
            if fk and is_pp(fk[0], 0x04):
                fpp = parse_pp(r.read(fk[1], min(fk[2], 4096)))
                if fpp and fpp["this"] == pp["footer"]:
                    end, _, closed, note = walk(r, off, off + pp["footer"], self.max_size)
                    end, closed = (end, True) if closed or end > off + pp["footer"] else (end, False)
            if end is None:
                note = "el footer indicado no está donde dice la cabecera (fragmentado o sobrescrito)"
        if end is None:
            end, parts, closed, why = walk(r, off, hdr_end, self.max_size)
            if any(p[1][13] == 0x04 for p in parts):
                closed = True
            note = note or ("" if closed else why)
        length = end - off
        if parts is not None:
            meta["particiones"] = 1 + len(parts)
        name = meta.get("nombre_paquete", "")
        safe = "".join(c for c in name if c.isalnum() or c in "._- ")[:80].strip()
        return self.found(
            off, length, complete=closed, confidence=0.99 if closed else 0.85,
            name=(safe + ".mxf") if safe else "", meta=meta,
            note="" if closed else "MXF sin footer: " + note + " — usa 'Finalizar MXF' al recuperar")


def needs_finalize(path):
    from ..source import Source
    with Source(path) as s:
        k = read_klv(s, 0)
        if not k or not is_pp(k[0], 0x02):
            return False
        pp = parse_pp(s.read(k[1], min(k[2], 4096)))
        if not pp:
            return False
        if pp["footer"]:
            fk = read_klv(s, pp["footer"])
            if fk and is_pp(fk[0], 0x04):
                return False
        _, parts, closed, _ = walk(s, 0, k[1] + k[2], s.size)
        return not (closed or any(p[1][13] == 0x04 for p in parts))


def finalize(path):
    """Close a truncated MXF in place (operate on the recovered copy, never the source).

    Truncates to the last complete KLV, appends a closed footer partition and a
    RIP, and patches FooterPartition in every partition pack. Returns a report.
    """
    from ..source import Source
    with Source(path) as s:
        k = read_klv(s, 0)
        if not k or not is_pp(k[0], 0x02):
            raise ValueError("no es un MXF con cabecera de partición")
        hkey, hvo, hvl = k
        hpp_raw = s.read(hvo, hvl)
        hpp = parse_pp(hpp_raw)
        end, parts, closed, why = walk(s, 0, hvo + hvl, s.size)
        if closed or any(p[1][13] == 0x04 for p in parts):
            return {"cambiado": False, "motivo": "ya tiene footer"}
        orig = s.size
    parts = [(0, hkey, hpp)] + parts
    footer_at = end
    kag = hpp["kag"] or 1
    if kag > 1 and footer_at % kag:
        # Align the footer to the KAG with a KLV fill item, as the spec requires.
        need = kag - footer_at % kag
        while need < 16 + 5:
            need += kag
        fill = FILL_PREFIX + b"\x00\x00" + ber(need - 16 - 5, 4) + b"\0" * (need - 16 - 5)
    else:
        fill = b""
    footer_at += len(fill)

    tail = hpp_raw[PP_FIXED:]
    value = struct.pack(PP_FMT, hpp["major"], hpp["minor"], hpp["kag"], footer_at, parts[-1][0],
                        footer_at, 0, 0, 0, 0, 0) + tail
    footer = PP_PREFIX + b"\x04\x04\x00" + ber(len(value), 4) + value
    entries = [(0 if p[2] is None else p[2]["body_sid"], p[0]) for p in parts] + [(0, footer_at)]
    rip_body = b"".join(struct.pack(">IQ", sid, o) for sid, o in entries)
    rip_len = 16 + 5 + len(rip_body) + 4
    rip = RIP_KEY + ber(len(rip_body) + 4, 4) + rip_body + struct.pack(">I", rip_len)

    with open(path, "r+b") as f:
        f.truncate(end)
        f.seek(end)
        f.write(fill + footer + rip)
        for p_off, _, _ in parts:
            f.seek(p_off)
            h = f.read(25)
            n = h[16] & 0x7F if h[16] & 0x80 else 0
            field = p_off + 17 + n + 24  # FooterPartition inside the pack value
            f.seek(field)
            f.write(struct.pack(">Q", footer_at))
    return {"cambiado": True, "truncado_desde": orig, "datos_validos": end,
            "footer_en": footer_at, "particiones": len(parts), "motivo": why}
