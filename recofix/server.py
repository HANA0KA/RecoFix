"""Local HTTP API + web UI. Binds to 127.0.0.1 only and requires a per-launch token.

The API mirrors the engine service Recoverit's UI talks to over gRPC
(``EnumDevices``, ``Scan``, ``RequestScanData``, ``Preview``, ``Recover``,
``EngineNotify``): every long operation is a *job* polled for status.
"""
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import devices, formats, imaging, mhl, reassembly, remote, rescue, sessions, smart
from .modes import camera, locations, timemachine, trash
from .recover import Recovery, check_destination
from .repair import document, photo, video
from .scanner import Scan, quick_info
from .source import Source
from .util import chown_user, home, tool, unique

WEB = os.path.join(os.path.dirname(__file__), "web")
FFMPEG = tool("ffmpeg")
JOBS = {}
LOG = []
HTTPD = None
VERSION = "1.0.0"


def log(msg):
    LOG.append("%s %s" % (time.strftime("%H:%M:%S"), msg))
    del LOG[:-1000]


# ---------------------------------------------------------------- jobs

class FuncJob:
    """Run ``fn(job)`` in a thread; ``fn`` may update ``job.progress`` / ``job.phase`` and check ``job.cancel``."""

    def __init__(self, kind, fn, label=""):
        self.kind, self.fn, self.label = kind, fn, label
        self.progress = 0.0
        self.phase = "en cola"
        self.result = None
        self.results = []
        self.error = ""
        self.started = time.time()
        self.finished = None
        self.cancel = threading.Event()

    def run(self):
        self.phase = "en curso"
        try:
            self.result = self.fn(self)
            self.phase = "cancelado" if self.cancel.is_set() else "terminado"
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e) if not isinstance(e, (ValueError, RuntimeError)) else str(e)
            self.phase = "error"
            log("%s: %s" % (self.kind, traceback.format_exc()))
        finally:
            self.progress = 1.0 if not self.error else self.progress
            self.finished = time.time()

    def status(self):
        return {"kind": self.kind, "label": self.label, "phase": self.phase, "progress": round(self.progress, 4),
                "result": self.result, "results": self.results[-500:], "error": self.error,
                "done": self.finished is not None, "elapsed": (self.finished or time.time()) - self.started}


def start(obj, kind):
    jid = secrets.token_hex(4)
    obj.kind = getattr(obj, "kind", kind) or kind
    JOBS[jid] = obj
    threading.Thread(target=obj.run, daemon=True).start()
    return jid


def job_status(jid):
    j = JOBS[jid]
    st = j.status()
    st["id"] = jid
    st["kind"] = getattr(j, "kind", "")
    return st


def jobs_overview():
    out = []
    for jid, j in list(JOBS.items())[-50:]:
        st = j.status()
        out.append({"id": jid, "kind": getattr(j, "kind", ""), "label": getattr(j, "label", ""),
                    "phase": st.get("phase"), "progress": st.get("progress"), "done": st.get("done"),
                    "error": st.get("error")})
    return out


# ---------------------------------------------------------------- scan results

def _item_filter(q):
    group, cat = q.get("group"), q.get("category")
    text = (q.get("q") or "").lower().strip()
    exts = {e.strip().lower().lstrip(".") for e in (q.get("ext") or "").split(",") if e.strip()}
    smin, smax = int(q.get("size_min") or 0), int(q.get("size_max") or 0)
    dfrom, dto = q.get("date_from") or "", q.get("date_to") or ""
    folder = q.get("folder")
    show_system = q.get("system") == "1"
    complete = q.get("complete") == "1"
    status = q.get("status")  # deleted | existing

    def ok(it):
        if group and it.group != group:
            return False
        if status == "deleted" and it.group == "existing" or status == "existing" and it.group != "existing":
            return False
        if cat and it.category != cat:
            return False
        if not show_system and it.category == "system":
            return False
        if complete and not it.complete:
            return False
        if exts and it.ext not in exts:
            return False
        if smin and it.size < smin or smax and it.size > smax:
            return False
        if (dfrom or dto) and (not it.mtime or dfrom and it.mtime[:10] < dfrom or dto and it.mtime[:10] > dto):
            return False
        if folder is not None:
            p = _tree_path(it)
            if not (p == folder or p.startswith(folder.rstrip("/") + "/")):
                return False
        if text and text not in (it.path or it.name).lower() and text != it.ext:
            return False
        return True
    return ok


