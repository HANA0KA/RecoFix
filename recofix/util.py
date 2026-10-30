"""Small helpers shared by the engine modules."""
import os
import shutil


def chown_user(path):
    """Give files written as root (sudo) back to the user who launched RecoFix."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid or os.geteuid() != 0:
        return
    targets = [path]
    if os.path.isdir(path) and not os.path.islink(path):
        for root, dirs, files in os.walk(path):
            targets += [os.path.join(root, n) for n in dirs + files]
    for p in targets:
        try:
            os.lchown(p, int(uid), int(gid or uid))
        except OSError:
            pass


def unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)


def tool(name):
    """Absolute path of an external tool (Homebrew locations included) or None."""
    for p in (shutil.which(name), "/opt/homebrew/bin/" + name, "/usr/local/bin/" + name):
        if p and os.path.exists(p):
            return p
    return None


def home():
    return os.path.expanduser("~" + (os.environ.get("SUDO_USER") or ""))
