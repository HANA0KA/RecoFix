"""Scan history: save, list, reload and resume scan sessions.

Recoverit's engine has ``SaveScanRecord*`` / ``LoadScanRecord*`` messages and
asks "Incomplete scan detected. Resume scanning?". RecoFix stores each session
as a gzip-compressed JSON file (``.rfscan``) with every item, its extents and
the byte offset where the deep pass stopped, so a scan of a slow disk never has
to start from zero.
"""
import base64
import gzip
import json
import os
import time

from .fs import Volume
from .scanner import Item, Scan

VERSION = 1


def default_dir():
    home = os.path.expanduser("~" + (os.environ.get("SUDO_USER") or ""))
    d = os.path.join(home, "Library", "Application Support", "RecoFix", "scans")
    os.makedirs(d, exist_ok=True)
    return d


def _item(it):
    d = it.to_dict()
    d["extents"] = it.extents
    if it.resident is not None:
        d["resident"] = base64.b64encode(it.resident).decode()
    return d


def save(scan, path=None):
    st = scan.status()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(scan.started))
    path = path or os.path.join(default_dir(), "%s_%s.rfscan" % (stamp, _slug(scan.label)))
    doc = {
        "version": VERSION, "source": scan.path, "label": scan.label, "mode": scan.mode,
        "started": scan.started, "finished": scan.finished, "deep": scan.deep,
        "complete": st["phase"] == "terminado",
        "resume_offset": getattr(scan, "resume_offset", 0) if st["phase"] != "terminado" else None,
        "categories": sorted(scan.categories) if scan.categories else None,
        "exts": sorted(scan.exts) if scan.exts else None, "scope": scan.scope,
        "volumes": [dict(v.to_dict(), meta=v.meta) for v in scan.volumes],
        "bad_sectors": st["bad_sectors"],
        "items": [_item(it) for it in list(scan.items)],
    }
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, path)
    _chown(path)
    return path


def _chown(path):
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.chown(path, int(uid), int(gid or uid))
        except OSError:
            pass


def _slug(s):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:40] or "scan"


def load(path):
    """Rebuild a finished ``Scan`` from a saved session (no disk access needed)."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        doc = json.load(f)
    sc = Scan(doc["source"], deep=doc.get("deep", True), categories=doc.get("categories"),
              exts=doc.get("exts"), scope=doc.get("scope"), label=doc.get("label", ""), mode=doc.get("mode", "disk"))
    sc.started = doc.get("started") or time.time()
    sc.finished = doc.get("finished") or time.time()
    sc.phase = "terminado" if doc.get("complete") else "incompleto"
    sc.progress = 1.0 if doc.get("complete") else 0
    sc.bad_sectors = doc.get("bad_sectors", 0)
    sc.resume_offset = doc.get("resume_offset") or 0
    sc.volumes = [Volume(v["offset"], v["size"], v["fstype"], v.get("label", ""), v.get("scheme", ""), v.get("meta", {}))
                  for v in doc.get("volumes", [])]
    for d in doc["items"]:
        res = base64.b64decode(d["resident"]) if d.get("resident") else None
        ext = d.get("extents")
        it = Item(id=d["id"], group=d["group"], name=d["name"], path=d["path"], ext=d["ext"],
                  category=d["category"], size=d["size"], start=d["start"],
                  extents=[tuple(e) for e in ext] if ext else None, resident=res,
                  complete=d.get("complete", True), confidence=d.get("confidence", 1.0), fmt=d.get("fmt", ""),
                  meta=d.get("meta") or {}, note=d.get("note", ""), mtime=d.get("mtime", ""), fs=d.get("fs", ""))
        sc.items.append(it)
        if it.start is not None and it.start >= 0:
            sc._starts[it.start] = it
    sc.saved_path = path
    return sc


def resume(path, log=None):
    """A new Scan that continues the deep pass of an incomplete session, keeping what it already found."""
    old = load(path)
    sc = Scan(old.path, deep=True, categories=old.categories, exts=old.exts, scope=old.scope,
              lost_partitions=False, resume_from=old.resume_offset, label=old.label, mode=old.mode, log=log)
    sc.items = old.items
    sc._starts = old._starts
    return sc


def listing(d=None):
    d = d or default_dir()
    out = []
    for n in sorted(os.listdir(d), reverse=True):
        if not n.endswith(".rfscan"):
            continue
        p = os.path.join(d, n)
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            continue
        out.append({"path": p, "label": doc.get("label"), "source": doc.get("source"), "mode": doc.get("mode"),
                    "started": doc.get("started"), "complete": doc.get("complete"), "items": len(doc.get("items", [])),
                    "resume_offset": doc.get("resume_offset"), "size": os.path.getsize(p)})
    return out