def _tree_path(it):
    """Folder an item lives in for the tree view ("Ruta" in Recoverit)."""
    if it.group == "raw" or not it.path:
        return "/Sin nombre (RAW)/" + it.category
    return it.path.rsplit("/", 1)[0] or "/"


def filtered(scan, q):
    ok = _item_filter(q)
    return [it for it in list(scan.items) if ok(it)]


def facets(scan, q):
    """Counts for the sidebar and Recoverit-style filter buckets (dr.FilterStatisticsInfo)."""
    base = dict(q)
    for k in ("group", "category", "status"):
        base.pop(k, None)
    ok = _item_filter(base)
    g, c, sizes, ages = {}, {}, {"<1MB": 0, "1-100MB": 0, "100MB-1GB": 0, ">1GB": 0}, {}
    for it in list(scan.items):
        if not ok(it):
            continue
        g[it.group] = g.get(it.group, 0) + 1
        c[it.category] = c.get(it.category, 0) + 1
        s = it.size
        sizes["<1MB" if s < 1 << 20 else "1-100MB" if s < 100 << 20 else "100MB-1GB" if s < 1 << 30 else ">1GB"] += 1
        if it.mtime:
            ages[it.mtime[:4]] = ages.get(it.mtime[:4], 0) + 1
    return {"groups": g, "categories": c, "sizes": sizes, "years": dict(sorted(ages.items()))}


def tree(scan, folder):
    """Immediate subfolders of ``folder`` with recursive counts."""
    folder = folder.rstrip("/") or ""
    subs = {}
    for it in list(scan.items):
        if it.category == "system":
            continue
        p = _tree_path(it)
        if folder and not (p == folder or p.startswith(folder + "/")):
            continue
        rest = p[len(folder):].strip("/")
        if not rest:
            continue
        name = rest.split("/", 1)[0]
        d = subs.setdefault(name, {"name": name, "path": folder + "/" + name, "count": 0, "size": 0, "deleted": 0})
        d["count"] += 1
        d["size"] += max(0, it.size)
        if it.group != "existing":
            d["deleted"] += 1
    return sorted(subs.values(), key=lambda d: (d["name"].startswith("Sin nombre"), d["name"].lower()))


PREVIEW_DIRECT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
                  "bmp": "image/bmp", "webp": "image/webp"}
TEXTLIKE = {"txt", "csv", "xml", "json", "mhl", "md", "log", "srt", "rtf", "html", "htm", "ini", "yaml", "yml"}


def _head(src, it, n):
    if it.resident is not None:
        return it.resident[:n]
    out, left = bytearray(), min(n, it.size)
    for off, ln in it.extents or [(it.start, it.size)]:
        if left <= 0:
            break
        k = min(ln, left)
        out += b"\0" * k if off is None else src.read(off, k)
        left -= k
    return bytes(out)


def preview(scan, it):
    """(content_type, bytes) for the preview pane, or None."""
    with Source(scan.path) as src:
        if it.ext in PREVIEW_DIRECT and it.size <= 40 << 20:
            return PREVIEW_DIRECT[it.ext], _head(src, it, it.size)
        if it.ext in TEXTLIKE or it.category == "document" and it.ext in ("txt",):
            return "text/plain; charset=utf-8", _head(src, it, 64 << 10).decode("utf-8", "replace").encode()
        if not FFMPEG or it.category not in ("video", "photo"):
            return None
        tmp = tempfile.mkdtemp(prefix="recofix_prev_")
        try:
            p = os.path.join(tmp, "in." + (it.ext or "bin"))
            with open(p, "wb") as f:
                f.write(_head(src, it, 96 << 20))
            out = os.path.join(tmp, "t.jpg")
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", p, "-frames:v", "1", "-vf", "scale=640:-2", out],
                           capture_output=True, timeout=60)
            if os.path.exists(out):
                with open(out, "rb") as f:
                    return "image/jpeg", f.read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return None


# ---------------------------------------------------------------- handlers for long tasks

