"""List physical disks and partitions with diskutil."""
import plistlib
import subprocess


def _plist(*args):
    out = subprocess.run(["diskutil", args[0], "-plist", *args[1:]], capture_output=True, timeout=30).stdout
    return plistlib.loads(out) if out else {}


def list_devices():
    data = _plist("list")
    out = []
    for d in data.get("AllDisksAndPartitions", []):
        ident = d["DeviceIdentifier"]
        try:
            info = _plist("info", ident)
        except Exception:
            info = {}
        if info.get("VirtualOrPhysical") == "Virtual" and info.get("BusProtocol") != "Disk Image":
            continue  # synthesized APFS container: recover from its physical store instead
        disk = {
            "id": ident,
            "path": "/dev/r" + ident,
            "name": (info.get("MediaName") or info.get("IORegistryEntryName") or ident).strip(),
            "size": d.get("Size", 0),
            "internal": info.get("Internal", False),
            "removable": info.get("RemovableMediaOrExternalDevice", info.get("Removable", False)),
            "protocol": info.get("BusProtocol", ""),
            "is_image": info.get("BusProtocol") == "Disk Image" or "Disk Image" in str(info.get("MediaName", "")),
            "partitions": [],
        }
        for p in d.get("Partitions", []):
            disk["partitions"].append({
                "id": p["DeviceIdentifier"], "path": "/dev/r" + p["DeviceIdentifier"],
                "name": p.get("VolumeName", ""), "size": p.get("Size", 0), "content": p.get("Content", ""),
                "mount": p.get("MountPoint", ""),
            })
        out.append(disk)
    return out
