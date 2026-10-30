"""Generate real sample files of many formats (ffmpeg + stdlib) for the tests."""
import bz2
import gzip
import io
import lzma
import os
import sqlite3
import subprocess
import tarfile
import zipfile

WORK = os.path.join(os.path.dirname(__file__), "_work")
SAMPLES = os.path.join(WORK, "samples")

V = ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25"]
A = ["-f", "lavfi", "-i", "sine=f=440:sample_rate=48000"]
FFMPEG = {
    "clip_xavc.mp4": V + A + ["-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    "clip_hevc.mov": V + ["-t", "2", "-c:v", "libx265", "-tag:v", "hvc1", "-x265-params", "log-level=none"],
    "clip_prores.mov": V + A + ["-t", "1", "-c:v", "prores_ks", "-c:a", "pcm_s16le"],
    "clip.mkv": V + A + ["-t", "2", "-c:v", "libx264", "-c:a", "libopus"],
    "clip.webm": V + ["-t", "1", "-c:v", "libvpx-vp9", "-b:v", "500k"],
    "clip.avi": V + A + ["-t", "2", "-c:v", "mjpeg", "-c:a", "pcm_s16le"],
    "clip.ts": V + A + ["-t", "2", "-c:v", "mpeg2video", "-c:a", "mp2", "-f", "mpegts"],
    "clip.m2ts": V + A + ["-t", "2", "-c:v", "libx264", "-c:a", "aac", "-f", "mpegts", "-mpegts_m2ts_mode", "1"],
    "clip.mpg": V + A + ["-t", "2", "-c:v", "mpeg2video", "-c:a", "mp2", "-f", "vob"],
    "op1a_mpeg2.mxf": V + A + ["-t", "3", "-c:v", "mpeg2video", "-b:v", "8M", "-c:a", "pcm_s16le",
                               "-timecode", "10:00:00:00"],
    "op1a_dnxhr.mxf": ["-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=24", "-t", "1", "-c:v", "dnxhd",
                       "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"],
    "op1a_prores.mxf": ["-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=25", "-t", "1", "-c:v", "prores_ks"],
    "audio.wav": A + ["-t", "2", "-c:a", "pcm_s16le"],
    "audio.mp3": A + ["-t", "3", "-c:a", "libmp3lame", "-metadata", "title=prueba"],
    "audio.flac": A + ["-t", "3", "-c:a", "flac"],
    "audio.opus": A + ["-t", "3", "-c:a", "libopus"],
    "audio.aac": A + ["-t", "3", "-c:a", "aac", "-f", "adts"],
    "audio.aiff": A + ["-t", "1", "-c:a", "pcm_s16be"],
    "audio.m4a": A + ["-t", "2", "-c:a", "aac"],
    "photo.jpg": V + ["-frames:v", "1", "-q:v", "3"],
    "photo.png": V + ["-frames:v", "1"],
    "anim.gif": V + ["-t", "1", "-r", "5"],
    "photo.bmp": V + ["-frames:v", "1"],
    "photo.tif": V + ["-frames:v", "1"],
}

PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj
xref
0 4
0000000000 65535 f
trailer << /Size 4 /Root 1 0 R >>
startxref
0
%%EOF
"""


def _zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files:
            z.writestr(n, d)


def build():
    os.makedirs(SAMPLES, exist_ok=True)
    out = {}
    for name, args in FFMPEG.items():
        p = os.path.join(SAMPLES, name)
        if not os.path.exists(p):
            subprocess.run(["ffmpeg", "-v", "error", "-y"] + args + [p], check=True)
        out[name] = p
    py = {
        "doc.pdf": lambda p: open(p, "wb").write(PDF),
        "archivo.zip": lambda p: _zip(p, [("a.txt", b"hola " * 2000), ("b/c.bin", os.urandom(30000))]),
        "informe.docx": lambda p: _zip(p, [("[Content_Types].xml", b"<Types/>"), ("word/document.xml", b"<w/>" * 500)]),
        "hoja.xlsx": lambda p: _zip(p, [("[Content_Types].xml", b"<Types/>"), ("xl/workbook.xml", b"<x/>" * 500)]),
        "texto.odt": lambda p: _odt(p),
        "datos.sqlite": _sqlite,
        "paquete.tar": _tar,
        "comprimido.gz": lambda p: open(p, "wb").write(gzip.compress(os.urandom(5000) + b"z" * 50000)),
        "comprimido.bz2": lambda p: open(p, "wb").write(bz2.compress(os.urandom(5000) + b"z" * 50000)),
        "comprimido.xz": lambda p: open(p, "wb").write(lzma.compress(os.urandom(5000) + b"z" * 50000)),
        "nota.rtf": lambda p: open(p, "wb").write(b"{\\rtf1\\ansi{\\fonttbl{\\f0 Arial;}}\\f0 Hola {\\b mundo}\\par}"),
        "clip.mhl": lambda p: open(p, "wb").write(b'<?xml version="1.0"?>\n<hashlist version="1.1"><hash><file>A.mxf</file><xxhash64be>0011</xxhash64be></hash></hashlist>\n'),
    }
    for name, fn in py.items():
        p = os.path.join(SAMPLES, name)
        if not os.path.exists(p):
            fn(p)
        out[name] = p
    return out


def _odt(p):
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), b"application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", b"<office:document-content/>" * 100, zipfile.ZIP_DEFLATED)


def _sqlite(p):
    c = sqlite3.connect(p)
    c.execute("create table t(a, b)")
    c.executemany("insert into t values (?, ?)", [(i, os.urandom(200)) for i in range(500)])
    c.commit()
    c.close()


def _tar(p):
    with tarfile.open(p, "w", format=tarfile.USTAR_FORMAT) as t:
        for i in range(3):
            data = os.urandom(3000 + i)
            ti = tarfile.TarInfo("f%d.bin" % i)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))


if __name__ == "__main__":
    for k, v in build().items():
        print(k, os.path.getsize(v))
