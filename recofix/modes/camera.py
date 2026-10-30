"""SD card / camera media detection and per-brand scan profiles.

Recoverit shows "<Brand> Camera Scan Profile Ready" after it "Detects DCIM and
PRIVATE camera folders" (SDCardInfoRequset -> make/model). RecoFix reads the
folder layout each camera family writes, plus EXIF/XML sidecars when present,
and turns it into a scan profile: which formats to look for so the deep scan
spends its time on the files that card actually held.
"""
import os
import re

from ..formats.images import Tiff, exif_meta
from ..source import Source

# (brand, folder test, formats it records)
LAYOUTS = [
    ("Sony", lambda r: _has(r, "PRIVATE/M4ROOT"), ["mp4", "xml", "jpg", "arw"], "XAVC S / XAVC HS"),
    ("Sony", lambda r: _has(r, "XDROOT"), ["mxf", "xml"], "XDCAM"),
    ("Sony", lambda r: _has(r, "PRIVATE/AVCHD"), ["mts", "m2ts"], "AVCHD"),
    ("Canon", lambda r: _has(r, "CONTENTS/CLIPS001"), ["mxf", "mp4", "xml"], "XF-AVC / XF-HEVC"),
    ("Canon", lambda r: _dcim(r, r"\d{3}CANON|\d{3}EOS\w*"), ["cr3", "cr2", "jpg", "mp4", "mov", "crm"], "EOS"),
    ("Panasonic", lambda r: _has(r, "CONTENTS/VIDEO"), ["mxf"], "P2"),
    ("Panasonic", lambda r: _dcim(r, r"\d{3}_PANA"), ["rw2", "jpg", "mp4", "mov"], "LUMIX"),
    ("Nikon", lambda r: _dcim(r, r"\d{3}NIKON|\d{3}NZ\w*|\d{3}NCZ\w*"), ["nef", "nrw", "jpg", "mov", "mp4"], "Nikon"),
    ("Fujifilm", lambda r: _dcim(r, r"\d{3}_FUJI"), ["raf", "jpg", "mov", "mp4"], "X / GFX"),
    ("Olympus", lambda r: _dcim(r, r"\d{3}OLYMP|\d{3}OMSYS"), ["orf", "jpg", "mov", "mp4"], "OM System"),
    ("Leica", lambda r: _dcim(r, r"\d{3}LEICA"), ["dng", "jpg", "mov", "mp4"], "Leica"),
    ("Ricoh/Pentax", lambda r: _dcim(r, r"\d{3}RICOH|\d{3}_\w*PENTX|\d{3}PENTX"), ["dng", "pef", "jpg", "mov"], "Ricoh/Pentax"),
    ("GoPro", lambda r: _dcim(r, r"\d{3}GOPRO"), ["mp4", "lrv", "thm", "jpg", "gpr"], "HERO"),
    ("DJI", lambda r: _dcim(r, r"DJI_\d+|\d{3}MEDIA") or _has(r, "DCIM/DJI_001"), ["mp4", "mov", "dng", "jpg", "lrf"], "Drone / Osmo"),
    ("Insta360", lambda r: _dcim(r, r"Camera\d+") or _glob(r, "DCIM", (".insv", ".insp")), ["insv", "insp", "mp4", "dng"], "Insta360"),
    ("ARRI", lambda r: _glob_deep(r, ".mxf", "ARRI") or _glob_deep(r, ".ari"), ["mxf", "ari", "mhl"], "ALEXA"),
    ("RED", lambda r: _glob_deep(r, ".RDC") or _glob_deep(r, ".r3d"), ["r3d"], "R3D"),
    ("Blackmagic", lambda r: _glob(r, "", (".braw",)), ["braw", "mov"], "BRAW"),
]


def _has(root, rel):
    return os.path.isdir(os.path.join(root, rel))


def _dcim(root, pattern):
    try:
        return any(re.fullmatch(pattern, n, re.I) for n in os.listdir(os.path.join(root, "DCIM")))
    except OSError:
        return False


def _glob(root, rel, exts):
    base = os.path.join(root, rel)
    for dirpath, dirs, files in os.walk(base):
        if dirpath.count(os.sep) - base.count(os.sep) > 3:
            dirs[:] = []
        if any(f.lower().endswith(exts) for f in files):
            return True
    return False


def _glob_deep(root, ext, needle=None):
    for dirpath, dirs, files in os.walk(root):
        if dirpath.count(os.sep) - root.count(os.sep) > 5:
            dirs[:] = []
        for f in files:
            if f.lower().endswith(ext.lower()) and (needle is None or needle.lower() in dirpath.lower() + f.lower()
                                                    or f[:1].isalpha() and f[1:4].isdigit()):
                return True
    return False


def _exif_brand(root):
    """Make/Model from the first JPEG/RAW under DCIM (EXIF)."""
    for dirpath, dirs, files in os.walk(os.path.join(root, "DCIM")):
        for f in sorted(files):
            if f.lower().endswith((".jpg", ".jpeg", ".arw", ".nef", ".cr2", ".dng", ".orf", ".rw2", ".pef")):
                try:
                    with Source(os.path.join(dirpath, f)) as s:
                        head = s.read(0, 256 * 1024)
                    off = head.find(b"Exif\0\0")
                    base = off + 6 if off >= 0 else 0
                    t = Tiff(_Buf(head), base)
                    t.walk()
                    m = exif_meta(t.tags)
                    if m.get("marca") or m.get("modelo"):
                        return {"make": m.get("marca", ""), "model": m.get("modelo", "")}
                except Exception:
                    continue
                return {}
    return {}


class _Buf:
    def __init__(self, b):
        self.b, self.size = b, len(b)

    def read(self, off, n):
        return self.b[off:off + n]


def detect(mount):
    """Camera profile for a mounted card, or None if it does not look like camera media."""
    if not os.path.isdir(mount):
        return None
    hits = [(b, exts, line) for b, test, exts, line in LAYOUTS if _safe(test, mount)]
    if not hits:
        return None
    brand, exts, line = hits[0]
    allx = []
    for _, e, _ in hits:
        allx += [x for x in e if x not in allx]
    meta = _exif_brand(mount) if _has(mount, "DCIM") else {}
    return {"brand": brand, "line": line, "formats": allx, "camera": meta.get("camera") or meta.get("model") or "",
            "make": meta.get("make", ""),
            "folders": [n for n in ("DCIM", "PRIVATE", "CONTENTS", "XDROOT", "MISC") if _has(mount, n)],
            "categories": sorted({"video" if x in VIDEO else "photo" if x in PHOTO else "document" for x in allx})}


VIDEO = {"mp4", "mov", "mxf", "mts", "m2ts", "crm", "r3d", "braw", "insv", "lrv", "lrf", "ari"}
PHOTO = {"jpg", "arw", "cr3", "cr2", "nef", "nrw", "raf", "orf", "rw2", "dng", "pef", "gpr", "insp", "thm"}


def _safe(test, mount):
    try:
        return bool(test(mount))
    except OSError:
        return False


def card_devices(devices):
    """Removable disks from ``devices.list_devices()`` annotated with a camera profile when mounted."""
    out = []
    for d in devices:
        if d.get("internal") or d.get("is_image"):
            continue
        prof = None
        for p in d.get("partitions", []):
            if p.get("mount"):
                prof = detect(p["mount"])
                if prof:
                    prof["mount"] = p["mount"]
                    prof["partition"] = p["path"]
                    break
        out.append(dict(d, profile=prof))
    return out