def repair_job(b):
    kind, files, mode = b["kind"], b["files"], b.get("mode", "quick")
    sample, dest = b.get("sample") or None, b.get("dest") or None

    def fn(job):
        for i, path in enumerate(files):
            if job.cancel.is_set():
                break
            job.phase = "reparando %s" % os.path.basename(path)
            try:
                if kind == "video":
                    if mode == "advanced":
                        if not sample:
                            raise ValueError("la reparación avanzada necesita un vídeo de muestra")
                        r = video.advanced_repair(path, sample, dest)
                    else:
                        r = video.quick_repair(path, dest)
                elif kind == "photo":
                    r = photo.repair(path, sample, dest)
                elif kind == "document":
                    r = document.repair(path, dest)
                else:
                    raise ValueError("tipo de reparación desconocido")
            except Exception as e:
                r = {"ok": False, "error": str(e)}
            r["file"] = path
            job.results.append(r)
            job.progress = (i + 1) / len(files)
        return {"ok": sum(1 for r in job.results if r.get("ok")), "total": len(files)}
    return FuncJob("repair", fn, "Reparación de %s" % {"video": "vídeo", "photo": "fotos", "document": "documentos"}.get(kind, kind))


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    token = ""
    server_version = "RecoFix/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; style-src 'self' "
                                                    "'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send(403, {"error": "host no permitido"})
            return False
        if self.path.startswith("/api/"):
            tok = self.headers.get("X-Token") or urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("t", [""])[0]
            if not secrets.compare_digest(tok, self.token):
                self._send(401, {"error": "token inválido"})
                return False
            if self.command == "POST" and (self.headers.get("Origin") or "").split("//")[-1].rsplit(":", 1)[0] not in (
                    "", "127.0.0.1", "localhost"):
                self._send(403, {"error": "origen no permitido"})
                return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # ---------------------------------------------------------------- GET

    def do_GET(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = [x for x in u.path.split("/") if x]
        try:
            if not p or p == ["index.html"]:
                return self._static("index.html")
            if p[0] == "static":
                return self._static("/".join(p[1:]))
            if p[0] != "api":
                return self._send(404, {"error": "no encontrado"})
            r = self._get(p[1:], q)
            if r is not None:
                return r if r is True else self._send(200, r)
            self._send(404, {"error": "no encontrado"})
        except KeyError as e:
            self._send(404, {"error": "no encontrado: %s" % e})
        except Exception as e:
            log("GET %s: %s" % (self.path, traceback.format_exc()))
            self._send(500, {"error": str(e)})

    def _get(self, p, q):
        if p == ["env"]:
            return {"version": VERSION, "root": os.geteuid() == 0, "ffmpeg": bool(FFMPEG), "smartctl": bool(tool("smartctl")),
                    "xxhsum": bool(tool("xxhsum")), "home": home(), "user": os.environ.get("SUDO_USER") or os.environ.get("USER")}
        if p == ["devices"]:
            return _devices_with_profiles()
        if p == ["formats"]:
            return formats.supported()
        if p == ["locations"]:
            return locations.quick_locations()
        if p == ["log"]:
            return LOG[-300:]
        if p == ["jobs"]:
            return jobs_overview()
        if p == ["ls"]:
            return _ls(q.get("path") or "/Volumes", q.get("show", "all"))
        if p == ["trash"]:
            return trash.list_items()
        if p == ["tm"]:
            return timemachine.backups()
        if p == ["tm", "ls"]:
            return timemachine.browse(q["path"])
        if p == ["sessions"]:
            return sessions.listing()
        if p == ["smart"]:
            return smart.all_disks(devices.list_devices())
        if p[:1] == ["job"] and len(p) == 2:
            return job_status(p[1])
        if p[:1] == ["scan"] and len(p) >= 2:
            scan = JOBS[p[1]]
            if len(p) == 2:
                st = scan.status()
                st["saved_path"] = getattr(scan, "saved_path", "")
                return st
            if p[2] == "items":
                items = filtered(scan, q)
                key = q.get("sort", "")
                if key in ("size", "name", "start", "mtime", "ext"):
                    items.sort(key=lambda it: (it.name.lower() if key == "name" else getattr(it, key) or 0)
                               if key != "mtime" else (it.mtime or ""), reverse=q.get("desc") == "1")
                off, lim = int(q.get("offset", 0)), min(1000, int(q.get("limit", 200)))
                return {"total": len(items), "total_bytes": sum(max(0, i.size) for i in items),
                        "items": [i.to_dict() for i in items[off:off + lim]], "facets": facets(scan, q)}
            if p[2] == "tree":
                return tree(scan, q.get("folder", ""))
            if p[2] == "preview" and len(p) == 4:
                it = scan.items[int(p[3])]
                pv = preview(scan, it)
                if pv:
                    self._send(200, pv[1], pv[0])
                else:
                    self._send(404, {"error": "sin vista previa"})
                return True
        return None

    # ---------------------------------------------------------------- POST

    def do_POST(self):
        if not self._guard():
            return
        p = [x for x in urllib.parse.urlparse(self.path).path.split("/") if x]
        try:
            b = self._body()
            r = self._post(p[1:] if p[:1] == ["api"] else [], b)
            if r is None:
                return self._send(404, {"error": "no encontrado"})
            self._send(200, r)
        except KeyError as e:
            self._send(400, {"error": "falta o no existe: %s" % e})
        except Exception as e:
            log("POST %s: %s" % (self.path, traceback.format_exc()))
            self._send(400, {"error": str(e)})

    def _post(self, p, b):
        if p == ["source"]:
            src = imaging.open_image(b["path"]) if os.path.isfile(b["path"]) else {"path": b["path"]}
            info = quick_info(src["path"])
            info["path"] = src["path"]
            return info
        if p == ["locations", "resolve"]:
            return locations.resolve_folder(b["path"])
        if p == ["scan"]:
            path = b["path"]
            if os.path.isfile(path):
                path = imaging.open_image(path)["path"]
            scan = Scan(path, deep=b.get("deep", True), categories=b.get("categories") or None,
                        exts=b.get("exts") or None, scope=b.get("scope") or None, volume_index=b.get("volume"),
                        lost_partitions=b.get("lost_partitions", True), label=b.get("label") or "",
                        mode=b.get("mode") or "disk", log=log)
            jid = start(scan, "scan")
            log("escaneo %s (%s) de %s" % (jid, scan.mode, path))
            return {"id": jid}
        if p[:1] == ["scan"] and len(p) == 3:
            scan = JOBS[p[1]]
            if p[2] == "pause":
                scan.paused.set()
            elif p[2] == "resume":
                scan.paused.clear()
            elif p[2] == "cancel":
                scan.paused.clear()
                scan.cancel.set()
            elif p[2] == "save":
                scan.saved_path = sessions.save(scan, b.get("path"))
                return {"ok": True, "path": scan.saved_path}
            else:
                return None
            return {"ok": True}
        if p == ["sessions", "load"]:
            sc = sessions.load(b["path"])
            jid = secrets.token_hex(4)
            sc.kind = "scan"
            JOBS[jid] = sc
            return {"id": jid}
        if p == ["sessions", "resume"]:
            sc = sessions.resume(b["path"], log=log)
            return {"id": start(sc, "scan")}
        if p == ["sessions", "delete"]:
            if os.path.dirname(os.path.realpath(b["path"])) == os.path.realpath(sessions.default_dir()):
                os.remove(b["path"])
            return {"ok": True}
        if p == ["recover"]:
            scan = JOBS[b["scan"]]
            items = [scan.items[i] for i in b["ids"]] if b.get("ids") is not None else filtered(scan, b.get("filter") or {})
            check_destination(scan.path, b["dest"])
            rec = Recovery(scan.path, items, b["dest"], finalize_mxf=b.get("finalize_mxf", True),
                           keep_structure=b.get("keep_structure", True), log=log)
            rec.label = "Recuperar %d archivos" % len(items)
            return {"id": start(rec, "recover")}
        if p[:1] == ["job"] and len(p) == 3 and p[2] == "cancel":
            JOBS[p[1]].cancel.set()
            return {"ok": True}
        if p == ["trash", "restore"]:
            return trash.restore(b["paths"], b.get("dest"), b.get("to_original", False))
        if p == ["tm", "mount"]:
            return {"path": timemachine.mount_snapshot(b["name"], b.get("volume", "/"))}
        if p == ["tm", "search"]:
            root, text = b["root"], b["text"]
            return {"id": start(FuncJob("tm_search", lambda j: timemachine.search(root, text, cancel=j.cancel),
                                        "Buscar «%s» en Time Machine" % text), "tm_search")}
        if p == ["tm", "restore"]:
            return timemachine.restore(b["paths"], b["dest"])
        if p == ["repair", "probe"]:
            return video.probe(b["path"])
        if p == ["repair"]:
            return {"id": start(repair_job(b), "repair")}
        if p == ["reassemble"]:
            scan = JOBS[b["scan"]]
            it = scan.items[int(b["item"])]
            check_destination(scan.path, b["dest"])
            out = unique(os.path.join(b["dest"], os.path.splitext(it.name)[0] + "_reensamblado." + (it.ext or "mp4")))

            def fn(job):
                r = reassembly.reassemble(scan.path, it.start, out, cluster=int(b.get("cluster") or 32768),
                                          progress=lambda x: setattr(job, "progress", x), cancel=job.cancel)
                chown_user(out)
                r["check"] = video.probe(out)
                return r
            return {"id": start(FuncJob("reassemble", fn, "Reensamblar %s" % it.name), "reassemble")}
        if p == ["image"]:
            im = imaging.Imager(b["path"], b["out"])
            im.label = "Imagen de %s" % os.path.basename(b["path"])
            return {"id": start(im, "image")}
        if p == ["image", "open"]:
            return imaging.open_image(b["path"])
        if p == ["remote", "discover"]:
            return remote.discover(float(b.get("timeout", 4)))
        if p[:1] == ["remote"]:
            ssh = remote.SSH(b["host"], b["user"], b.get("password"), b.get("port", 22), b.get("key") or None)
            if p == ["remote", "test"]:
                return ssh.test()
            if p == ["remote", "devices"]:
                return ssh.devices()
            if p == ["remote", "image"]:
                ri = remote.RemoteImager(ssh, b["device"], b["out"], int(b.get("size") or 0))
                ri.label = "Copia de %s:%s" % (b["host"], b["device"])
                return {"id": start(ri, "remote_image")}
        if p == ["rescue"]:
            return rescue.make_kit(b["volume"])
        if p == ["mhl"]:
            mhls, files = [], []
            for root, _, names in os.walk(b["dir"]):
                for n in names:
                    (mhls if n.lower().endswith(".mhl") else files).append(os.path.join(root, n))
            if b.get("mhl"):
                mhls = [b["mhl"]]
            return {"mhl": mhls, "rows": mhl.verify(mhls, files)}
        if p == ["reveal"]:
            path = b["path"]
            if os.path.exists(path):
                user = os.environ.get("SUDO_USER")
                cmd = ["open", "-R", path]
                subprocess.Popen(["sudo", "-u", user] + cmd if user and os.geteuid() == 0 else cmd)
            return {"ok": True}
        if p == ["quit"]:
            threading.Thread(target=lambda: (time.sleep(0.2), HTTPD and HTTPD.shutdown()), daemon=True).start()
            return {"ok": True}
        if p == ["mkdir"]:
            os.makedirs(b["path"], exist_ok=True)
            chown_user(b["path"])
            return {"ok": True, "path": b["path"]}
        return None

    def _static(self, rel):
        path = os.path.realpath(os.path.join(WEB, rel))
        if not path.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(path):
            return self._send(404, {"error": "no encontrado"})
        with open(path, "rb") as f:
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/javascript":
                ctype += "; charset=utf-8"
            self._send(200, f.read(), ctype)


def _devices_with_profiles():
    ds = devices.list_devices()
    for d in ds:
        d["profile"] = None
        if not d.get("internal") and not d.get("is_image"):
            for part in d.get("partitions", []):
                if part.get("mount"):
                    prof = camera.detect(part["mount"])
                    if prof:
                        prof["mount"], prof["partition"] = part["mount"], part["path"]
                        d["profile"] = prof
                        break
    return ds


MEDIA_EXT = (".mxf", ".mov", ".mp4", ".m4v", ".mts", ".m2ts", ".avi", ".mkv", ".3gp", ".insv", ".crm", ".jpg",
             ".jpeg", ".png", ".heic", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf", ".pef", ".tif",
             ".tiff", ".gif", ".bmp", ".webp", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pages",
             ".numbers", ".key", ".odt", ".ods", ".odp", ".zip", ".txt", ".csv", ".rtf", ".sqlite", ".db", ".epub",
             ".mhl", ".json", ".xml")


def _ls(path, show):
    path = os.path.realpath(os.path.expanduser(path))
    names = sorted(os.listdir(path), key=str.lower)
    entries = []
    for n in names:
        if n.startswith("."):
            continue
        full = os.path.join(path, n)
        isdir = os.path.isdir(full)
        low = n.lower()
        if isdir or show == "all" or show == "images" and low.endswith(imaging.IMAGE_EXT) or \
                show == "media" and low.endswith(MEDIA_EXT):
            try:
                size = 0 if isdir else os.path.getsize(full)
            except OSError:
                size = 0
            entries.append({"name": n, "path": full, "dir": isdir, "size": size})
    st = shutil.disk_usage(path)
    return {"path": path, "parent": os.path.dirname(path), "entries": entries, "free": st.free, "home": home()}


def serve(port=8765, open_browser=True, token=None):
    global HTTPD
    Handler.token = token or os.environ.get("RECOFIX_TOKEN") or secrets.token_urlsafe(16)
    httpd = HTTPD = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/?t=%s" % (port, Handler.token)
    print("RecoFix escuchando en " + url, flush=True)
    if open_browser:
        subprocess.Popen(["open", url])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        timemachine.unmount_all()
