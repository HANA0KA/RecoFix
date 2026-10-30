"""Verify recovered files against Media Hash Lists (legacy MHL 1.x and ASC MHL).

On-set offloads write .mhl files next to the camera media; when a card is
recovered they are the ground truth for whether a clip came back intact.
"""
import hashlib
import os
import xml.etree.ElementTree as ET

from .recover import xxh64_file

ALGOS = ("xxhash64be", "xxhash64", "xxh64", "xxh3", "md5", "sha1", "c4")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse(path_or_bytes):
    """Return [{"file": basename, "size": int|None, "algo": str, "hash": str}]."""
    root = ET.fromstring(path_or_bytes) if isinstance(path_or_bytes, bytes) else ET.parse(path_or_bytes).getroot()
    out = []
    for h in root.iter():
        if _local(h.tag) != "hash":
            continue
        rec = {"file": None, "size": None, "algo": None, "hash": None}
        for c in h:
            t = _local(c.tag)
            if t in ("file", "path"):
                rec["file"] = os.path.basename((c.text or "").strip())
                if c.get("size"):
                    rec["size"] = int(c.get("size"))
            elif t == "size":
                rec["size"] = int((c.text or "0").strip())
            elif t in ALGOS and rec["hash"] is None:
                rec["algo"], rec["hash"] = t, (c.text or "").strip().lower()
        if rec["file"] and rec["hash"]:
            out.append(rec)
    return out


def file_hash(path, algo):
    if algo in ("xxhash64be", "xxhash64", "xxh64"):
        return xxh64_file(path)
    if algo in ("md5", "sha1"):
        h = hashlib.new(algo)
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(8 << 20), b""):
                h.update(b)
        return h.hexdigest()
    return None


def verify(mhl_paths, recovered):
    """``recovered`` is a list of file paths. Returns one row per MHL entry."""
    by_name = {}
    for p in recovered:
        by_name.setdefault(os.path.basename(p), []).append(p)
    rows = []
    for m in mhl_paths:
        for rec in parse(m):
            cands = by_name.get(rec["file"], [])
            row = dict(rec, mhl=os.path.basename(m), recovered=None, status="no recuperado")
            for c in cands:
                if rec["size"] is not None and os.path.getsize(c) != rec["size"]:
                    row.update(recovered=c, status="tamaño distinto")
                    continue
                got = file_hash(c, rec["algo"])
                if got is None:
                    row.update(recovered=c, status="algoritmo %s no disponible" % rec["algo"])
                elif got.lower() == rec["hash"]:
                    row.update(recovered=c, status="OK", got=got)
                    break
                else:
                    row.update(recovered=c, status="hash distinto", got=got)
            rows.append(row)
    return rows
