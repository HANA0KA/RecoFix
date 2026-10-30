"""Build real exFAT / FAT32 disk images with macOS tools, delete and reformat, then export raw.

Only ever touches devices that ``hdiutil attach`` created for our own image file,
and checks ``hdiutil info`` before erasing anything.
"""
import os
import plistlib
import shutil
import subprocess

from fixtures import SAMPLES, WORK, build

IMG = os.path.join(WORK, "images")


def _attach(img):
    out = plistlib.loads(subprocess.run(["hdiutil", "attach", "-nobrowse", "-plist", img],
                                        capture_output=True, check=True).stdout)
    ents = out["system-entities"]
    mount = next(e["mount-point"] for e in ents if "mount-point" in e)
    dev = next(e["dev-entry"] for e in ents if "mount-point" in e)
    return dev, mount


def _owned(dev, img):
    info = plistlib.loads(subprocess.run(["hdiutil", "info", "-plist"], capture_output=True, check=True).stdout)
    for im in info.get("images", []):
        if os.path.realpath(im.get("image-path", "")) == os.path.realpath(img):
            if any(e.get("dev-entry") == dev for e in im.get("system-entities", [])):
                return True
    return False


def _raw(img, out):
    if os.path.exists(out):
        os.remove(out)
    subprocess.run(["hdiutil", "convert", "-quiet", img, "-format", "UDTO", "-o", out], check=True)
    if os.path.exists(out + ".cdr"):
        os.rename(out + ".cdr", out)


def make(fs, name, deleted, reformat=False, size="160m"):
    os.makedirs(IMG, exist_ok=True)
    raw = os.path.join(IMG, name + ".raw")
    if os.path.exists(raw):
        return raw
    s = build()
    img = os.path.join(IMG, name + ".dmg")
    if os.path.exists(img):
        os.remove(img)
    subprocess.run(["hdiutil", "create", "-quiet", "-size", size, "-layout", "NONE", "-fs", fs,
                    "-volname", "CAMARA", img], check=True)
    dev, mnt = _attach(img)
    try:
        clips = os.path.join(mnt, "RDC", "OCF", "A057R0Y8")
        os.makedirs(clips)
        shutil.copy(s["op1a_mpeg2.mxf"], os.path.join(clips, "A057C001_261015_R0Y8.mxf"))
        shutil.copy(s["op1a_dnxhr.mxf"], os.path.join(clips, "A057C002_261015_R0Y8.mxf"))
        shutil.copy(s["op1a_prores.mxf"], os.path.join(clips, "A057C003_261015_R0Y8.mxf"))
        shutil.copy(s["clip.mhl"], os.path.join(clips, "A057R0Y8.mhl"))
        os.makedirs(os.path.join(mnt, "DCIM", "100CANON"))
        for k in ("photo.jpg", "photo.png", "clip_xavc.mp4", "clip_prores.mov", "audio.wav", "doc.pdf",
                  "informe.docx", "clip.mkv", "audio.mp3"):
            shutil.copy(s[k], os.path.join(mnt, "DCIM", "100CANON", k))
        subprocess.run(["sync"])
        for rel in deleted:
            os.remove(os.path.join(mnt, rel))
        subprocess.run(["sync"])
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", dev], check=True)
    if reformat:
        dev, mnt = _attach(img)
        assert _owned(dev, img), "refusing to erase a device that is not our image"
        fmt = {"ExFAT": "ExFAT", "MS-DOS FAT32": "FAT32"}[fs]
        subprocess.run(["diskutil", "eraseVolume", fmt, "NUEVA", dev], check=True, capture_output=True)
        subprocess.run(["hdiutil", "detach", "-quiet", dev], check=True)
    _raw(img, raw)
    os.remove(img)
    return raw


DELETED = ["RDC/OCF/A057R0Y8/A057C002_261015_R0Y8.mxf", "DCIM/100CANON/photo.jpg", "DCIM/100CANON/clip_xavc.mp4"]


def all_images():
    return {
        "exfat_borrados": make("ExFAT", "exfat_borrados", DELETED),
        "exfat_formateada": make("ExFAT", "exfat_formateada", [], reformat=True),
        "fat32_borrados": make("MS-DOS FAT32", "fat32_borrados", DELETED),
    }


if __name__ == "__main__":
    for k, v in all_images().items():
        print(k, v, os.path.getsize(v))
