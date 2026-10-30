"""Quick locations and folder scans ("Hard Drives and Locations" in Recoverit).

A folder scan is a scan of the volume that contains the folder, restricted to
entries under it (Recoverit's ``ScanRequset.scan_path``). Deleted entries keep
their original parent path in FAT/exFAT/NTFS metadata, so this finds files
deleted from that folder too.
"""
import os
import plistlib
import subprocess

PARSED_FS = {"exfat", "msdos", "ntfs", "fat32", "fat16", "fat12"}


def _home():
    return os.path.expanduser("~" + (os.environ.get("SUDO_USER") or ""))


def quick_locations():
    h = _home()
    out = []
    for key, label, path in (("desktop", "Escritorio", os.path.join(h, "Desktop")),
                             ("documents", "Documentos", os.path.join(h, "Documents")),
                             ("downloads", "Descargas", os.path.join(h, "Downloads")),
                             ("movies", "Películas", os.path.join(h, "Movies")),
                             ("pictures", "Imágenes", os.path.join(h, "Pictures")),
                             ("trash", "Papelera", os.path.join(h, ".Trash"))):
        out.append({"key": key, "label": label, "path": path, "exists": os.path.isdir(path)})
    return out


def resolve_folder(path):
    """Device node, mount point, filesystem and in-volume path of ``path``."""
    path = os.path.realpath(path)
    try:
        line = subprocess.run(["df", "-P", path], capture_output=True, text=True, timeout=20).stdout.splitlines()[1]
    except (IndexError, subprocess.SubprocessError):
        raise ValueError("No se pudo resolver el volumen de %s" % path)
    dev = line.split()[0]
    mount = line.split(None, 5)[5] if len(line.split()) >= 6 else "/"
    info = {}
    try:
        info = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", dev], capture_output=True, timeout=20).stdout)
    except Exception:
        pass
    fs = (info.get("FilesystemType") or "").lower()
    rel = "/" + os.path.relpath(path, mount).strip(".").strip("/") if path != mount else "/"
    return {
        "device": "/dev/r" + dev[5:] if dev.startswith("/dev/disk") else dev,
        "mount": mount, "fs": fs, "fs_name": info.get("FilesystemName", fs), "scope": rel,
        "metadata_scan": fs in PARSED_FS,
        "warning": "" if fs in PARSED_FS else (
            "%s no conserva nombres de archivos borrados accesibles; se hará un escaneo profundo del "
            "volumen completo y los resultados no se limitan a la carpeta." % (info.get("FilesystemName") or fs or "Este sistema")),
    }
