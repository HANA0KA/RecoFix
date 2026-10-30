"""Crashed-computer kit (Recoverit "System Crash Recovery" / "Create A Bootable USB").

Recoverit downloads a bootable WinPE/Linux ISO (``config.ini [bootable]
drpe.iso``). On a Mac that no longer boots the native, supported route is
macOS Recovery (Cmd-R / hold the power button on Apple silicon), which has a
Terminal but no Python. RecoFix therefore writes to a USB drive:

* ``rescate.sh`` — POSIX shell using only tools present in macOS Recovery
  (``diskutil``, ``dd``, ``ditto``, ``df``): copy the user folders, or make a
  raw image of the internal disk, onto the USB drive.
* ``RecoFix/`` — a copy of RecoFix to run on a working Mac afterwards (scan
  the image, or the crashed Mac's disk via "Compartir disco"/Target Disk Mode).
* ``LEEME.txt`` — step-by-step guide.
"""
import os
import shutil

from .util import chown_user

SCRIPT = r"""#!/bin/sh
# RecoFix · rescate desde macOS Recovery (Terminal). Solo lectura sobre el disco interno.
USB="$(cd "$(dirname "$0")" && pwd)"
OUT="$USB/Rescate_$(date +%Y%m%d_%H%M%S)"
echo "RecoFix · kit de rescate"
echo "Unidad de rescate: $USB"
echo
diskutil list internal physical
echo
echo "1) Copiar carpetas de usuario (Escritorio, Documentos, Descargas, Imágenes, Películas)"
echo "2) Crear imagen completa de un disco (para escanearla después con RecoFix)"
echo "3) Salir"
printf "Opción: "; read OPT
case "$OPT" in
1)
  for V in /Volumes/*; do
    if [ -d "$V/Users" ] && [ "$V" != "$USB" ]; then
      echo "Volumen con usuarios: $V"
      if [ -n "$(ls "$V/Users" 2>/dev/null)" ]; then :; else
        echo "  (vacío o cifrado: desbloquéalo con Utilidad de Discos y vuelve a ejecutar)"; fi
      for U in "$V"/Users/*; do
        N="$(basename "$U")"; [ "$N" = "Shared" ] && continue
        for F in Desktop Documents Downloads Pictures Movies; do
          if [ -d "$U/$F" ]; then
            echo "  Copiando $N/$F ..."
            mkdir -p "$OUT/$N"
            ditto "$U/$F" "$OUT/$N/$F" 2>>"$OUT/errores.log"
          fi
        done
      done
    fi
  done
  echo "Hecho. Copias en $OUT (errores en errores.log si los hay)."
  ;;
2)
  printf "Identificador del disco a copiar (p. ej. disk0): "; read D
  case "$D" in disk[0-9]*) ;; *) echo "Identificador no válido"; exit 1;; esac
  FREE=$(df -k "$USB" | awk 'NR==2{print $4*1024}')
  SIZE=$(diskutil info "$D" | awk -F'[()]' '/Disk Size/{split($2,a," "); print a[1]; exit}')
  echo "Tamaño del disco: $SIZE bytes · libre en la unidad: $FREE bytes"
  if [ -n "$SIZE" ] && [ "$SIZE" -gt "$FREE" ]; then echo "No cabe en la unidad de rescate."; exit 1; fi
  mkdir -p "$OUT"
  echo "Creando $OUT/$D.img (puede tardar horas; Ctrl+T muestra el progreso)..."
  dd if="/dev/r$D" of="$OUT/$D.img" bs=4m conv=noerror,sync
  echo "Imagen terminada. En otro Mac: RecoFix › Imagen de disco › Usar imagen › $D.img"
  ;;
*) exit 0;;
esac
"""

README = """RecoFix · Kit de rescate para un Mac que no arranca
====================================================

A) Desde macOS Recovery (el Mac roto)
  1. Conecta esta unidad USB al Mac que no arranca.
  2. Arranca en Recuperación:
       - Apple silicon: mantén pulsado el botón de encendido hasta ver «Opciones».
       - Intel: Cmd (⌘) + R al encender.
  3. Si el disco está cifrado (FileVault), ábrelo en Utilidad de Discos › Montar.
  4. Menú Utilidades › Terminal y escribe:
         sh /Volumes/{label}/rescate.sh
  5. Elige 1 (copiar carpetas de usuario) o 2 (imagen completa del disco).

B) Con otro Mac (recomendado si el disco tiene errores)
  - Apple silicon: en Recuperación, Utilidades › Compartir disco; conecta ambos Macs por USB-C/Thunderbolt.
  - Intel: arranca el Mac roto pulsando T (modo disco de destino).
  El disco aparece en el Mac sano: abre RecoFix (carpeta RecoFix de esta unidad o tu instalación)
  y escanéalo, o crea primero una imagen de disco desde RecoFix › Imagen de disco.

RecoFix nunca escribe en el disco que recupera. Guarda siempre lo recuperado en otra unidad.
"""


def make_kit(volume, app_root=None):
    """Write the rescue kit to the root of a mounted volume (USB drive)."""
    volume = os.path.realpath(volume)
    if not volume.startswith("/Volumes/") or volume.count("/") != 2:
        raise ValueError("Elige la raíz de una unidad externa montada en /Volumes")
    st = os.statvfs(volume)
    if st.f_bavail * st.f_frsize < 64 << 20:
        raise ValueError("La unidad necesita al menos 64 MB libres")
    label = os.path.basename(volume)
    script = os.path.join(volume, "rescate.sh")
    with open(script, "w", newline="\n") as f:
        f.write(SCRIPT)
    os.chmod(script, 0o755)
    with open(os.path.join(volume, "LEEME.txt"), "w") as f:
        f.write(README.replace("{label}", label))
    app_root = app_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dst = os.path.join(volume, "RecoFix")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(app_root, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "_work", "build", "dist",
                                                                   ".claude", "*.img", "*.raw"))
    for p in (script, os.path.join(volume, "LEEME.txt"), dst):
        chown_user(p)
    return {"ok": True, "volume": volume, "files": ["rescate.sh", "LEEME.txt", "RecoFix/"]}
