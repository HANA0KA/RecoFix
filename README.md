# RecoFix

Recuperación y reparación de datos para macOS con todas las modalidades de
Wondershare Recoverit 15, reimplementadas a partir de un análisis de ingeniería
inversa de la app instalada ([docs/REVERSE_ENGINEERING.md](docs/REVERSE_ENGINEERING.md)).

Motor propio en Python (solo biblioteca estándar) + ffmpeg para vista previa y
vídeo, interfaz web local y app nativa de macOS. **Nunca escribe en el disco
que recupera**: se niega a guardar en el mismo disco físico.

## Abrir

| Forma | Cómo |
|---|---|
| App de macOS | `./scripts/build_app.sh` y abre `dist/RecoFix.app`. Pide la contraseña de administrador para leer discos; si la cancelas funciona con imágenes, carpetas y reparación. |
| Lanzador | Doble clic en `RecoFix.command` (sudo + navegador). |
| Sin privilegios | `python3 -m recofix serve` |

Requisitos: macOS 12+, Python 3.9+ (Homebrew recomendado), `brew install ffmpeg`.
Opcionales: `smartmontools` (atributos S.M.A.R.T.), `xxhash` (hashes xxh64 en el informe).
Para la Papelera y Time Machine, concede «Acceso total al disco» a RecoFix o a Terminal.

## Modalidades

| Recoverit | RecoFix | Cómo funciona |
|---|---|---|
| Discos duros y ubicaciones | Discos y ubicaciones | Rápido: árbol de exFAT/FAT/NTFS con borrados (nombre, carpeta, fecha). Profundo: firmas por sector con tamaño estructural de ~90 formatos. Escaneo de carpeta (`scan_path`). |
| Papelera | Papelera | Papelera del usuario, `.Trashes` de cada volumen e iCloud Drive; restaura a la ubicación original leyendo `ptbL/ptbN` del `.DS_Store`. |
| Tarjeta SD / perfiles de cámara | Tarjeta SD y cámaras | Detecta DCIM, PRIVATE/M4ROOT, CONTENTS, XDROOT, ARRI, RED, BRAW, GoPro, DJI, Insta360… y escanea con el perfil de la marca. |
| — | Rescate de clips vaciados | Para **cualquier disco, tarjeta o imagen** donde archivos grabados completos aparecen con 0 bytes (o han desaparecido) pero sus datos siguen en el disco. Asistente en 4 pasos: origen → manifiesto de hashes → diagnóstico previo → rescate con registro detallado. **exFAT y FAT12/16/32**: usa la tabla de asignación (clusters libres, sobrescritos y la cadena FAT que queda tras el borrado, para reconstruir archivos **fragmentados**). **Cualquier otro sistema (NTFS, APFS, HFS+, ext…) o disco sin sistema legible**: búsqueda por bloques de cabecera. Cada archivo se reconoce con el manifiesto del rodaje o de la copia — MHL (ASC y 1.x), listas `.md5`/`.sha1`/`.sha256`/`.xxh64` (con o sin tamaño) — y se comprueba contra su hash. Cada archivo que no se puede rescatar explica por qué y qué hacer (añadir el manifiesto, buscar en todo el volumen, usar otra copia). |
| Recuperación avanzada de vídeo | Vídeo avanzado | Reensambla MP4/MOV fragmentados: localiza cada fotograma de la tabla de muestras en el disco (H.264, HEVC, ProRes). |
| Particiones perdidas | Particiones perdidas | Busca boot sectors/superbloques (exFAT, FAT, NTFS, APFS, HFS+, ext) en el espacio sin particionar y abre los volúmenes encontrados. |
| Time Machine | Time Machine | Copias e instantáneas APFS locales: explorar, buscar y restaurar. |
| iCloud | iCloud Drive | «Eliminados recientemente» local de iCloud Drive (sin iniciar sesión en la cuenta). |
| NAS y Linux | NAS y Linux | Bonjour + SSH: lista discos con `lsblk`, copia el dispositivo a una imagen local (solo lectura) y la escanea. |
| Crear / usar imagen de disco | Imagen de disco | Copia en dos pasadas (salta zonas dañadas, luego sector a sector) con mapa de sectores ilegibles; abre .img/.dd/.raw/.iso/.dmg. |
| Ordenador bloqueado | Mac que no arranca | Kit USB con `rescate.sh` para el Terminal de macOS Recovery + guía de Compartir disco / modo disco de destino. |
| Reparación de vídeo (rápida / avanzada) | Reparar vídeo | Rápida: remux e índice nuevo, MXF con footer+RIP. Avanzada: reconstrucción con vídeo de muestra (H.264, HEVC, ProRes + audio PCM). |
| Reparación de fotos | Reparar fotos | JPEG truncado, cabecera trasplantada desde foto de muestra, PNG con CRC/IDAT, vista previa de RAW. |
| Reparación de documentos | Reparar documentos | Office/iWork/ODF/EPUB/ZIP desde cabeceras locales, PDF con xref nueva, SQLite `.recover`, texto. |
| S.M.A.R.T. | Salud del disco | `smartctl` si está instalado; si no, estado de macOS. |
| Historial / reanudar | Historial de escaneos | Guarda sesiones `.rfscan`, las reabre sin escanear y reanuda escaneos profundos. |
| Filtros, búsqueda, vista previa | Resultados | Árbol por ruta o tipo, filtros de estado/tipo/tamaño/fecha, búsqueda, miniaturas, vista previa y metadatos. |
| — | Verificar MHL | Comprueba lo recuperado contra los .mhl del rodaje (xxh64/MD5/SHA1). |

