"""Corrupted photo repair (Recoverit "Corrupted Photo Repair", incl. RAW).

* JPEG truncated or with garbage after the image: cut at the last complete
  entropy segment and close it with EOI (decoders grey-fill the missing part).
* JPEG with a damaged header: transplant the header (quantisation/Huffman
  tables, frame header) from a *sample photo* of the same camera and keep the
  damaged file's own scan data — RecoFix's equivalent of "Add a sample photo".
* Embedded preview rescue: RAW files (CR2/CR3/NEF/ARW/DNG/ORF/RW2/RAF…) and
  JPEGs carry full-size or large JPEG previews; when the main image cannot be
  saved the largest decodable preview is extracted.
* PNG: recompute chunk CRCs and close the file with IEND.
"""
import os
import struct
import subprocess
import zlib

from ..util import chown_user, tool, unique

FFMPEG = tool("ffmpeg")
SIPS = "/usr/bin/sips" if os.path.exists("/usr/bin/sips") else None


def _out(path, dest, suffix, ext=None):
    base, e = os.path.splitext(os.path.basename(path))
    d = dest or os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    return unique(os.path.join(d, base + suffix + (ext or e)))


def decodes(path):
    """(ok, detail): whether a decoder can render the image without fatal errors."""
    if FFMPEG:
        p = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-frames:v", "1", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        return p.returncode == 0, p.stderr.strip().splitlines()[:3]
    if SIPS:
        p = subprocess.run([SIPS, "-g", "pixelWidth", path], capture_output=True, text=True, timeout=60)
        return "pixelWidth" in p.stdout, []
    return True, []


# ---------------------------------------------------------------- JPEG structure

def jpeg_segments(b):
    """[(marker, offset, length)] up to and including SOS; plus scan data start or -1."""
    if b[:2] != b"\xff\xd8":
        return [], -1
    segs, pos = [], 2
    while pos + 4 <= len(b):
        if b[pos] != 0xFF:
            return segs, -1
        m = b[pos + 1]
        if m == 0xFF:
            pos += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            pos += 2
            continue
        ln = struct.unpack_from(">H", b, pos + 2)[0]
        if ln < 2 or pos + 2 + ln > len(b):
            return segs, -1
        segs.append((m, pos, 2 + ln))
        pos += 2 + ln
        if m == 0xDA:
            return segs, pos
    return segs, -1


def entropy_end(b, start):
    """End of the last complete entropy-coded data (before a real marker, junk or EOF)."""
    i = start
    last_good = start
    n = len(b)
    while True:
        i = b.find(b"\xff", i)
        if i < 0 or i + 1 >= n:
            return n if i < 0 else i
        m = b[i + 1]
        if m == 0 or m == 0xFF:
            i += 1 if m == 0xFF else 2
            continue
        if 0xD0 <= m <= 0xD7:
            last_good = i
            i += 2
            continue
        if m == 0xD9:
            return i
        # a marker inside scan data: progressive JPEGs have more scans; anything else ends it
        if m in (0xC4, 0xDA, 0xDB, 0xDD):
            return -i  # caller handles multi-scan
        return max(last_good, i)


def _has_tables(b, segs):
    kinds = {m for m, _, _ in segs}
    return 0xDB in kinds and (0xC4 in kinds or 0xC2 in kinds and 0xC4 in kinds) and \
        any(0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC) for m in kinds)


def repair_jpeg(path, sample=None, dest=None):
    with open(path, "rb") as f:
        b = f.read()
    steps = []
    segs, scan = jpeg_segments(b)
    if scan < 0 or not _has_tables(b, segs):
        if not sample:
            prev = extract_preview(path, dest)
            if prev.get("ok"):
                prev["method"] = "cabecera dañada: se rescató la vista previa incrustada"
                return prev
            return {"ok": False, "needs_sample": True,
                    "error": "La cabecera JPEG está dañada. Añade una foto de muestra de la misma cámara."}
        with open(sample, "rb") as f:
            s = f.read()
        ssegs, sscan = jpeg_segments(s)
        if sscan < 0:
            return {"ok": False, "error": "la foto de muestra no es un JPEG válido"}
        header = s[:sscan]
        # scan data of the damaged file: after its own SOS if it survived, else after the sample's header length
        data_start = scan if scan >= 0 else _guess_scan_start(b, len(header))
        body = b[data_start:]
        steps.append("cabecera de la muestra (%d bytes) + datos de imagen propios desde el byte %d" % (len(header), data_start))
    else:
        header, body = b[:scan], b[scan:]
    end = entropy_end(body, 0)
    if end < 0:  # progressive: keep everything up to the final EOI or the end
        e = body.rfind(b"\xff\xd9")
        end = e if e > 0 else len(body)
    if end < len(body) - 2:
        steps.append("se descartan %d bytes dañados/sobrantes" % (len(body) - end))
    elif not body[end:end + 2] == b"\xff\xd9":
        steps.append("archivo truncado: se cierra la imagen")
    out = _out(path, dest, "_reparado", ".jpg")
    with open(out, "wb") as w:
        w.write(header + body[:end] + b"\xff\xd9")
    ok, det = decodes(out)
    chown_user(out)
    return {"ok": ok, "out": out, "method": "; ".join(steps) or "estructura JPEG normalizada", "decoder": det}


def _guess_scan_start(b, hint):
    """Damaged header: entropy data usually starts right where the original header ended."""
    for m in (b.find(b"\xff\xda"),):
        if m >= 0 and m + 4 < len(b):
            ln = struct.unpack_from(">H", b, m + 2)[0]
            return m + 2 + ln
    return min(hint, len(b))


