"""Corrupted document repair (Recoverit "Corrupted Document Repair").

* ZIP-based documents (DOCX/XLSX/PPTX, Pages/Numbers/Keynote, ODF, EPUB,
  plain ZIP): the central directory is often what breaks (truncated download,
  bad sector at the end of the file). Every member is rebuilt from its *local*
  header, decompressed and CRC-checked, and a fresh archive is written.
* PDF: rebuild the cross-reference table from the objects actually present and
  write a new trailer pointing at the document catalog.
* SQLite: ``sqlite3 .recover``.
* Text-like files (TXT/CSV/RTF/XML/JSON): drop NUL runs and invalid bytes.
"""
import os
import re
import shutil
import struct
import subprocess
import zipfile
import zlib

from ..util import chown_user, unique

ZIP_EXT = {"docx", "docm", "dotx", "xlsx", "xlsm", "xltx", "pptx", "pptm", "potx", "ppsx", "pages", "numbers", "key",
           "odt", "ods", "odp", "odg", "epub", "zip", "jar", "apk", "ipa", "xps", "oxps", "vsdx", "3mf", "idml", "sketch"}
TEXT_EXT = {"txt", "csv", "tsv", "rtf", "xml", "json", "html", "htm", "md", "log", "srt", "ini", "yaml", "yml"}


def _out(path, dest, suffix):
    base, e = os.path.splitext(os.path.basename(path))
    d = dest or os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    return unique(os.path.join(d, base + suffix + e))


# ---------------------------------------------------------------- ZIP family

def zip_members(b):
    """Yield (name, method, flags, data_or_None, crc, note) for every local file header in ``b``."""
    pos = 0
    while True:
        i = b.find(b"PK\x03\x04", pos)
        if i < 0 or i + 30 > len(b):
            return
        ver, flags, method, mt, md, crc, csize, usize, nlen, xlen = struct.unpack_from("<HHHHHIIIHH", b, i + 4)
        name_b = b[i + 30:i + 30 + nlen]
        start = i + 30 + nlen + xlen
        if nlen == 0 or nlen > 1024 or start > len(b):
            pos = i + 4
            continue
        name = name_b.decode("utf-8" if flags & 0x800 else "cp437", "replace")
        if csize == 0xFFFFFFFF or usize == 0xFFFFFFFF:  # zip64 sizes in the extra field
            x = b[i + 30 + nlen:start]
            p = 0
            while p + 4 <= len(x):
                hid, hl = struct.unpack_from("<HH", x, p)
                if hid == 1 and hl >= 16:
                    usize, csize = struct.unpack_from("<QQ", x, p + 4)
                p += 4 + hl
        data, note = None, ""
        if flags & 8 or (csize == 0 and method == 8):
            # sizes are in a data descriptor after the data: inflate until the stream ends
            if method == 8:
                d = zlib.decompressobj(-15)
                try:
                    data = d.decompress(b[start:start + (512 << 20)])
                    used = len(b[start:start + (512 << 20)]) - len(d.unused_data)
                    if not d.eof:
                        note = "flujo deflate incompleto"
                    csize = used
                except zlib.error as e:
                    note = "deflate dañado: %s" % e
                    data = None
            else:
                j = b.find(b"PK\x07\x08", start)
                k = b.find(b"PK\x03\x04", start)
                ends = [x for x in (j, k) if x > 0]
                csize = (min(ends) if ends else len(b)) - start
                data = b[start:start + csize]
            if b[start + csize:start + csize + 4] == b"PK\x07\x08":
                crc = struct.unpack_from("<I", b, start + csize + 4)[0]
            elif data is not None:
                crc = zlib.crc32(data) & 0xFFFFFFFF
        else:
            raw = b[start:start + csize]
            if len(raw) < csize:
                note = "miembro truncado"
            if method == 0:
                data = raw
            elif method == 8:
                d = zlib.decompressobj(-15)
                try:
                    data = d.decompress(raw)
                    if not d.eof:
                        note = note or "flujo deflate incompleto"
                except zlib.error as e:
                    note, data = "deflate dañado: %s" % e, None
            else:
                note = "método de compresión %d no soportado" % method
        yield name, method, flags, data, crc, note
        pos = start + max(csize, 0) if csize and csize < len(b) else i + 4