Cada recuperación deja `informe_recuperacion_*.json/csv` con el offset de origen y el MD5 (y xxh64) de cada archivo.

## Línea de comandos

```bash
sudo python3 -m recofix scan /dev/rdisk5 --tipos video,photo
```

```bash
sudo python3 -m recofix recover /dev/rdisk5s2 /Volumes/Backup/Recuperado --grupo deleted
```

```bash
python3 -m recofix repair-video clip_roto.mp4 --muestra clip_bueno.mp4
```

```bash
sudo python3 -m recofix zeroed /dev/rdisk5s2 /Volumes/Backup/Recuperado
```

Sin `DESTINO` solo lista lo que encuentra. `--diagnostico` analiza el volumen y explica qué se puede rescatar sin buscar nada. `--mhl RUTA` (repetible) añade archivos o carpetas `.mhl`; `--todo-el-volumen` busca en todo el espacio libre; `--rango A-B` limita la búsqueda a un rango de clusters. Termina con código 1 si algún clip no se encontró o su hash no coincide.

```bash
sudo python3 -m recofix partitions /dev/rdisk4
```

Más: `image`, `reassemble`, `repair-photo`, `repair-doc`, `trash`, `smart`, `mhl` (`python3 -m recofix -h`).

## Arquitectura

```
recofix/
  source.py        lectura de solo lectura, alineada a sector, tolerante a sectores dañados
  formats/         ~90 formatos con tamaño estructural (MXF, MP4/MOV, R3D, BRAW, RAW, Office…)
  fs/              exFAT (incl. entradas huérfanas tras formateo), FAT12/16/32, NTFS
  scanner.py       pase de sistema de archivos + pase profundo; pausa, alcance de carpeta, reanudación
  partitions.py    particiones perdidas
  sessions.py      historial .rfscan
  reassembly.py    vídeo fragmentado
  repair/          mp4 (lectura/escritura de moov), video, photo, document
  modes/           ubicaciones, papelera (.DS_Store), cámaras, Time Machine
  imaging.py       imágenes de disco    remote.py  NAS/Linux por SSH
  smart.py         S.M.A.R.T.           rescue.py  kit de rescate
  zeroed.py        rescate de clips vaciados + MHL        recover.py  escritura segura + informes
  server.py        API local con token
  web/             interfaz
app/main.swift     app nativa (WKWebView) que arranca el motor como administrador
```

La API local escucha solo en 127.0.0.1, exige un token aleatorio por arranque y rechaza otros Host/Origin.

## Tests

```bash
python3 -m unittest tests.test_engine tests.test_modes tests.test_server
```

Generan clips reales con ffmpeg y volúmenes exFAT/FAT32 con `hdiutil` (borrados, formateo rápido, partición perdida),
fragmentan vídeos en un disco simulado y comprueban que el reensamblado es idéntico bit a bit.

## Límites conocidos

* Rescate de clips vaciados: cualquier disco, pero solo los formatos con reconocimiento de cabecera (MXF, MOV/MP4/BRAW, R3D, MKV, AVI/WAV, MPEG, fotos, audio, documentos…), y un archivo se reconstruye si sus datos no se sobrescribieron. Si archivos pequeños
  (p. ej. `._*`) se escribieron encima de parte del clip, se avisa («clusters ocupados») y el hash del MHL no coincidirá:
  la copia se conserva para inspección pero no se da por buena. Sin MHL no hay tamaño ni hash con que reconocer la
  cabecera, y el clip se reporta como sin resolver en lugar de adivinarlo. Los clips que no coinciden con el hash se
  guardan siempre como `<nombre>_NO_VERIFICADO.<ext>`, nunca con el nombre original. Si el espacio libre del disco está
  a ceros (formato seguro, TRIM) los datos ya no existen y RecoFix lo indica. Sin tabla de asignación (NTFS, APFS, HFS+, ext, disco sin sistema) se asume que cada archivo está contiguo: uno fragmentado se reporta como no encontrado, no se adivina. La búsqueda por bloques recorre todo el volumen, así que en discos grandes conviene limitarla con un rango. El soporte NTFS/APFS/HFS+/ext está probado con discos sin sistema, no con imágenes reales de esos formatos; no reconstruye cabeceras a partir de
  otros clips (un archivo así no coincidiría con el original y no sirve como copia de entrega).

* APFS y HFS+ no se recorren como sistema de archivos: sus borrados se recuperan por escaneo profundo (sin nombre),
  o desde Time Machine / instantáneas.
* La reparación avanzada de vídeo recupera audio PCM; el audio AAC entrelazado no se reconstruye.
* iCloud: solo lo que sigue en este Mac; no se accede a la cuenta en la nube.
* Sin etiquetas de IA ni mejora de fotos con IA.
