"""Disk images: create (ddrescue-style, bad-sector aware) and open existing ones.

Recoverit offers "Create Image" and, when it meets bad sectors, "Stop this
process and use a disk image for recovery?". Imaging first is the
professional workflow for failing media: the device is read once, mostly
sequentially, and every later scan runs against the copy.

Passes:

1. Fast copy in large blocks. A read error marks the block and *skips ahead*
   (growing the jump while errors continue) instead of hammering a dying area.
2. Retry the skipped areas sector by sector, from the end backwards.

The image is a plain raw file (``.img``) plus ``.img.map.json`` with every
unreadable range, so recovered files can be flagged as damaged.
"""
import json
import os
import plistlib
import subprocess
import threading
import time

from .recover import check_destination
from .source import Source
from .util import chown_user

BLOCK = 4 << 20
IMAGE_EXT = (".img", ".dd", ".raw", ".bin", ".iso", ".001", ".dmg", ".sparseimage", ".sparsebundle", ".vhd", ".e01")


class Imager:
    def __init__(self, source, out_path, start=0, end=None, retry=True):
        self.source = source
        self.out = out_path
        self.start = start
        self.end = end
        self.retry = retry
        self.done = 0
        self.total = 0
        self.bad = []  # [(offset, length)] still unreadable
        self.skipped = []
        self.phase = "preparando"
        self.error = ""
        self.finished = None
        self.started = time.time()
        self.cancel = threading.Event()

    def status(self):
        el = (self.finished or time.time()) - self.started
        sp = self.done / el if el > 0 else 0
        return {"done_bytes": self.done, "total_bytes": self.total, "progress": self.done / self.total if self.total else 0,
                "bad_sectors": sum(n for _, n in self.bad) // 512, "bad_ranges": len(self.bad), "phase": self.phase,
                "speed": sp, "eta": (self.total - self.done) / sp if sp and self.phase == "copiando" else None,
                "error": self.error, "done": self.finished is not None, "out": self.out}

    def run(self):
        try:
            check_destination(self.source, os.path.dirname(self.out) or ".")
            with Source(self.source) as src, open(self.out, "xb") as w:
                end = min(self.end or src.size, src.size)
                self.total = end - self.start
                w.truncate(self.total)
                self.phase = "copiando"
                self._pass1(src, w, end)
                if self.retry and self.skipped and not self.cancel.is_set():
                    self.phase = "reintentando sectores dañados"
                    self._pass2(src, w)
                else:
                    self.bad = list(self.skipped)
            self._write_map()
            chown_user(self.out)
            self.phase = "cancelado" if self.cancel.is_set() else "terminado"
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
            self.phase = "error"
        finally:
            self.finished = time.time()

    def _pass1(self, src, w, end):
        pos, jump = self.start, BLOCK
        fd = src.fd
        while pos < end and not self.cancel.is_set():
            n = min(BLOCK, end - pos)
            try:
                b = os.pread(fd, n, pos)
                if not b:
                    break
                w.seek(pos - self.start)
                w.write(b)
                pos += len(b)
                jump = BLOCK
            except OSError:
                skip = min(jump, end - pos)
                self.skipped.append((pos, skip))
                pos += skip
                jump = min(jump * 2, 256 << 20)  # dying region: back off quickly
            self.done = pos - self.start

    def _pass2(self, src, w):
        ss = src.sector_size
        for off, ln in reversed(self.skipped):
            run_start = None
            for s in range(off + ln - ss, off - ss, -ss):
                if self.cancel.is_set():
                    return
                try:
                    b = os.pread(src.fd, ss, s)
                    w.seek(s - self.start)
                    w.write(b)
                    if run_start is not None:
                        self.bad.append((s + ss, run_start - s - ss))
                        run_start = None
                except OSError:
                    if run_start is None:
                        run_start = s + ss
            if run_start is not None:
                self.bad.append((off, run_start - off))
        self.bad.sort()

    def _write_map(self):
        with open(self.out + ".map.json", "w") as f:
            json.dump({"origen": self.source, "inicio": self.start, "bytes": self.total, "sector": 512,
                       "fecha": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "sectores_ilegibles": [{"offset": o, "len": n} for o, n in self.bad]}, f, indent=1)
        chown_user(self.out + ".map.json")


def open_image(path):
    """Path the scanner can read for an image file: raw images directly, DMG & co. attached read-only."""
    low = path.lower()
    if low.endswith((".dmg", ".sparseimage", ".sparsebundle")):
        out = subprocess.run(["hdiutil", "attach", "-readonly", "-nomount", "-plist", path],
                             capture_output=True, timeout=120)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode("utf-8", "replace").strip() or "hdiutil no pudo abrir la imagen")
        ents = plistlib.loads(out.stdout).get("system-entities", [])
        whole = sorted((e["dev-entry"] for e in ents if "dev-entry" in e), key=len)
        if not whole:
            raise RuntimeError("la imagen no expone ningún dispositivo")
        return {"path": "/dev/r" + whole[0][5:], "attached": whole[0], "kind": "dmg"}
    if low.endswith(".e01"):
        raise RuntimeError("Las imágenes EnCase (.E01) no están soportadas: conviértelas a raw con ewfexport")
    return {"path": path, "attached": None, "kind": "raw"}


def detach(dev):
    subprocess.run(["hdiutil", "detach", "-quiet", dev], capture_output=True, timeout=60)
