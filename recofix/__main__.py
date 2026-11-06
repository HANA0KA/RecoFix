"""RecoFix command line.

  python3 -m recofix serve [--port 8765] [--no-browser]
  python3 -m recofix scan ORIGEN [--rapido] [--json] [--tipos video,photo] [--carpeta /DCIM]
  python3 -m recofix recover ORIGEN DESTINO [--grupo deleted] [--categoria video] [--ext mxf] [--rapido]
  python3 -m recofix zeroed ORIGEN [DESTINO] [--mhl RUTA ...] [--todo-el-volumen]
  python3 -m recofix partitions ORIGEN
  python3 -m recofix image ORIGEN SALIDA.img
  python3 -m recofix reassemble ORIGEN OFFSET SALIDA.mp4 [--cluster 32768]
  python3 -m recofix repair-video CLIP [--muestra SAMPLE] [--destino DIR]
  python3 -m recofix repair-photo FOTO [--muestra SAMPLE] [--destino DIR]
  python3 -m recofix repair-doc DOC [--destino DIR]
  python3 -m recofix trash
  python3 -m recofix smart
  python3 -m recofix mhl CARPETA
"""
import argparse
import json
import os
import sys
import time


def _human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u) if u != "B" else "%d B" % n
        n /= 1024


def _scan(a):
    from .scanner import Scan, run_in_thread
    s = Scan(a.origen, deep=not a.rapido, categories=a.tipos.split(",") if getattr(a, "tipos", None) else None,
             scope=getattr(a, "carpeta", None))
    t = run_in_thread(s)
    while t.is_alive():
        st = s.status()
        sys.stderr.write("\r%-28s %5.1f%%  %s/s  encontrados %d   " % (
            st["phase"][:28], 100 * st["progress"], _human(st["speed"]), st["found"]))
        time.sleep(0.5)
    sys.stderr.write("\n")
    if s.error:
        sys.exit("error: " + s.error)
    return s


def _print(r):
    print(json.dumps(r, indent=1, ensure_ascii=False, default=str))
    if not r.get("ok", True):
        sys.exit(1)


