"""Disk health: S.M.A.R.T. status and attributes (Recoverit "S.M.A.R.T Disk Monitoring").

Recoverit ships smartmontools inside its DiskSMARTMonitor login item. RecoFix
uses ``smartctl`` when it is installed (``brew install smartmontools``) for the
full attribute table, and falls back to what macOS itself reports
(``diskutil``/``system_profiler``) so every disk still gets a verdict.
"""
import json
import plistlib
import subprocess

from .util import tool

CRITICAL_ATA = {5: "Sectores reasignados", 187: "Errores no corregibles", 188: "Timeouts de comando",
                197: "Sectores pendientes", 198: "Sectores incorregibles offline", 199: "Errores CRC (cable)"}


def _diskutil(dev):
    try:
        return plistlib.loads(subprocess.run(["diskutil", "info", "-plist", dev], capture_output=True, timeout=30).stdout)
    except Exception:
        return {}


def _profiler():
    try:
        out = subprocess.run(["system_profiler", "-json", "SPNVMeDataType", "SPSerialATADataType", "SPUSBDataType"],
                             capture_output=True, text=True, timeout=60).stdout
        return json.loads(out or "{}")
    except Exception:
        return {}


def _smartctl(dev):
    exe = tool("smartctl")
    if not exe:
        return None
    for extra in ([], ["-d", "sat"]):
        p = subprocess.run([exe, "-a", "-j"] + extra + [dev], capture_output=True, text=True, timeout=60)
        try:
            d = json.loads(p.stdout or "{}")
        except ValueError:
            continue
        if d.get("smart_status") or d.get("ata_smart_attributes") or d.get("nvme_smart_health_information_log"):
            return d
    return None


def health(disk):
    """Verdict + details for a whole disk identifier (``disk4``)."""
    dev = "/dev/" + disk.replace("/dev/", "").replace("rdisk", "disk")
    info = _diskutil(dev)
    rep = {"disk": dev, "model": info.get("MediaName", ""), "size": info.get("TotalSize") or info.get("Size", 0),
           "protocol": info.get("BusProtocol", ""), "ssd": info.get("SolidState"),
           "macos_status": info.get("SMARTStatus", "No compatible"), "attributes": [], "warnings": [],
           "source": "diskutil", "temperature": None, "power_on_hours": None}
    d = _smartctl(dev)
    if d:
        rep["source"] = "smartctl"
        rep["model"] = d.get("model_name") or rep["model"]
        rep["serial"] = d.get("serial_number", "")
        rep["firmware"] = d.get("firmware_version", "")
        passed = (d.get("smart_status") or {}).get("passed")
        rep["temperature"] = (d.get("temperature") or {}).get("current")
        rep["power_on_hours"] = (d.get("power_on_time") or {}).get("hours")
        for a in (d.get("ata_smart_attributes") or {}).get("table", []):
            raw = (a.get("raw") or {}).get("value", 0)
            row = {"id": a["id"], "name": a.get("name", ""), "value": a.get("value"), "worst": a.get("worst"),
                   "thresh": a.get("thresh"), "raw": raw, "failing": bool(a.get("when_failed"))}
            rep["attributes"].append(row)
            if a["id"] in CRITICAL_ATA and raw:
                rep["warnings"].append("%s: %s" % (CRITICAL_ATA[a["id"]], raw))
            if row["failing"]:
                rep["warnings"].append("Atributo %s por debajo del umbral" % a.get("name"))
        nv = d.get("nvme_smart_health_information_log")
        if nv:
            for k, label in (("percentage_used", "Desgaste (%)"), ("available_spare", "Reserva disponible (%)"),
                             ("media_errors", "Errores de medio"), ("unsafe_shutdowns", "Apagados inseguros"),
                             ("data_units_written", "Unidades escritas"), ("critical_warning", "Aviso crítico")):
                if k in nv:
                    rep["attributes"].append({"id": k, "name": label, "raw": nv[k]})
            if nv.get("critical_warning"):
                rep["warnings"].append("El controlador NVMe informa de un aviso crítico")
            if nv.get("media_errors"):
                rep["warnings"].append("Errores de medio: %s" % nv["media_errors"])
            if (nv.get("percentage_used") or 0) >= 90:
                rep["warnings"].append("Vida útil casi agotada (%s%%)" % nv["percentage_used"])
        verdict = "ok" if passed and not rep["warnings"] else "aviso" if passed else "fallo" if passed is False else "desconocido"
    else:
        st = (rep["macos_status"] or "").lower()
        verdict = "ok" if st == "verified" else "fallo" if st == "failing" else "desconocido"
        if verdict == "desconocido":
            rep["warnings"].append("macOS no expone S.M.A.R.T. para este disco (USB/lector). Instala smartmontools "
                                   "(brew install smartmontools) para leer los atributos.")
    rep["verdict"] = verdict
    return rep


def all_disks(devices):
    return [health(d["id"]) for d in devices if not d.get("is_image")]
