"""Trash / Recycle bin recovery.

Lists everything in the user's Trash, every mounted volume's ``.Trashes/<uid>``
and the local iCloud Drive "Recently Deleted" folder, with the original
location Finder recorded (``ptbL``/``ptbN`` in ``.DS_Store``). Restoring copies
(never moves) so the Trash keeps its copy until the user empties it.
"""
import glob
import os
import pwd
import shutil
import time

from ..util import chown_user, unique as _unique
from .dsstore import put_back_info


def _user():
    name = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    pw = pwd.getpwnam(name)
    return name, pw.pw_uid, pw.pw_dir


def trash_dirs():
    name, uid, home = _user()
    out = [{"kind": "Papelera", "path": os.path.join(home, ".Trash"), "volume": "/"}]
    for vol in sorted(glob.glob("/Volumes/*")):
        t = os.path.join(vol, ".Trashes", str(uid))
        if os.path.isdir(t) and not os.path.islink(vol):
            out.append({"kind": "Papelera de " + os.path.basename(vol), "path": t, "volume": vol})
    icloud = os.path.join(home, "Library", "Mobile Documents", "com~apple~CloudDocs", ".Trash")
    if os.path.isdir(icloud):
        out.append({"kind": "iCloud Drive · Eliminados recientemente", "path": icloud, "volume": "icloud"})
    return out


def _size(p):
    if os.path.isfile(p) or os.path.islink(p):
        return os.lstat(p).st_size, 1
    tot = n = 0
    for root, dirs, files in os.walk(p):
        for f in files:
            try:
                tot += os.lstat(os.path.join(root, f)).st_size
                n += 1
            except OSError:
                pass
    return tot, n


def list_items():
    """[{id, name, path, size, files, is_dir, trashed, original, bin}] plus errors (e.g. no Full Disk Access)."""
    items, errors = [], []
    for t in trash_dirs():
        try:
            names = os.listdir(t["path"])
        except PermissionError:
            errors.append("%s: sin permiso. Concede «Acceso total al disco» a RecoFix (o a Terminal) en "
                          "Ajustes del Sistema › Privacidad y seguridad." % t["kind"])
            continue
        except OSError as e:
            errors.append("%s: %s" % (t["kind"], e))
            continue
        pb = put_back_info(os.path.join(t["path"], ".DS_Store"))
        vol_root = "" if t["volume"] in ("/", "icloud") else t["volume"]
        for n in names:
            if n in (".DS_Store", ".localized"):
                continue
            p = os.path.join(t["path"], n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            size, files = _size(p)
            info = pb.get(n, {})
            original = ""
            if info.get("folder") is not None:
                original = os.path.join("/" + vol_root.strip("/") if vol_root else "/", info["folder"],
                                        info.get("name") or n)
            items.append({"id": len(items), "name": info.get("name") or n, "trash_name": n, "path": p,
                          "size": size, "files": files, "is_dir": os.path.isdir(p),
                          "trashed": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_ctime)),
                          "original": original, "bin": t["kind"],
                          "ext": n.rsplit(".", 1)[-1].lower() if "." in n else ""})
    return {"items": items, "errors": errors}


def restore(paths, dest=None, to_original=False):
    """Copy trashed items to ``dest`` or to their recorded original folder."""
    known = {it["path"]: it for it in list_items()["items"]}
    results = []
    for p in paths:
        it = known.get(p)
        if not it:
            results.append({"path": p, "ok": False, "note": "no está en la papelera"})
            continue
        if to_original and it["original"]:
            target = _unique(it["original"])
        elif dest:
            target = _unique(os.path.join(dest, it["name"]))
        else:
            results.append({"path": p, "ok": False, "note": "sin destino ni ubicación original"})
            continue
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if it["is_dir"]:
                shutil.copytree(p, target, symlinks=True)
            else:
                shutil.copy2(p, target)
            chown_user(target)
            results.append({"path": p, "ok": True, "target": target})
        except OSError as e:
            results.append({"path": p, "ok": False, "note": str(e)})
    return results
