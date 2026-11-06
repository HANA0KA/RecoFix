"""Rescue of clips whose directory entry was reset to size 0 while their data stayed in free space.

Typical case: an exFAT camera/backup volume where some clips show 0 bytes and
first cluster 0 although the media was written completely (and verified by the
on-set MHL). The bytes are still there, just unreferenced and marked free.

The rescue combines independent sources of truth:

* the MHL (found on the volume itself or given by the user) says which clips
  must exist, their exact size and their hash;
* the format structure (a header the handler accepts, a footer that closes at
  the MHL size, the name stored inside the file) says where each clip starts;
* the FAT chain that deletion leaves behind says where a *fragmented* clip
  continues (a zero entry means "the next cluster": a contiguous clip);
* the allocation bitmap says which clusters belong to other files today, i.e.
  which parts of a clip were overwritten and can no longer be recovered;
* the hash decides: any ambiguity is resolved by hashing the candidates, and
  the written copy is compared with the MHL again by ``Recovery``.

Every clip that cannot be rescued gets a diagnosis (``code``), a plain-language
explanation and what to do about it (``fix`` / ``action``). Nothing is ever
written to the source.
"""
import bisect
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field

from . import mhl
from .formats import BY_KEY, HANDLERS
from .fs import FileEntry, Volume, open_fs, volumes
from .fs.flat import Flat
from .scanner import Scan, category_of
from .source import Source
from .util import tool

HEAD = 64  # bytes read at the start of every candidate cluster (longest signature is 13 bytes at offset 0)
PAD_MAX = 1 << 20  # bytes a structure-derived length may fall short of the MHL size (trailing padding)
MAX_VERIFY = 6  # candidates hashed to break a tie
HASH_CHUNK = 8 << 20
SURVEY_SAMPLES = 240  # free clusters sampled to tell "data still there" from "free space wiped"
MAX_EVENTS = 4000
XXH = ("xxhash64be", "xxhash64", "xxh64")
HASHLIB = ("md5", "sha1", "sha256")
MAX_HASH_ONLY = 300  # candidates hashed when the manifest has no size (hash is the only way to recognise the clip)
CHUNK = 16 << 20  # bytes read at once when the cluster is small enough that a full read beats one seek per cluster
CLUSTER_FS = ("exfat", "fat")
MIN_FLAT = 64 << 20  # volumes smaller than this are not searched without a file tree (boot/recovery partitions)

# extension -> format handlers whose header can start such a file
EXT_HANDLERS = {
    "mxf": ("mxf",),
    "mov": ("bmff",), "mp4": ("bmff",), "m4v": ("bmff",), "3gp": ("bmff",), "braw": ("bmff",), "crm": ("bmff",),
    "insv": ("bmff",), "r3d": ("r3d",), "mkv": ("mkv",), "webm": ("mkv",), "avi": ("riff",), "wav": ("riff",),
    "mpg": ("mpg",), "vob": ("mpg",),
}

RANK_LABEL = {4: "exacto", 3: "con relleno", 2: "sin verificar", 1: "solo firma"}

# code -> (what to do, action the UI can offer: add_mhl | search_all | None)
REMEDY = {
    "no_mhl": ("Añade el .mhl del rodaje o una lista de hashes (.md5, .sha1, .sha256, .xxh64): el archivo o la carpeta que la "
               "contiene. Sin el hash no hay forma de reconocer este archivo.", "add_mhl"),
    "mhl_conflict": ("Hay dos manifiestos con hashes o tamaños distintos para este nombre. Quita el que no pertenezca a este "
                     "disco y repite la búsqueda.", "add_mhl"),
    "no_handler": ("RecoFix no reconoce todavía la cabecera de este formato. Prueba con «Discos y ubicaciones» → "
                   "escaneo profundo.", None),
    "no_header_near": ("No hay ninguna cabecera cerca de los demás archivos de su carpeta. Pulsa «Buscar en todo el "
                       "volumen»: es más lento pero revisa todo el espacio libre.", "search_all"),
    "no_header_all": ("Se revisó todo el espacio libre y no hay ninguna cabecera con este nombre. Lo más probable es que "
                      "la copia de este clip no llegara a escribirse en este disco o se haya sobrescrito: recupéralo de "
                      "otra copia (tarjeta de cámara u otro disco del mismo rodaje).", None),
    "data_gone": ("El espacio libre de este disco está a ceros: los datos se borraron de verdad (formato seguro, TRIM o "
                  "sobrescritura). No existe nada que reconstruir aquí; recupéralo de otra copia (tarjeta de cámara u "
                  "otro disco del mismo rodaje).", None),
    "ambiguous": ("Hay varias cabeceras igual de buenas y no se pudo desempatar. Comprueba que el MHL es el de este disco "
                  "o acota la búsqueda con un rango de clusters (opciones avanzadas).", None),
    "hash_none": ("Ninguna cabecera candidata coincide con el hash del MHL: sus datos se sobrescribieron o el MHL es de "
                  "otra copia. Comprueba que el MHL pertenece a este disco; si es correcto, recupéralo de otra copia.",
                  None),
    "all_taken": ("Todas las cabeceras compatibles ya se asignaron a otros clips. Revisa que los nombres del MHL sean los "
                  "de este rodaje.", None),
    "overwritten": ("Parte de sus clusters pertenece hoy a otros archivos, así que esos datos ya no existen. El resto se "
                    "recupera igualmente y queda marcado como NO_VERIFICADO. Para tener el clip íntegro hace falta otra "
                    "copia (tarjeta de cámara u otro disco).", None),
}


def issue(code, reason):
    fix, action = REMEDY[code]
    return {"code": code, "reason": reason, "fix": fix, "action": action}


def handlers_for(ext):
    keys = EXT_HANDLERS.get(ext)
    if keys:
        return [BY_KEY[k] for k in keys if k in BY_KEY]
    return [h for h in HANDLERS if h.ext == ext]


def _stem(name):
    return os.path.splitext(name or "")[0].strip().lower()


def friendly_error(msg):
    """Plain-language version of the low-level errors a user hits before the scan even starts."""
    low = msg.lower()
    if "permission denied" in low or "errno 1]" in low or "operation not permitted" in low:
        return ("Sin permiso para leer el disco. Abre RecoFix con permisos de administrador (la app te los pide al "
                "iniciar) o, en la terminal: sudo python3 -m recofix serve. " + "[" + msg + "]")
    if "no such file" in low or "errno 2]" in low or "device not configured" in low or "errno 6]" in low:
        return ("El disco ya no está disponible. Los números de dispositivo (diskN) cambian al reconectar un disco: "
                "vuelve al paso 1 y elígelo de nuevo. [" + msg + "]")
    if "resource busy" in low or "errno 16]" in low:
        return "El disco está en uso por otra aplicación. Ciérrala o expúlsalo y vuelve a conectarlo. [" + msg + "]"
    if "input/output error" in low or "errno 5]" in low:
        return ("Error de lectura del disco. Comprueba el cable y el puerto; si persiste, crea primero una imagen del "
                "disco (Imagen de disco) y rescata desde la imagen. [" + msg + "]")
    return msg


