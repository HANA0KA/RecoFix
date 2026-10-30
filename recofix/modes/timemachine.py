"""Time Machine: list backups and local APFS snapshots, browse them, restore files.

Recoverit's ``TimeMachineSelectWndCtrl`` asks for "one Time Machine backup file
to scan". Backups are complete, intact copies, so RecoFix browses and copies
from them instead of carving: names, folders and dates come back exactly.
"""
import os
import plistlib
import re
import shutil
import subprocess
import tempfile

from ..util import chown_user, unique

MOUNTS = {}


def _run(*args, timeout=60):
    p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def destinations():
    code, out, err = _run("tmutil", "destinationinfo", "-X")
    try:
        d = plistlib.loads(out.encode()) if code == 0 and out.strip() else {}
    except Exception:
        d = {}
    return [{"name": x.get("Name", ""), "kind": x.get("Kind", ""), "mount": x.get("MountPoint", ""),
             "id": x.get("ID", "")} for x in d.get("Destinations", [])]


def backups():
    """Completed backups (paths are browsable while the destination is mounted)."""
    code, out, err = _run("tmutil", "listbackups", "-m", timeout=120)
    items = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("/"):
            m = re.search(r"(\d{4}-\d{2}-\d{2}-\d{6})", line)
            items.append({"path": line, "date": _fmt(m.group(1)) if m else os.path.basename(line), "kind": "backup"})
    error = ""
    if code != 0:
        error = (err or out).strip() or "tmutil no devolvió copias"
        if "Full Disk Access" in error or "permission" in error.lower():
            error = "tmutil necesita «Acceso total al disco» para RecoFix/Terminal."
    return {"backups": items[::-1], "error": error, "destinations": destinations(), "snapshots": snapshots()}


def _fmt(stamp):
    d, t = stamp[:10], stamp[11:]
    return "%s %s:%s:%s" % (d, t[:2], t[2:4], t[4:6])


def snapshots(volume="/"):
    code, out, err = _run("tmutil", "listlocalsnapshots", volume)
    snaps = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("com.apple.TimeMachine."):
            m = re.search(r"(\d{4}-\d{2}-\d{2}-\d{6})", line)
            snaps.append({"name": line, "date": _fmt(m.group(1)) if m else line, "kind": "snapshot", "volume": volume})
    return snaps[::-1]


def mount_snapshot(name, volume="/"):
    """Mount a local snapshot read-only (needs root) and return its mount point."""
    if name in MOUNTS and os.path.ismount(MOUNTS[name]):
        return MOUNTS[name]
    mnt = tempfile.mkdtemp(prefix="recofix_snap_")
    code, out, err = _run("mount_apfs", "-o", "ro,nobrowse", "-s", name, volume, mnt)
    if code != 0:
        os.rmdir(mnt)
        raise RuntimeError((err or out).strip() or "no se pudo montar la instantánea (requiere administrador)")
    MOUNTS[name] = mnt
    return mnt


def unmount_all():
    for name, mnt in list(MOUNTS.items()):
        _run("umount", mnt)
        try:
            os.rmdir(mnt)
        except OSError:
            pass
        MOUNTS.pop(name, None)


def browse(path, limit=5000):
    """Directory listing inside a backup or mounted snapshot."""
    out = []
    with os.scandir(path) as it:
        for e in it:
            if len(out) >= limit:
                break
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            out.append({"name": e.name, "path": e.path, "is_dir": e.is_dir(follow_symlinks=False),
                        "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return out


def search(root, text, limit=500, cancel=None):
    text = text.lower()
    hits = []
    for dirpath, dirs, files in os.walk(root):
        if cancel is not None and cancel.is_set():
            break
        for n in dirs + files:
            if text in n.lower():
                p = os.path.join(dirpath, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                hits.append({"name": n, "path": p, "is_dir": os.path.isdir(p), "size": st.st_size,
                             "mtime": st.st_mtime})
                if len(hits) >= limit:
                    return hits
    return hits


def restore(paths, dest):
    results = []
    for p in paths:
        target = unique(os.path.join(dest, os.path.basename(p.rstrip("/"))))
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.copytree(p, target, symlinks=True)
            else:
                shutil.copy2(p, target)
            chown_user(target)
            results.append({"path": p, "ok": True, "target": target})
        except OSError as e:
            results.append({"path": p, "ok": False, "note": str(e)})
    return results