# ---------------------------------------------------------------- previews inside RAW/JPEG

def embedded_jpegs(b, min_size=16 << 10):
    """[(offset, length)] of complete JPEG streams embedded in ``b`` (largest first)."""
    out = []
    pos = 1
    while True:
        i = b.find(b"\xff\xd8\xff", pos)
        if i < 0:
            break
        segs, scan = jpeg_segments(b[i:i + (4 << 20)])
        if scan > 0 and _has_tables(b[i:], segs):
            j = b.find(b"\xff\xd9", i + scan)
            if j > 0 and j + 2 - i >= min_size:
                out.append((i, j + 2 - i))
                pos = j + 2
                continue
        pos = i + 3
    return sorted(out, key=lambda x: -x[1])


def extract_preview(path, dest=None):
    with open(path, "rb") as f:
        b = f.read()
    for off, ln in embedded_jpegs(b):
        out = _out(path, dest, "_vista_previa", ".jpg")
        with open(out, "wb") as w:
            w.write(b[off:off + ln])
        ok, _ = decodes(out)
        if ok:
            chown_user(out)
            return {"ok": True, "out": out, "method": "vista previa incrustada (%d KB, offset %d)" % (ln >> 10, off)}
        os.remove(out)
    return {"ok": False, "error": "no hay vistas previas JPEG recuperables dentro del archivo"}


# ---------------------------------------------------------------- PNG

def repair_png(path, dest=None):
    with open(path, "rb") as f:
        b = f.read()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        b = b"\x89PNG\r\n\x1a\n" + b[8:] if b[1:4] == b"PNG" else b
    out_chunks, pos, fixed, ended = [], 8, 0, False
    while pos + 12 <= len(b):
        n, typ = struct.unpack_from(">I4s", b, pos)
        if not typ.isalpha():
            break
        if pos + 12 + n > len(b):
            if typ != b"IDAT":
                break
            n = len(b) - pos - 8  # truncated image data: keep what is there
            if n <= 0:
                break
            b += b"\0\0\0\0"
        data = b[pos + 8:pos + 8 + n]
        crc = struct.unpack_from(">I", b, pos + 8 + n)[0]
        good = zlib.crc32(typ + data) & 0xFFFFFFFF
        if crc != good:
            fixed += 1
        out_chunks.append(struct.pack(">I4s", n, typ) + data + struct.pack(">I", good))
        pos += 12 + n
        if typ == b"IEND":
            ended = True
            break
    if not out_chunks or not out_chunks[0][4:8] == b"IHDR":
        return {"ok": False, "error": "falta la cabecera IHDR del PNG"}
    if not ended:
        out_chunks.append(struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF))
    out = _out(path, dest, "_reparado", ".png")
    with open(out, "wb") as w:
        w.write(b"\x89PNG\r\n\x1a\n" + b"".join(out_chunks))
    ok, det = decodes(out)
    steps = []
    if not ok:
        rebuilt = _png_reencode(out_chunks)
        if rebuilt:
            with open(out, "wb") as w:
                w.write(rebuilt[0])
            ok, det = decodes(out)
            steps.append("datos de imagen recomprimidos: %d de %d filas intactas" % rebuilt[1:])
    chown_user(out)
    if fixed:
        steps.append("%d CRC corregidos" % fixed)
    if not ended:
        steps.append("IEND añadido (archivo truncado)")
    return {"ok": ok, "out": out, "method": "; ".join(steps) or "estructura PNG normalizada", "decoder": det}


def _png_reencode(chunks):
    """Inflate whatever IDAT data survives, pad the missing rows and write a single clean IDAT."""
    ihdr = chunks[0][8:21]
    w, h, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr)
    if interlace or w == 0 or h == 0 or w * h > 1 << 28:
        return None
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if not ch:
        return None
    stride = (w * ch * depth + 7) // 8 + 1
    idat = b"".join(c[8:-4] for c in chunks if c[4:8] == b"IDAT")
    d = zlib.decompressobj()
    raw = b""
    for i in range(0, len(idat), 64):  # small pieces: a corrupt spot only costs the rows after it
        try:
            raw += d.decompress(idat[i:i + 64])
        except zlib.error:
            break
    good = min(h, len(raw) // stride)
    raw = raw[:good * stride] + (b"\0" * stride) * (h - good)
    keep = [c for c in chunks if c[4:8] not in (b"IDAT", b"IEND")]
    data = zlib.compress(raw, 6)
    idat_chunk = struct.pack(">I4s", len(data), b"IDAT") + data + struct.pack(">I", zlib.crc32(b"IDAT" + data) & 0xFFFFFFFF)
    iend = struct.pack(">I4s", 0, b"IEND") + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + b"".join(keep) + idat_chunk + iend, good, h


RAW_EXT = {"cr2", "cr3", "nef", "nrw", "arw", "sr2", "dng", "orf", "rw2", "raf", "pef", "srw", "x3f", "gpr", "3fr",
           "iiq", "erf", "kdc", "mrw", "heic"}


def repair(path, sample=None, dest=None):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in ("jpg", "jpeg", "jpe", "jfif", "insp", "thm"):
        return repair_jpeg(path, sample, dest)
    if ext == "png":
        return repair_png(path, dest)
    if ext in RAW_EXT:
        r = extract_preview(path, dest)
        if r.get("ok"):
            r["method"] = "RAW: " + r["method"]
        return r
    return {"ok": False, "error": "formato de foto no soportado para reparación: .%s" % ext}
