"""Write recovered files, finalize MXF copies, hash them, and log a chain-of-custody report."""
import csv
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import threading
import time

from .formats import mxf
from .source import Source

COPY = 8 << 20


class UnsafeDestination(Exception):
    pass


def whole_disk_of(path):
    """BSD whole-disk name (disk5) that holds ``path`` (device node or mounted path)."""
    p = os.path.realpath(path)
    target = "/dev/" + p[6:] if p.startswith("/dev/rdisk") else p
    if not target.startswith("/dev/"):
        while not os.path.exists(target):
            target = os.path.dirname(target)
        try:
            target = subprocess.run(["df", "-P", target], capture_output=True, text=True,
                                    timeout=20).stdout.splitlines()[1].split()[0]
        except Exception:
            return None
    try:
        out = subprocess.run(["diskutil", "info", "-plist", target], capture_output=True, timeout=20).stdout
        info = plistlib.loads(out)
    except Exception:
        return None
    disk = info.get("ParentWholeDisk") or info.get("DeviceIdentifier")
    # APFS volumes live on a synthesized container disk; follow it to the physical store
    store = info.get("APFSPhysicalStores") or []
    if store:
        try:
            s = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", store[0]["APFSPhysicalStore"]],
                                              capture_output=True, timeout=20).stdout)
            disk = s.get("ParentWholeDisk") or disk
        except Exception:
            pass
    return disk


def check_destination(source, dest):
    """Refuse to write onto the medium being recovered: that overwrites the data we want back."""
    dest = os.path.realpath(dest)
    if source.startswith("/dev/"):
        a = whole_disk_of(source)
        b = whole_disk_of(dest)
        if a and b and a == b:
            raise UnsafeDestination("El destino está en el mismo disco que se recupera (%s). "
                                    "Elige otro disco para no sobrescribir los datos." % a)
    else:
        src_dev = os.stat(source).st_dev
        if os.path.isdir(dest) and os.stat(dest).st_dev == src_dev:
            pass  # recovering from an image file onto the same volume is fine
    return True


def _unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)


def _safe(name):
    name = name.replace("/", "_").replace("\0", "").strip() or "sin_nombre"
    return name[:200]


def _chown(path):
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and os.geteuid() == 0:
        try:
            os.chown(path, int(uid), int(gid or uid))
        except OSError:
            pass


def xxh64_file(path):
    exe = shutil.which("xxhsum") or ("/opt/homebrew/bin/xxhsum" if os.path.exists("/opt/homebrew/bin/xxhsum") else None)
    if not exe:
        return None
    out = subprocess.run([exe, "-H1", path], capture_output=True, text=True).stdout.split()
    return out[0] if out else None


class Recovery:
    def __init__(self, source, items, dest, finalize_mxf=True, hashes=True, keep_structure=True, log=None):
        self.source = source
        self.items = items
        self.dest = dest
        self.finalize_mxf = finalize_mxf
        self.hashes = hashes
        self.keep_structure = keep_structure
        self.total = sum(max(0, it.size) for it in items)
        self.done = 0
        self.current = ""
        self.results = []
        self.error = ""
        self.finished = None
        self.started = time.time()
        self.cancel = threading.Event()
        self.log = log or (lambda m: None)

    def status(self):
        el = (self.finished or time.time()) - self.started
        return {"done_bytes": self.done, "total_bytes": self.total, "current": self.current,
                "progress": self.done / self.total if self.total else 1.0, "results": self.results[-200:],
                "count": len(self.results), "error": self.error, "done": self.finished is not None,
                "speed": self.done / el if el > 0 else 0, "dest": self.dest}

    def run(self):
        try:
            check_destination(self.source, self.dest)
            os.makedirs(self.dest, exist_ok=True)
            with Source(self.source) as src:
                for it in self.items:
                    if self.cancel.is_set():
                        break
                    self.results.append(self._one(src, it))
            self._report()
        except UnsafeDestination as e:
            self.error = str(e)
        except Exception as e:
            import traceback
            self.error = "%s: %s" % (type(e).__name__, e)
            self.log(traceback.format_exc())
        finally:
            self.finished = time.time()

    def _target(self, it):
        group = {"existing": "Existentes", "deleted": "Borrados", "orphan": "Huerfanos", "raw": "Sin_nombre_RAW"}[it.group]
        if it.path and self.keep_structure:
            parts = [_safe(p) for p in it.path.strip("/").split("/")]
            path = os.path.join(self.dest, group, *parts)
        else:
            path = os.path.join(self.dest, group, it.category, _safe(it.name))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return _unique(path)

    def _one(self, src, it):
        out = self._target(it)
        self.current = out
        res = {"id": it.id, "name": it.name, "path": out, "size": it.size, "source_offset": it.start,
               "group": it.group, "ok": False, "note": ""}
        md5 = hashlib.md5() if self.hashes else None
        written = 0
        try:
            with open(out, "xb") as w:
                if it.resident is not None:
                    w.write(it.resident[:it.size])
                    md5 and md5.update(it.resident[:it.size])
                    written = len(it.resident[:it.size])
                    self.done += written
                else:
                    extents = it.extents or [(it.start, it.size)]
                    for off, ln in extents:
                        left = ln
                        while left > 0:
                            if self.cancel.is_set():
                                raise InterruptedError("cancelado")
                            n = min(COPY, left)
                            b = b"\0" * n if off is None else src.read(off + (ln - left), n)
                            if not b:
                                raise IOError("lectura corta en offset %d" % (off + ln - left))
                            w.write(b)
                            md5 and md5.update(b)
                            left -= len(b)
                            written += len(b)
                            self.done += len(b)
            _chown(out)
            res["ok"] = written == it.size
            res["written"] = written
            if md5:
                res["md5"] = md5.hexdigest()
                x = xxh64_file(out)
                if x:
                    res["xxh64"] = x
            if it.ext == "mxf" and self.finalize_mxf and mxf.needs_finalize(out):
                fixed = _unique(out[:-4] + "_reparado.mxf")
                shutil.copyfile(out, fixed)
                rep = mxf.finalize(fixed)
                _chown(fixed)
                res["mxf_reparado"] = fixed
                res["note"] = "MXF sin footer: copia reparada creada (datos válidos %d bytes)" % rep.get("datos_validos", 0)
            if src.bad_ranges:
                res["note"] = (res["note"] + "; " if res["note"] else "") + \
                    "%d sectores ilegibles rellenados con ceros en el medio" % len(src.bad_ranges)
        except InterruptedError:
            res["note"] = "cancelado"
        except Exception as e:
            res["note"] = "%s: %s" % (type(e).__name__, e)
        return res

    def _report(self):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        jpath = os.path.join(self.dest, "informe_recuperacion_%s.json" % stamp)
        with open(jpath, "w") as f:
            json.dump({"origen": self.source, "fecha": stamp, "archivos": self.results}, f, indent=2, ensure_ascii=False)
        cpath = os.path.join(self.dest, "informe_recuperacion_%s.csv" % stamp)
        with open(cpath, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ok", "nombre", "ruta", "bytes", "offset_origen", "md5", "xxh64", "nota"])
            for r in self.results:
                w.writerow([r["ok"], r["name"], r["path"], r["size"], r["source_offset"], r.get("md5", ""),
                            r.get("xxh64", ""), r["note"]])
        _chown(jpath)
        _chown(cpath)
        self.report = jpath