@dataclass
class Target:
    entry: object
    name: str
    ext: str
    folder: str
    rec: dict = None  # MHL record (size/hash/algo) or None when only the name is known
    size: int = None
    need: int = None  # clusters
    cands: list = field(default_factory=list)


class Interval:
    """Sorted, merged cluster intervals: which clusters the live directory tree already owns."""

    def __init__(self, ranges):
        merged = []
        for a, b in sorted(ranges):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self.starts = [m[0] for m in merged]
        self.ends = [m[1] for m in merged]

    def has(self, c):
        i = bisect.bisect_right(self.starts, c) - 1
        return i >= 0 and c < self.ends[i]

    def overlap(self, a, b):
        """Clusters of [a, b) that fall inside an interval."""
        i = max(0, bisect.bisect_right(self.starts, a) - 1)
        n = 0
        while i < len(self.starts) and self.starts[i] < b:
            n += max(0, min(b, self.ends[i]) - max(a, self.starts[i]))
            i += 1
        return n


def _fmt_b(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%d B" % n if u == "B" else "%.1f %s" % (n, u)
        n /= 1024


class ZeroedScan(Scan):
    """Find zeroed clips on exFAT volumes and turn them into recoverable ``Item``s (group ``zeroed``).

    ``mhl_paths``: extra .mhl files or folders (the volume's own MHLs are always used).
    ``search``: ``near`` looks only around the files that live in the same folder (fast);
    ``all`` looks through the whole volume.
    ``cluster_range``: ``(first, last)`` clusters to search, overriding ``search`` (advanced).
    ``include_missing``: also look for manifest files that are not on the volume at all (lost, not just emptied).
    It is always on where the filesystem tree cannot be read (NTFS damage, APFS, HFS+, ext, raw disk).

    Works on any volume: exFAT and FAT12/16/32 use the allocation table (FAT chains for fragmented clips, the table
    to tell overwritten clusters); anything else is searched block by block and arbitrated by the hash.
    """

    STAGES = ("leer", "mhl", "buscar", "emparejar", "fin")

    def __init__(self, path, mhl_paths=None, search="near", volume_index=None, label="", log=None, cluster_range=None,
                 include_missing=False):
        super().__init__(path, deep=False, volume_index=volume_index, lost_partitions=False,
                         label=label, mode="zeroed", log=log)
        self.mhl_paths = list(mhl_paths or [])
        self.wide = search == "all"
        self.cluster_range = tuple(cluster_range) if cluster_range else None
        self.include_missing = include_missing
        self.unresolved = []
        self.mhl_sources = []
        self.mhl_errors = []
        self.targets_total = 0
        self.summary = ""
        self.events = []
        self.stage = "leer"
        self.volume_info = []
        self.survey_info = {}
        self._runs_cache = {}
        self._verified = {}
        self._target_keys = set()
        self._ext_found = None
        self._resolved = []
        self._logged_sources = []
        self._logged_errors = []

    # ------------------------------------------------------------------ status / control

    def _ev(self, msg, level="info"):
        """Detailed process log shown to the user ("what is RecoFix doing and why")."""
        if len(self.events) < MAX_EVENTS:
            self.events.append({"t": round(time.time() - self.started, 1), "level": level, "msg": msg})

    def status(self):
        st = super().status()
        st["zeroed"] = {"targets": self.targets_total, "resolved": sum(1 for i in self.items if i.group == "zeroed"),
                        "unresolved": list(self.unresolved), "mhl_files": list(self.mhl_sources),
                        "mhl_errors": list(self.mhl_errors),
                        "search": "range" if self.cluster_range else "all" if self.wide else "near",
                        "range": list(self.cluster_range) if self.cluster_range else None,
                        "summary": self.summary, "stage": self.stage, "volumes": list(self.volume_info),
                        "survey": dict(self.survey_info), "events": self.events[-300:], "events_total": len(self.events),
                        "xxhsum": bool(tool("xxhsum"))}
        return st

    def _checkpoint(self):
        """Honour pause/cancel; True when the scan must stop."""
        if self.paused.is_set() and not self.cancel.is_set():
            before = self.phase
            while self.paused.is_set() and not self.cancel.is_set():
                self.phase = "en pausa"
                time.sleep(0.2)
            self.phase = before
        if self.cancel.is_set():
            self.phase = "cancelado"
            return True
        return False

    def run(self):
        super().run()
        if self.error:
            self.error = friendly_error(self.error)
            self._ev(self.error, "error")
        self.stage = "fin"

    # ------------------------------------------------------------------ main flow

    def _run(self, src):
        self.phase = "sistemas de archivos"
        self.stage = "leer"
        vols = volumes(src) or [Volume(0, src.size, "?", scheme="sin particiones")]
        self.volumes = vols
        if self.volume_index is not None and not 0 <= self.volume_index < len(vols):
            raise ValueError("El volumen %s no existe: el origen tiene %d." % (self.volume_index, len(vols)))
        pool = vols if self.volume_index is None else [vols[self.volume_index]]
        usable = [v for v in pool if v.fstype in CLUSTER_FS or v.size >= MIN_FLAT]
        if not usable:
            raise RuntimeError("El origen no contiene ningún volumen utilizable (solo particiones muy pequeñas o vacías).")
        self._ev("Origen abierto en solo lectura: nada se escribe en él.")
        self._ev("Volúmenes del origen: " + "; ".join("%s %s%s" % (v.fstype.upper() if v.fstype != "?" else "sin reconocer",
                                                                  _fmt_b(v.size), " «%s»" % v.label if v.label else "") for v in pool))
        for v in usable:
            if self._checkpoint():
                return
            try:
                self._volume(src, v)
            except (OSError, ValueError, RuntimeError) as e:
                if len(usable) == 1:
                    raise
                self._ev("Volumen %s (%s): %s" % (v.label or v.fstype, _fmt_b(v.size), friendly_error(str(e))), "warn")
        if len(usable) > 1:  # a clip found in a later volume is not "unresolved" because an earlier one lacked it
            done = {n.lower() for n in self._resolved}
            self.unresolved = [u for u in self.unresolved if u["name"].lower() not in done]
        if not self.targets_total and not self.summary:
            self.summary = ("No hay nada que reconstruir: ningún archivo de 0 bytes figura en un manifiesto de hashes "
                            "(MHL o lista md5/sha/xxhash).")
            self._ev(self.summary)
        self.stage = "fin"

    def _open(self, src, vol):
        """(fs, parsed, cluster_fs, live, tree): the volume as the rescue will see it.

        ``fs`` always offers cluster arithmetic: the real parser for exFAT / FAT, a block view (``Flat``) for anything
        else. ``live`` are the files the readable tree still lists; ``tree`` says whether a tree could be read at all."""
        try:
            parsed = open_fs(src, vol)
        except Exception as e:
            parsed = None
            self._ev("No se pudo abrir el sistema de archivos %s (%s): se buscará por bloques." % (vol.fstype, e), "warn")
        cluster_fs = vol.fstype in CLUSTER_FS and parsed is not None
        fs = parsed if cluster_fs else Flat(src, vol, parsed)
        live, tree = [], False
        if parsed is not None:
            self._ev("Leyendo el árbol de carpetas del volumen %s…" % (getattr(parsed, "name", vol.fstype)))
            try:
                live = [e for e in fs.walk() if not e.deleted]
                tree = True
            except Exception as e:
                self._ev("El árbol de carpetas no se pudo leer (%s): se buscará por bloques." % e, "warn")
        return fs, parsed, cluster_fs, live, tree

    def _volume(self, src, vol):
        self._runs_cache = {}
        self.phase = "leyendo el volumen"
        fs, parsed, cluster_fs, live, tree = self._open(src, vol)
        free = fs.free_count()
        info = {"label": getattr(fs, "label", "") or vol.label, "cluster_size": fs.cs, "clusters": fs.n_clusters,
                "free_clusters": free, "files": len(live), "bitmap": free is not None, "fstype": vol.fstype,
                "mode": "tabla de asignación" if cluster_fs else "bloques"}
        self.volume_info.append(info)
        self._ev("Volumen «%s» (%s): %d archivos, %s de %s%s." % (
            info["label"] or "sin nombre", vol.fstype.upper() if vol.fstype != "?" else "sin sistema de archivos reconocido",
            len(live), "clusters" if cluster_fs else "bloques de búsqueda", _fmt_b(fs.cs),
            ", %s libres" % _fmt_b(free * fs.cs) if free is not None else ""))
        if not cluster_fs:
            self._ev("Sin mapa de asignación legible: se buscarán cabeceras en todos los bloques y el hash del manifiesto "
                     "decide. Se asume que cada archivo está contiguo (los fragmentados no se pueden seguir aquí).", "warn")
        elif free is None:
            self._ev("No se pudo leer el mapa de asignación: se usarán los archivos existentes para saber qué está "
                     "ocupado (el diagnóstico de datos sobrescritos será menos preciso).", "warn")
        self.stage = "mhl"
        self.phase = "leyendo manifiestos"
        recs = self._load_mhls(src, live)
        for m in self.mhl_sources:
            if m not in self._logged_sources:
                self._logged_sources.append(m)
                self._ev("Manifiesto leído: %s (%d entradas)" % (m["path"], m["entries"]))
        for e in self.mhl_errors:
            if e not in self._logged_errors:
                self._logged_errors.append(e)
                self._ev("Manifiesto ilegible: " + e, "warn")
        if not self.mhl_sources:
            self._ev("No se encontró ningún manifiesto de hashes (MHL, .md5, .sha1…) en el volumen ni en las rutas indicadas.", "warn")
        missing = self.include_missing or not tree or not live
        if missing and not self.include_missing:
            self._ev("Sin árbol de archivos legible: se buscarán todos los archivos del manifiesto.", "warn")
        before = len(self.unresolved)
        targets = [t for t in self._targets(live, recs, missing) if t.entry.path.lower() not in self._target_keys]
        self._target_keys |= {t.entry.path.lower() for t in targets}
        self.targets_total = len(self._target_keys)
        for u in self.unresolved[before:]:
            self._ev("%s: %s" % (u["name"], u["reason"]), "warn")
        if not targets:
            if not recs and not tree:
                self.summary = ("Este volumen no tiene un árbol de archivos que RecoFix pueda leer: para rescatar archivos por "
                                "su cabecera hace falta un manifiesto de hashes (MHL o lista md5/sha/xxhash). Añádelo y repite.")
            return
        needs_hash = {t.rec["algo"] for t in targets if t.rec}
        if needs_hash & set(XXH) and not tool("xxhsum"):
            self._ev("Los manifiestos usan xxHash64 pero xxhsum no está instalado: los archivos se copiarán igualmente, "
                     "pero sin comprobar el hash. Instálalo con «brew install xxhash».", "warn")
        self._ev("Archivos a rescatar: %d (%s)." % (
            len(targets), ", ".join(t.name for t in targets[:6]) + (" …" if len(targets) > 6 else "")))
        owned = self._owned(fs, live)
        windows = self._windows(fs, live, targets)
        for a, b in windows:
            self._ev("Zona de búsqueda: %s %d–%d (%s)." % ("clusters" if cluster_fs else "bloques", a, b, _fmt_b((b - a) * fs.cs)))
        self.stage = "buscar"
        cands = self._search(src, fs, windows, owned, targets)
        if cands is None:
            return
        self.stage = "emparejar"
        for t in targets:
            self._rate(src, fs, owned, t, cands)
        self._assign(src, fs, targets)
        self._diagnose_gone(src, fs, free)

    # ------------------------------------------------------------------ manifests

    def _load_mhls(self, src, live):
        found = []
        for p in self.mhl_paths if self._ext_found is None else ():
            paths = []
            if os.path.isdir(p):
                for root, _, names in os.walk(p):
                    paths += [os.path.join(root, n) for n in names if n.lower().endswith(mhl.LIST_EXTS) and not n.startswith("._")]
            elif os.path.isfile(p):
                paths.append(p)
            else:
                self.mhl_errors.append("%s: no existe" % p)
            for q in paths:
                try:
                    found.append((q, mhl.parse_any(q)))
                except Exception as e:
                    self.mhl_errors.append("%s: %s" % (q, e))
        if self._ext_found is None:
            self._ext_found = list(found)
        else:
            found = list(self._ext_found)
        for e in live:
            name = e.path.rsplit("/", 1)[-1]
            if not name.lower().endswith(mhl.LIST_EXTS) or name.startswith("._") or not e.extents or not 0 < e.size <= 16 << 20:
                continue
            try:
                data = e.resident[:e.size] if e.resident is not None else \
                    b"".join(src.read(off, ln) for off, ln in e.extents if off is not None)[:e.size]
                found.append((e.path, mhl.parse_any(data)))
            except Exception as ex:
                self.mhl_errors.append("%s: %s" % (e.path, ex))
        by_name = {}
        for source, recs in found:
            if not any(m["path"] == source for m in self.mhl_sources):
                self.mhl_sources.append({"path": source, "entries": len(recs)})
            for r in recs:
                by_name.setdefault(r["file"].lower(), []).append(r)
        return by_name

    @staticmethod
    def _expected(path, name, by_name):
        """(record, problem) for the file at ``path``: the manifest entry that describes it."""
        cands = by_name.get(name.lower())
        if not cands:
            return None, ""
        norm = path.strip("/").lower()
        pool = [r for r in cands if r["path"] and norm.endswith(r["path"].strip("/").lower())] or cands
        return _pick(pool)

    def _targets(self, live, by_name, missing=False):
        """Files to rescue: entries at 0 bytes that a manifest describes (plus, with ``missing``, manifest files absent
        from the volume)."""
        targets = []

        def add(e, name, rec):
            ext = name.rsplit(".", 1)[-1].lower() if "." in name.lstrip(".") else ""
            if rec is not None and rec["size"] == 0:
                return  # the manifest says it really is empty
            if rec is None and ext not in EXT_HANDLERS:
                return  # no manifest and not camera media: an empty file is more likely legitimate
            if not handlers_for(ext):
                self.unresolved.append(dict(issue("no_handler", "no hay reconocimiento de cabecera para archivos .%s" % ext),
                                            name=name, path=e.path, size=rec["size"] if rec else None))
                return
            targets.append(Target(entry=e, name=name, ext=ext, folder=e.path.rsplit("/", 1)[0] or "/", rec=rec,
                                  size=rec["size"] if rec else None))

        present = set()
        for e in live:
            name = e.path.rsplit("/", 1)[-1]
            if e.size > 0:
                present.add(name.lower())
            if e.size != 0 or name.startswith("._") or e.origin == "orphan":
                continue
            rec, problem = self._expected(e.path, name, by_name)
            if problem:
                self.unresolved.append(dict(issue("mhl_conflict", problem), name=name, path=e.path, size=None))
                continue
            add(e, name, rec)
        if missing:
            have = {e.path.rsplit("/", 1)[-1].lower() for e in live if e.size == 0}
            for key, recs in sorted(by_name.items()):
                if key in present or key in have or key.startswith("._"):
                    continue
                r, problem = _pick(recs)
                if problem:
                    self.unresolved.append(dict(issue("mhl_conflict", problem), name=recs[0]["file"],
                                                path="/" + recs[0]["file"], size=None))
                    continue
                path = "/" + (r["path"].strip("/") or r["file"])
                add(FileEntry(path, 0, False, [], "", "ausente del volumen", origin="missing"), r["file"], r)
        return targets

    # ------------------------------------------------------------------ where to look

    @staticmethod
    def _owned(fs, live):
        """Clusters that live entries (files, folders) own: a clip cannot start or continue there."""
        ranges = [(fs.root, fs.root + 1)]
        for c in getattr(fs, "_dirs_seen", ()):
            ranges.append((c, c + 1))
        for e in live:
            for off, ln in e.extents or ():
                if off is not None and ln > 0:
                    ranges.append((fs.cluster_of(off), fs.cluster_of(off + ln - 1) + 1))
        return Interval(ranges)

    def _windows(self, fs, live, targets):
        """Cluster ranges worth reading: around each folder's live files, widened by what is missing there."""
        top = fs.n_clusters + 2
        if self.cluster_range:
            a, b = self.cluster_range
            a, b = max(2, min(a, b)), min(top, max(a, b) + 1)
            if a >= b:
                raise ValueError("El rango de clusters está fuera del volumen (el volumen tiene %d clusters)." % fs.n_clusters)
            return [(a, b)]
        if self.wide:
            return [(2, top)]
        by_folder = {}
        for e in live:
            if e.extents and e.size > 0 and e.extents[0][0] is not None:
                f = e.path.rsplit("/", 1)[0] or "/"
                a = fs.cluster_of(e.extents[0][0])
                by_folder.setdefault(f, []).append((a, a + -(-e.size // fs.cs)))
        wins = []
        for folder in {t.folder for t in targets}:
            mine = [t for t in targets if t.folder == folder]
            missing = sum(-(-t.size // fs.cs) if t.size else 1 << 16 for t in mine)
            sib = by_folder.get(folder)
            if not sib:
                self._ev("La carpeta %s no tiene archivos sanos que sirvan de referencia: se buscará en todo el volumen." % folder, "warn")
                return [(2, top)]  # nothing nearby to anchor the search: use the whole volume
            pad = missing + max(1024, missing // 10)
            wins.append((max(2, min(a for a, _ in sib) - pad), min(top, max(b for _, b in sib) + pad)))
        return _merge(wins)

    def _search(self, src, fs, windows, owned, targets):
        """Header candidates at cluster starts of every window; None if cancelled.

        Small clusters are read in big sequential chunks and scanned with ``bytes.find`` (a full read is faster than one
        seek per cluster); big clusters (camera disks use 256 KiB) read only the first bytes of each cluster.
        """
        keys = {h.key for t in targets for h in handlers_for(t.ext)}
        sigs = [(off, magic, h) for h in HANDLERS if h.key in keys for off, magic in h.signatures]
        total = sum(b - a for a, b in windows)
        unit = "bloque" if isinstance(fs, Flat) else "cluster"
        self.phase = "buscando cabeceras"
        self.bytes_total, self.bytes_done = total * fs.cs, 0
        cands, done, t0, d0 = [], 0, time.time(), 0
        for a, b in windows:
            chunked = fs.cs <= 65536 and b - a >= 64
            step = max(1, CHUNK // fs.cs) if chunked else 512
            c = a
            while c < b:
                if self._checkpoint():
                    return None
                n = min(step, b - c)
                self.phase = "buscando cabeceras"
                self.current_path = "%s %d" % (unit, c)
                self.bytes_done = done * fs.cs
                self.progress = done / max(1, total)
                dt = time.time() - t0
                if dt > 1:
                    self.speed, t0, d0 = (done - d0) * fs.cs / dt, time.time(), done
                hits = []
                if chunked:
                    buf = src.read(fs.cl_off(c), n * fs.cs)
                    for sig_off, magic, h in sigs:
                        i = buf.find(magic)
                        while i >= 0:
                            st = i - sig_off
                            if st >= 0 and st % fs.cs == 0:
                                hits.append((c + st // fs.cs, h))
                            i = buf.find(magic, i + 1)
                else:
                    for k in range(c, c + n):
                        if owned.has(k):
                            continue
                        head = src.read(fs.cl_off(k), HEAD)
                        hits += [(k, h) for sig_off, magic, h in sigs if head[sig_off:sig_off + len(magic)] == magic]
                for k, h in sorted(hits, key=lambda x: x[0]):
                    if owned.has(k):
                        continue
                    off = fs.cl_off(k)
                    try:
                        f = h.probe(src, off)
                    except Exception:
                        f = None
                    cands.append({"cluster": k, "off": off, "key": h.key, "found": f})
                    if len(cands) <= 120:
                        nm = (f.meta.get("nombre_paquete") if f else "") or "sin nombre legible"
                        self._ev("Cabecera %s en espacio no asignado, %s %d (%s)." % (h.key.upper(), unit, k, nm))
                done += n
                c += n
        if len(cands) > 120:
            self._ev("… y %d cabeceras más (no se listan todas)." % (len(cands) - 120))
        self._ev("Búsqueda terminada: %d cabeceras candidatas en %s recorridos." % (len(cands), _fmt_b(total * fs.cs)))
        self.progress, self.bytes_done = 1.0, total * fs.cs
        return cands

    # ------------------------------------------------------------------ matching

    def _rate(self, src, fs, owned, t, cands):
        """Candidates compatible with ``t``, each with how well it fits (rank), name match and overlap."""
        keys = {h.key for h in handlers_for(t.ext)}
        t.need = -(-t.size // fs.cs) if t.size else None
        stem = _stem(t.name)
        for c in cands:
            if keys and c["key"] not in keys:
                continue
            f = c["found"]
            pkg = _stem((f.meta.get("nombre_paquete") if f else "") or "")
            name_ok = bool(pkg) and pkg == stem
            if t.size:
                if c["cluster"] + t.need > fs.n_clusters + 2:
                    continue
                if f is None:
                    rank, length = 1, None
                elif not f.complete:
                    rank, length = 2, f.length
                elif f.length == t.size:
                    rank, length = 4, f.length
                elif 0 < t.size - f.length <= PAD_MAX and not src.read(c["off"] + f.length, t.size - f.length).strip(b"\0"):
                    rank, length = 3, f.length  # only zero bytes follow the structure: trailing padding
                else:
                    continue  # the structure ends somewhere the MHL does not allow
                need = t.need
            else:
                if not (f and f.complete):
                    continue  # without a size only a structurally complete file can be measured
                if not (name_ok or t.rec):
                    continue  # no manifest at all: only a same-name file is trustworthy
                # with a hash but no size the hash decides, so every complete file of the right format is a candidate
                rank, length, need = (4 if name_ok and not t.rec else 3 if name_ok else 2), f.length, -(-f.length // fs.cs)
            t.cands.append({"cluster": c["cluster"], "off": c["off"], "key": c["key"], "rank": rank, "name_ok": name_ok,
                            "length": length, "need": need, "found": f,
                            "overlap": owned.overlap(c["cluster"], c["cluster"] + need)})

    # -- layout of a clip on disk: FAT chain (fragmented) or contiguous

    def _runs_of(self, fs, first, need):
        key = (first, need)
        if key not in self._runs_cache:
            self._runs_cache[key] = fs.chain_runs(first, need)
        return self._runs_cache[key]

    @staticmethod
    def _extents(fs, runs, size):
        """Absolute byte extents of ``runs`` trimmed to ``size`` bytes."""
        out, left = [], size
        for c, n in runs:
            ln = min(n * fs.cs, left)
            if ln <= 0:
                break
            out.append((fs.cl_off(c), ln))
            left -= ln
        return out

    def _assign(self, src, fs, targets):
        self.phase = "emparejando"
        taken = Interval([])
        for t in sorted(targets, key=lambda x: len(x.cands)):
            if self._checkpoint():
                return
            self.current_path = t.entry.path

            def free_of_others(c):
                runs, _ = self._runs_of(fs, c["cluster"], c["need"])
                return not any(taken.overlap(a, a + n) for a, n in (runs or [(c["cluster"], c["need"])]))
            avail = [c for c in t.cands if free_of_others(c)]
            if not avail:
                if t.cands:
                    self._miss(t, "all_taken", "hay cabeceras, pero todas chocan con otros archivos ya emparejados", len(t.cands))
                elif not t.rec:
                    self._miss(t, "no_mhl", "no figura en ningún manifiesto: sin tamaño ni hash no hay forma de reconocer su "
                                            "cabecera (indica el .mhl del rodaje)", 0)
                elif self.wide or self.cluster_range or isinstance(fs, Flat):
                    self._miss(t, "no_header_all", "no se encontró ninguna cabecera compatible en " + (
                        "el rango indicado" if self.cluster_range else "todo el volumen"), 0)
                else:
                    self._miss(t, "no_header_near", "no se encontró ninguna cabecera compatible cerca de los demás "
                                                    "archivos de su carpeta (prueba la búsqueda en todo el espacio libre)", 0)
                continue
            hash_only = bool(t.rec) and not t.size
            if hash_only:
                order = sorted(avail, key=lambda c: (c["rank"], c["name_ok"]), reverse=True)[:MAX_HASH_ONLY]
                pick, how, code = self._tiebreak(src, fs, t, order, force=True)
            else:
                best = max((c["rank"], c["name_ok"]) for c in avail)
                top = [c for c in avail if (c["rank"], c["name_ok"]) == best]
                pick, how, code = (top[0], "", "") if len(top) == 1 else self._tiebreak(src, fs, t, top)
            if pick is None:
                self._miss(t, code, how, len(avail) if hash_only else len(top))
                continue
            layout = self._layout(src, fs, t, pick)
            taken = Interval([(a, a + n) for a, n in layout["runs"]] + list(zip(taken.starts, taken.ends)))
            self._item(fs, t, pick, how, layout)

    def _check_layouts(self, src, fs, t, c):
        """Hash a candidate under every layout it can have: the FAT chain first, then plain contiguous.

        Returns ``(True, runs)`` on a match, ``(False, None)`` if none matches and ``(None, None)`` when this hash
        cannot be computed here (algorithm unsupported or xxhsum missing)."""
        size = t.size or c["length"]
        runs, _ = self._runs_of(fs, c["cluster"], c["need"])
        cont = [(c["cluster"], c["need"])]
        options = ([runs] if runs and runs != cont else []) + [cont]
        for lay in options:
            ok = self._verify_extents(src, self._extents(fs, lay, size), size, t.rec)
            if ok is None:
                return None, None
            if ok:
                return True, lay
        return False, None

    def _tiebreak(self, src, fs, t, top, force=False):
        """Several candidates: hash them against the manifest; else only a unique least-overlap one wins.

        ``force`` (no size in the manifest): the hash is the only proof, so it must match or nothing is returned.
        Returns (candidate | None, explanation, code-when-None)."""
        if t.rec and (t.size or force) and (len(top) <= MAX_VERIFY or force):
            self.phase = "verificando candidatos"
            self._ev("%s: %d cabeceras candidatas; se comparan con el hash del manifiesto." % (t.name, len(top)))
            any_checked = False
            for i, c in enumerate(top):
                self.current_path = "%s · candidato %d de %d" % (t.name, i + 1, len(top))
                ok, runs = self._check_layouts(src, fs, t, c)
                any_checked = any_checked or ok is not None
                if ok:
                    c["verified"], c["runs"] = True, runs
                    return c, "elegido entre %d candidatos porque su hash coincide con el manifiesto" % len(top), ""
            if any_checked:
                return None, "ninguno de los %d candidatos coincide con el hash del manifiesto" % len(top), "hash_none"
            if force:
                return None, ("no se puede comprobar el hash (%s) en este equipo: sin él no se puede reconocer el archivo"
                              % t.rec["algo"]), "hash_none"
        low = min(c["overlap"] for c in top)
        least = [c for c in top if c["overlap"] == low]
        if len(least) == 1 and low < max(c["overlap"] for c in top):
            return least[0], "elegido entre %d candidatos por ser el único sin clusters ocupados" % len(top), ""
        return None, "%d candidatos igual de buenos y no se pudo desempatar con el hash" % len(top), "ambiguous"

    def _layout(self, src, fs, t, pick):
        """Where the file's clusters really are: follow the FAT chain left by deletion, check it against the hash.

        A file written without fragmentation has no chain (entries are 0) and comes out as one run. When the chain
        shows several runs the hash decides between "chain" and "contiguous"; if neither matches, the chain is kept
        (it survived) and the file is reported as unverified.
        """
        first, need = pick["cluster"], pick["need"]
        runs, issues = self._runs_of(fs, first, need)
        cont = [(first, need)]
        out = {"runs": runs or cont, "issues": list(issues), "verified": True if pick.get("verified") else None,
               "how": "contiguo"}
        if pick.get("runs"):  # already proven by the hash while choosing the candidate
            lay = pick["runs"]
            out.update(runs=lay, how="cadena FAT (%d tramos)" % len(lay) if len(lay) > 1 else "contiguo")
            if len(lay) > 1:
                self._ev("%s: la cadena FAT lo reparte en %d tramos (%s)." % (
                    t.name, len(lay), ", ".join("%d+%d" % r for r in lay[:5]) + (" …" if len(lay) > 5 else "")))
            return out
        if not runs or runs == cont:
            if issues:
                self._ev("%s: %s." % (t.name, "; ".join(issues)), "warn")
            return out
        out["how"] = "cadena FAT (%d tramos)" % len(runs)
        self._ev("%s: la cadena FAT lo reparte en %d tramos (%s)." % (
            t.name, len(runs), ", ".join("%d+%d" % r for r in runs[:5]) + (" …" if len(runs) > 5 else "")))
        if issues:
            self._ev("%s: %s." % (t.name, "; ".join(issues)), "warn")
        if not (t.rec and (t.size or pick["length"])):
            return out
        self.phase = "verificando disposición"
        ok, lay = self._check_layouts(src, fs, t, pick)
        if ok is None:
            self._ev("%s: no se puede comprobar el hash de este manifiesto aquí; se usa la cadena FAT." % t.name, "warn")
        elif ok:
            same = lay == runs
            out.update(runs=list(lay), verified=True, how=out["how"] if same else "contiguo")
            self._ev("%s: el hash coincide con la disposición «%s»." % (t.name, "cadena FAT" if same else "contiguo"))
        else:
            out["verified"] = False
            self._ev("%s: ni la cadena FAT ni la lectura contigua dan el hash del manifiesto: faltan datos (sobrescritos)." % t.name, "warn")
        return out

    def _verify_extents(self, src, extents, size, rec):
        """True/False if the bytes of ``extents`` hash to the MHL value, None if this algorithm can't be checked."""
        key = (tuple(extents), rec["algo"], rec["hash"])
        if key in self._verified:
            return self._verified[key]
        algo = rec["algo"]
        proc = None
        if algo in XXH:
            exe = tool("xxhsum")
            if not exe:
                return None
            proc = subprocess.Popen([exe, "-H1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            feed = proc.stdin.write
        elif algo in HASHLIB:
            h = hashlib.new(algo)
            feed = h.update
        else:
            return None
        try:
            pos = 0
            for off, ln in extents:
                left = ln
                while left > 0:
                    if self.cancel.is_set():
                        return None
                    b = src.read(off + ln - left, min(HASH_CHUNK, left))
                    if not b:
                        self._verified[key] = False
                        return False
                    feed(b)
                    pos += len(b)
                    left -= len(b)
                    self.current_path = "verificando %.0f%%" % (100 * pos / max(1, size))
            if proc:
                proc.stdin.close()
                got = (proc.stdout.read().split() or [b""])[0].decode()
                proc.wait()
            else:
                got = h.hexdigest()
            res = got.lower() == rec["hash"].lower()
            self._verified[key] = res
            return res
        except BrokenPipeError:
            return None
        finally:
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()

    def _verify(self, src, off, size, rec):
        """Contiguous convenience wrapper kept for callers that hash one extent."""
        return self._verify_extents(src, [(off, size)], size, rec)

    # ------------------------------------------------------------------ diagnosis

    def _gaps(self, fs, runs, size):
        """Parts of the clip, as (offset, length) inside the file, whose clusters belong to other files today."""
        out, pos = [], 0
        for c0, n in runs:
            used = fs.used_in_runs([(c0, n)])
            if used is None:
                return None
            for u0, un in used:
                off = pos + (u0 - c0) * fs.cs
                ln = min(un * fs.cs, size - off)
                if ln <= 0:
                    continue
                if out and out[-1][0] + out[-1][1] == off:
                    out[-1] = (out[-1][0], out[-1][1] + ln)
                else:
                    out.append((off, ln))
            pos += n * fs.cs
        return out

    def _diagnose_gone(self, src, fs, free):
        """If clips are missing, sample the free space: all zeros means the data is gone for good, not just unfound."""
        pending = [u for u in self.unresolved if u["code"] in ("no_header_near", "no_header_all", "hash_none")]
        if not pending or self.cancel.is_set():
            return
        self.phase = "comprobando el espacio libre"
        self._ev("Comprobando si el espacio libre conserva datos (muestreo de %d puntos)…" % SURVEY_SAMPLES)
        top = fs.n_clusters + 2
        step = max(1, (top - 2) // SURVEY_SAMPLES)
        sampled = zeros = 0
        for c in range(2 + step // 2, top, step):
            if fs.is_used(c):
                continue
            sampled += 1
            if not any(src.read(fs.cl_off(c), 4096)):
                zeros += 1
        data = sampled - zeros
        self.survey_info = {"sampled": sampled, "zeros": zeros, "data": data}
        if sampled < 20:
            return
        self._ev("Espacio libre: %d de %d muestras tienen datos, %d están a ceros." % (data, sampled, zeros))
        if data == 0:
            for u in pending:
                u.update(issue("data_gone", "el espacio libre del disco está a ceros: los datos ya no existen en él"))
            self._ev("Todo el espacio libre está a ceros: los clips pendientes no se pueden recuperar de este disco.", "error")

    # ------------------------------------------------------------------ results

    def _miss(self, t, code, why, n):
        self._ev("%s: sin resolver — %s" % (t.name, why), "warn")
        self.unresolved.append(dict(issue(code, why), name=t.name, path=t.entry.path, size=t.size, candidates=n))

    def _item(self, fs, t, c, how, layout):
        size = t.size or c["length"]
        f = c["found"]
        runs = layout["runs"]
        verified = layout["verified"]
        gaps = self._gaps(fs, runs, size)
        busy = sum(-(-ln // fs.cs) for _, ln in gaps) if gaps is not None else c["overlap"]
        free = fs.allocated_count(c["cluster"], c["need"]) if len(runs) == 1 else None
        notes = [{4: "la estructura del formato coincide con el tamaño del MHL",
                  3: "la estructura del formato queda %d bytes por debajo del MHL (relleno final)" % (size - (c["length"] or 0)),
                  2: "cabecera válida pero sin cierre: se verificará con el hash",
                  1: "solo la firma del formato coincide: se verificará con el hash"}[c["rank"]]]
        if not t.size and t.rec:
            notes[0] = "sin tamaño en el manifiesto: se midió con la estructura del archivo y se reconoció por su hash"
        if c["name_ok"]:
            notes.append("el nombre guardado dentro del archivo coincide")
        if how:
            notes.append(how)
        if len(runs) > 1:
            notes.append("clip fragmentado: se reconstruye siguiendo la cadena FAT (%d tramos)" % len(runs))
        for i in layout["issues"]:
            notes.append(i)
        if verified:
            notes.append("el hash del MHL coincide con esta disposición")
        if busy:
            notes.append("%d clusters (%d KiB) de este rango pertenecen hoy a otros archivos: el clip saldrá con un "
                         "hueco y el hash no coincidirá" % (busy, busy * fs.cs // 1024))
        elif verified is False:
            notes.append("el hash del MHL no coincide con ninguna disposición: faltan datos o el MHL es de otra copia")
        if not t.rec:
            notes.append("sin MHL: tamaño tomado de la estructura del archivo, no verificado")
        conf = {4: 0.95, 3: 0.85, 2: 0.6, 1: 0.4}[c["rank"]] + (0.04 if c["name_ok"] else 0) - (0.3 if busy else 0)
        if verified:
            conf = max(conf, 0.99)
        if verified is False:
            conf = min(conf, 0.5)
        meta = {"ajuste": RANK_LABEL[c["rank"]], "cluster_inicial": c["cluster"], "clusters": c["need"],
                "clusters_ocupados": busy, "disposicion": layout["how"],
                "hash_en_busqueda": "coincide" if verified else "no coincide" if verified is False else "no comprobado"}
        if free is not None:
            meta["clusters_libres_segun_bitmap"] = c["need"] - free
        if f is not None:
            meta.update({k: v for k, v in f.meta.items() if k in ("codec", "ancho", "alto", "fps", "duracion_s", "timecode",
                                                                   "nombre_paquete")})
        if gaps:
            dur = meta.get("duracion_s")
            meta["huecos"] = [{"offset": o, "bytes": ln,
                               "segundo": round(o / size * dur, 1) if isinstance(dur, (int, float)) and dur else None}
                              for o, ln in gaps[:20]]
            meta["problema"] = issue("overwritten", "parte del clip fue sobrescrita")
        if t.rec:
            meta["mhl"] = {"algo": t.rec["algo"], "hash": t.rec["hash"], "size": t.rec["size"], "file": t.rec["file"]}
        extents = self._extents(fs, runs, size)
        complete = bool(t.rec) and not busy and verified is not False and (c["rank"] >= 3 or bool(verified))
        self._resolved.append(t.name)
        self._ev("%s: localizado en el %s %d (%s)%s." % (
            t.name, "bloque" if isinstance(fs, Flat) else "cluster", c["cluster"], layout["how"],
            ", verificado con el hash del manifiesto" if verified else ""), "ok" if complete else "warn")
        self._add(group="zeroed", name=t.name, path=t.entry.path, ext=t.ext, category=category_of(t.name), size=size,
                  start=extents[0][0], extents=extents, complete=complete,
                  confidence=max(0.05, min(0.99, conf)), fmt=c["key"], meta=meta, note="; ".join(notes),
                  mtime=t.entry.mtime, fs=getattr(fs.parsed, "name", "") if isinstance(fs, Flat) and fs.parsed else (fs.kind if isinstance(fs, Flat) else fs.name))


def _pick(recs):
    """(record, problem) from several manifest entries for one file.

    The same file listed twice (an MHL with sizes plus a plain md5 list) is fine: the entry with a size wins. Two
    different hashes for the same algorithm, or two different sizes, are a real contradiction."""
    by_algo = {}
    for r in recs:
        by_algo.setdefault(r["algo"], set()).add(r["hash"])
    if any(len(v) > 1 for v in by_algo.values()) or len({r["size"] for r in recs if r["size"] is not None}) > 1:
        return None, "los manifiestos traen datos distintos para este nombre"
    return next((r for r in recs if r["size"] is not None), recs[0]), ""


def _merge(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# ---------------------------------------------------------------------- pre-check (before scanning)

def preflight(path, mhl_paths=None, volume_index=None, include_missing=False):
    """What a rescue would find, without searching: volumes, files to rescue, manifest coverage, warnings.

    Quick (reads the folder tree and the manifests only). Lets the UI tell the user, before the long search, whether
    the source is suitable, which files are candidates, which have a hash, and what is missing to rescue the rest.
    """
    sc = ZeroedScan(path, mhl_paths=mhl_paths, volume_index=volume_index, include_missing=include_missing)
    out = {"source": path, "volumes": [], "targets": [], "unresolved": [], "mhl_files": [], "mhl_errors": [],
           "warnings": [], "tools": {"xxhsum": bool(tool("xxhsum"))}, "verdict": "nada", "advice": ""}
    try:
        with Source(path) as src:
            vols = volumes(src) or [Volume(0, src.size, "?", scheme="sin particiones")]
            for i, v in enumerate(vols):
                out["volumes"].append({"index": i, "fstype": v.fstype, "label": v.label, "size": v.size,
                                       "usable": v.fstype in CLUSTER_FS or v.size >= MIN_FLAT,
                                       "mode": "tabla de asignación" if v.fstype in CLUSTER_FS else "bloques"})
            if volume_index is not None and not 0 <= volume_index < len(vols):
                raise ValueError("El volumen %s no existe: el origen tiene %d." % (volume_index, len(vols)))
            pool = vols if volume_index is None else [vols[volume_index]]
            usable = [v for v in pool if v.fstype in CLUSTER_FS or v.size >= MIN_FLAT]
            if not usable:
                out["verdict"] = "error"
                out["advice"] = "El origen no contiene ningún volumen utilizable (solo particiones muy pequeñas o vacías)."
                return out
            for v in usable:
                fs, parsed, cluster_fs, live, tree = sc._open(src, v)
                free = fs.free_count()
                info = {"label": getattr(fs, "label", "") or v.label, "fstype": v.fstype, "cluster_size": fs.cs,
                        "clusters": fs.n_clusters, "free_bytes": None if free is None else free * fs.cs,
                        "files": len(live), "tree": tree, "mode": "tabla de asignación" if cluster_fs else "bloques"}
                out["volume"] = out.get("volume") or info
                recs = sc._load_mhls(src, live)
                missing = include_missing or not tree or not live
                targets = sc._targets(live, recs, missing)
                folders_with_files = {e.path.rsplit("/", 1)[0] for e in live if e.size > 0}
                for t in targets:
                    out["targets"].append({
                        "name": t.name, "path": t.entry.path, "folder": t.folder, "size": t.size,
                        "has_mhl": t.rec is not None, "algo": t.rec["algo"] if t.rec else None,
                        "near_ok": cluster_fs and t.folder in folders_with_files, "absent": t.entry.origin == "missing",
                        "hash_only": t.rec is not None and not t.size, "volume": info["label"] or v.fstype})
                if v.size > 2 << 40:
                    out["warnings"].append("El volumen es grande (%.1f TB): recorrerlo entero puede tardar muchas horas. Usa la búsqueda "
                                           "«junto a los demás archivos» o limita la búsqueda a un rango." % (v.size / (1 << 40)))
                if not cluster_fs:
                    out["warnings"].append("Volumen %s: sin mapa de asignación legible. Se buscará por bloques y se asumirá "
                                           "que los archivos están contiguos; el hash del manifiesto decide." % (v.label or (v.fstype.upper() if v.fstype != "?" else "sin sistema reconocido")))
                elif free is None:
                    out["warnings"].append("No se pudo leer el mapa de asignación del volumen: el diagnóstico de datos "
                                           "sobrescritos será menos preciso.")
    except PermissionError as e:
        out["verdict"] = "error"
        out["advice"] = friendly_error(str(e))
        return out
    except Exception as e:
        out["verdict"] = "error"
        out["advice"] = friendly_error("%s: %s" % (type(e).__name__, e))
        return out
    out["mhl_files"], out["mhl_errors"], out["unresolved"] = sc.mhl_sources, sc.mhl_errors, sc.unresolved
    with_mhl = [t for t in out["targets"] if t["has_mhl"]]
    without = [t for t in out["targets"] if not t["has_mhl"]]
    if any(t["algo"] in XXH for t in with_mhl) and not out["tools"]["xxhsum"]:
        out["warnings"].append("Los manifiestos usan xxHash64 y xxhsum no está instalado: los archivos se copiarán pero sin "
                               "comprobar el hash. Instálalo con «brew install xxhash».")
    if without:
        out["warnings"].append("%d archivos no figuran en ningún manifiesto: añade el .mhl (o lista de hashes) del rodaje o "
                               "no se podrán reconocer." % len(without))
    if any(t["hash_only"] for t in with_mhl):
        out["warnings"].append("Algunos manifiestos no traen el tamaño (listas md5/sha): se reconocen solo por su hash, lo que "
                               "obliga a comprobar cada cabecera candidata y puede tardar más.")
    if any(not t["near_ok"] and not t["absent"] for t in with_mhl) and any(v["mode"] == "tabla de asignación" for v in out["volumes"]):
        out["warnings"].append("Alguna carpeta no tiene archivos sanos de referencia: la búsqueda recorrerá todo el volumen (más lenta).")
    if out["mhl_errors"]:
        out["warnings"].append("%d manifiestos no se pudieron leer." % len(out["mhl_errors"]))
    if not out["targets"] and not out["unresolved"]:
        out["verdict"] = "nada"
        if out.get("volume") and not out["volume"]["tree"] and not out["mhl_files"]:
            out["advice"] = ("Este volumen no tiene un árbol de archivos legible: para rescatar por cabecera hace falta un "
                             "manifiesto de hashes (MHL o lista md5/sha/xxhash). Añádelo en el paso 2.")
            out["verdict"] = "sin_mhl"
        else:
            out["advice"] = ("No hay archivos a 0 bytes que figuren en un manifiesto de hashes: no hay nada que reconstruir. "
                             "Si faltan archivos del manifiesto en el disco, activa «Buscar también los archivos que no aparecen». "
                             "Para recuperar archivos borrados usa «Discos y ubicaciones».")
    elif with_mhl:
        out["verdict"] = "listo" if not without and not out["unresolved"] else "parcial"
        out["advice"] = "%d archivos pueden buscarse con su hash%s." % (
            len(with_mhl), "; el resto necesita su manifiesto" if (without or out["unresolved"]) else "")
    else:
        out["verdict"] = "sin_mhl"
        out["advice"] = ("Hay archivos a 0 bytes pero ningún manifiesto los describe. Añade el .mhl o la lista de hashes del "
                         "rodaje (archivo o carpeta) para poder reconocerlos.")
    return out