def repair_zip(path, dest=None):
    with open(path, "rb") as f:
        b = f.read()
    out = _out(path, dest, "_reparado")
    ok_n, bad, seen = 0, [], set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, method, flags, data, crc, note in zip_members(b):
            if name in seen:
                continue
            seen.add(name)
            if data is None:
                bad.append({"name": name, "note": note})
                continue
            crc_ok = (zlib.crc32(data) & 0xFFFFFFFF) == crc
            if not crc_ok and not note:
                note = "CRC no coincide (contenido parcialmente dañado)"
            if note:
                bad.append({"name": name, "note": note})
                if not data:
                    continue
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            z.writestr(info, data)
            ok_n += 1
    if ok_n == 0:
        os.remove(out)
        return {"ok": False, "error": "no se encontró ningún miembro ZIP recuperable"}
    chown_user(out)
    try:
        with zipfile.ZipFile(out) as z:
            valid = z.testzip() is None
    except zipfile.BadZipFile:
        valid = False
    return {"ok": valid, "out": out, "method": "archivo reconstruido desde las cabeceras locales",
            "detail": {"recuperados": ok_n, "con_problemas": bad[:50]}}


# ---------------------------------------------------------------- PDF

_OBJ = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")


def repair_pdf(path, dest=None):
    with open(path, "rb") as f:
        b = f.read()
    if b"%PDF" not in b[:1024]:
        return {"ok": False, "error": "no es un PDF (falta la cabecera %PDF)"}
    start = b.find(b"%PDF")
    body = b[start:]
    objs = {}
    for m in _OBJ.finditer(body):
        objs[int(m.group(1))] = (m.start(), int(m.group(2)))  # last definition wins (incremental updates)
    if not objs:
        return {"ok": False, "error": "no se encontraron objetos PDF"}
    root = None
    for num, (off, gen) in sorted(objs.items()):
        seg = body[off:off + 4096]
        if re.search(rb"/Type\s*/Catalog", seg.split(b"endobj")[0]):
            root = (num, gen)
    if root is None:
        return {"ok": False, "error": "no se encontró el catálogo del documento (/Type /Catalog)"}
    info = None
    m = re.search(rb"/Info\s+(\d+)\s+(\d+)\s+R", body)
    if m and int(m.group(1)) in objs:
        info = (int(m.group(1)), int(m.group(2)))
    # cut trailing garbage after the last endobj, then append a fresh xref
    last = body.rfind(b"endobj")
    content = body[:last + 6] if last > 0 else body
    content = content.rstrip() + b"\n"
    size = max(objs) + 1
    lines = [b"xref\n", b"0 %d\n" % size, b"0000000000 65535 f \n"]
    for n in range(1, size):
        if n in objs:
            lines.append(b"%010d %05d n \n" % (objs[n][0], objs[n][1]))
        else:
            lines.append(b"0000000000 65535 f \n")
    xref_off = len(content)
    trailer = b"trailer\n<< /Size %d /Root %d %d R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        size, root[0], root[1], (b" /Info %d %d R" % info) if info else b"", xref_off)
    out = _out(path, dest, "_reparado")
    with open(out, "wb") as w:
        w.write(content + b"".join(lines) + trailer)
    chown_user(out)
    return {"ok": True, "out": out, "method": "tabla xref y trailer reconstruidos",
            "detail": {"objetos": len(objs), "catalogo": "%d %d R" % root}}


# ---------------------------------------------------------------- others

def repair_sqlite(path, dest=None):
    exe = shutil.which("sqlite3") or "/usr/bin/sqlite3"
    out = _out(path, dest, "_reparado")
    p = subprocess.run([exe, path, ".recover"], capture_output=True, timeout=1800)
    if p.returncode != 0 or not p.stdout:
        return {"ok": False, "error": (p.stderr.decode("utf-8", "replace").strip() or "sqlite3 .recover falló")}
    q = subprocess.run([exe, out], input=p.stdout, capture_output=True, timeout=1800)
    if q.returncode != 0:
        return {"ok": False, "error": q.stderr.decode("utf-8", "replace").strip()}
    chown_user(out)
    return {"ok": True, "out": out, "method": "sqlite3 .recover (filas recuperables volcadas a una base nueva)"}


def repair_text(path, dest=None):
    with open(path, "rb") as f:
        b = f.read()
    clean = re.sub(rb"\x00+", b"", b)
    text = clean.decode("utf-8", "replace") if not clean.startswith(b"{\\rtf") else clean.decode("latin-1")
    out = _out(path, dest, "_reparado")
    with open(out, "w", encoding="utf-8" if not clean.startswith(b"{\\rtf") else "latin-1") as w:
        w.write(text)
    chown_user(out)
    return {"ok": True, "out": out, "method": "%d bytes nulos/invalidos eliminados" % (len(b) - len(clean))}


def repair(path, dest=None):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    with open(path, "rb") as f:
        head = f.read(8)
    if ext == "pdf" or head.startswith(b"%PDF"):
        return repair_pdf(path, dest)
    if ext in ZIP_EXT or head.startswith(b"PK"):
        return repair_zip(path, dest)
    if ext in ("sqlite", "sqlite3", "db") or head.startswith(b"SQLite f"):
        return repair_sqlite(path, dest)
    if ext in TEXT_EXT:
        return repair_text(path, dest)
    return {"ok": False, "error": "formato de documento no soportado: .%s" % ext}