def _zeroed(a):
    from .recover import Recovery
    from .scanner import run_in_thread
    from .zeroed import ZeroedScan, preflight
    if a.diagnostico:
        d = preflight(a.origen, mhl_paths=a.mhl, include_missing=a.ausentes)
        print(d["advice"])
        if d.get("volume"):
            v = d["volume"]
            print("Volumen «%s»: %d archivos, clusters de %d B, %d MHL leídos" % (v["label"], v["files"], v["cluster_size"], len(d["mhl_files"])))
        for t in d["targets"]:
            print("  %-40s %s  MHL: %s" % (t["name"][:40], _human(t["size"]) if t["size"] else "?", "sí" if t["has_mhl"] else "FALTA"))
        for w in d["warnings"]:
            print("AVISO:", w)
        if d["verdict"] not in ("listo", "parcial"):
            sys.exit(1)
        return
    rng = None
    if a.rango:
        try:
            lo, hi = (int(x) for x in a.rango.split("-"))
        except ValueError:
            sys.exit("error: --rango debe ser A-B con dos números de cluster (p. ej. 29000000-29700000)")
        rng = (lo, hi)
    s = ZeroedScan(a.origen, mhl_paths=a.mhl, search="all" if a.todo_el_volumen else "near", cluster_range=rng,
                   include_missing=a.ausentes)
    t = run_in_thread(s)
    while t.is_alive():
        st = s.status()
        sys.stderr.write("\r%-28s %5.1f%%  encontrados %d   " % (st["phase"][:28], 100 * st["progress"], st["found"]))
        time.sleep(0.5)
    sys.stderr.write("\n")
    if s.error:
        sys.exit("error: " + s.error)
    for e in s.mhl_errors:
        print("aviso MHL:", e)
    for it in s.items:
        print("%-12s %10s  @%-14d %s  [%s]" % (it.name[:40], _human(it.size), it.start, it.path, it.note))
    for u in s.unresolved:
        print("SIN RESOLVER %s: %s" % (u["path"], u["reason"]))
        if u.get("fix"):
            print("    qué hacer: " + u["fix"])
    if s.summary:
        print(s.summary)
    if not a.destino:
        return
    r = Recovery(a.origen, s.items, a.destino)
    r.run()
    if r.error:
        sys.exit("error: " + r.error)
    for x in r.results:
        print("%-5s %-34s mhl=%s %s" % ("OK" if x["ok"] else "FALLO", os.path.basename(x["path"]), x.get("mhl", "-"), x["note"]))
    bad = sum(1 for x in r.results if x.get("estado") == "no_verificado")
    if bad:
        print("AVISO: %d clips no coinciden con el MHL y se guardaron como _NO_VERIFICADO (faltan datos sobrescritos)" % bad)
    ok = sum(1 for x in r.results if x["ok"])
    verified = sum(1 for x in r.results if x.get("mhl") == "OK")
    print("recuperados %d/%d en %s (%d verificados con su hash del MHL)" % (ok, len(r.results), a.destino, verified))
    if ok != len(r.results) or s.unresolved:
        sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="recofix", description="RecoFix · recuperación y reparación de datos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--token")
    for name in ("scan", "recover"):
        p = sub.add_parser(name)
        p.add_argument("origen")
        if name == "recover":
            p.add_argument("destino")
            p.add_argument("--grupo", choices=["existing", "deleted", "orphan", "raw"])
            p.add_argument("--categoria")
            p.add_argument("--ext")
        else:
            p.add_argument("--json", action="store_true")
        p.add_argument("--rapido", action="store_true", help="solo sistema de archivos")
        p.add_argument("--tipos", help="categorías: video,photo,audio,document,archive,other")
        p.add_argument("--carpeta", help="solo entradas bajo esta carpeta del volumen")
    p = sub.add_parser("zeroed", help="rescate de clips vaciados (0 bytes) cuyos datos siguen en el espacio libre (exFAT + MHL)")
    p.add_argument("origen")
    p.add_argument("destino", nargs="?", help="si se indica, recupera los clips encontrados aquí (otro disco)")
    p.add_argument("--mhl", action="append", default=[], help="manifiesto de hashes: .mhl, .md5, .sha1, .sha256, .xxh64 o carpeta que los contenga (se usan además de los del volumen)")
    p.add_argument("--rango", help="limitar la búsqueda a clusters A-B (p. ej. 29000000-29700000)")
    p.add_argument("--ausentes", action="store_true", help="buscar también los archivos del manifiesto que no aparecen en el disco")
    p.add_argument("--diagnostico", action="store_true", help="solo analizar el volumen y explicar qué se puede rescatar")
    p.add_argument("--todo-el-volumen", action="store_true", help="buscar en todo el volumen, no solo junto a los demás archivos de la carpeta")
    p = sub.add_parser("partitions")
    p.add_argument("origen")
    p = sub.add_parser("image")
    p.add_argument("origen")
    p.add_argument("salida")
    p = sub.add_parser("reassemble")
    p.add_argument("origen")
    p.add_argument("offset", type=int)
    p.add_argument("salida")
    p.add_argument("--cluster", type=int, default=32768)
    for name in ("repair-video", "repair-photo", "repair-doc"):
        p = sub.add_parser(name)
        p.add_argument("archivo")
        p.add_argument("--destino")
        if name != "repair-doc":
            p.add_argument("--muestra")
    sub.add_parser("trash")
    sub.add_parser("smart")
    p = sub.add_parser("mhl")
    p.add_argument("carpeta")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from .server import serve
        serve(a.port, not a.no_browser, a.token)
    elif a.cmd == "scan":
        s = _scan(a)
        if a.json:
            json.dump([it.to_dict() for it in s.items], sys.stdout, indent=1, ensure_ascii=False)
            return
        for it in s.items:
            if it.category != "system":
                print("%-8s %-6s %10s  @%-14d %s%s" % (it.group, it.ext, _human(it.size), it.start, it.path or it.name,
                                                       ("  [" + it.note + "]") if it.note else ""))
    elif a.cmd == "recover":
        from .recover import Recovery
        s = _scan(a)
        items = [it for it in s.items if it.category != "system" and (not a.grupo or it.group == a.grupo)
                 and (not a.categoria or it.category == a.categoria) and (not a.ext or it.ext == a.ext.lower())]
        r = Recovery(a.origen, items, a.destino)
        r.run()
        if r.error:
            sys.exit("error: " + r.error)
        print("recuperados %d/%d en %s" % (sum(1 for x in r.results if x["ok"]), len(r.results), a.destino))
    elif a.cmd == "zeroed":
        _zeroed(a)
    elif a.cmd == "partitions":
        from .fs import volumes
        from .partitions import find_lost
        from .source import Source
        with Source(a.origen) as src:
            vs = volumes(src)
            for v in vs:
                print("tabla    %-6s @%-14d %12s %s" % (v.fstype, v.offset, _human(v.size), v.label))
            for v in find_lost(src, vs):
                print("PERDIDA  %-6s @%-14d %12s" % (v.fstype, v.offset, _human(v.size)))
    elif a.cmd == "image":
        from .imaging import Imager
        im = Imager(a.origen, a.salida)
        im.run()
        _print(im.status())
    elif a.cmd == "reassemble":
        from .reassembly import reassemble
        _print(reassemble(a.origen, a.offset, a.salida, cluster=a.cluster))
    elif a.cmd == "repair-video":
        from .repair import video
        _print(video.advanced_repair(a.archivo, a.muestra, a.destino) if a.muestra else video.quick_repair(a.archivo, a.destino))
    elif a.cmd == "repair-photo":
        from .repair import photo
        _print(photo.repair(a.archivo, a.muestra, a.destino))
    elif a.cmd == "repair-doc":
        from .repair import document
        _print(document.repair(a.archivo, a.destino))
    elif a.cmd == "trash":
        from .modes import trash
        r = trash.list_items()
        for e in r["errors"]:
            print("aviso:", e)
        for it in r["items"]:
            print("%-30s %10s  %s  ← %s" % (it["bin"][:30], _human(it["size"]), it["name"], it["original"] or "?"))
    elif a.cmd == "smart":
        from . import devices, smart
        for r in smart.all_disks(devices.list_devices()):
            print("%-12s %-28s %-12s %s" % (r["disk"], r["model"][:28], r["verdict"], "; ".join(r["warnings"])))
    elif a.cmd == "mhl":
        from . import mhl
        mhls, files = [], []
        for root, _, names in os.walk(a.carpeta):
            for n in names:
                (mhls if n.lower().endswith(".mhl") else files).append(os.path.join(root, n))
        for row in mhl.verify(mhls, files):
            print("%-14s %-40s %s" % (row["status"], row["file"], row.get("recovered") or ""))


if __name__ == "__main__":
    main()
