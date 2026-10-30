# Wondershare Recoverit 15.0.1.3 (macOS) — informe de ingeniería inversa

Análisis estático de la app instalada en `/Applications/Recoverit.app`, realizado el
2026-10-30 con REA (Reverse Engineer Anything) y utilidades nativas de macOS
(`plutil`, `strings`, `nm`, `codesign`). **No se ejecutó Recoverit**: todo lo que
sigue sale de los binarios y recursos tal y como se distribuyen.

Leyenda de confianza:

* **Observado** — leído literalmente en el binario o en un recurso.
* **Inferido** — deducción razonable a partir de lo observado; no verificado en ejecución.
* **Desconocido** — no se pudo determinar con análisis estático.

Los identificadores `ev_…` son registros de evidencia de la sesión REA (firma de
`Recoverit` y Mach-O de `libscanopreator`). La exportación del ledger falló por
tiempo de espera del proveedor, así que no se adjunta; la evidencia reproducible
está en [`rea/drengsrv-protobuf.txt`](rea/drengsrv-protobuf.txt) (descriptores
protobuf decodificados) y en los comandos citados en cada sección. Las búsquedas
de cadenas/clases con el proveedor profundo de REA superaron el tiempo límite en
binarios de >10 MB; esa parte se hizo con `strings`/`nm` sobre los mismos archivos.

## 1. Identidad del artefacto

| Dato | Valor | Fuente |
|---|---|---|
| Lanzador | `com.wondershare.recoveritloader` 15.0.1.3 (`Contents/MacOS/Recoverit.app` anidada) | Info.plist — observado |
| App real | `com.wondershare.recoverit`, Mach-O arm64, SHA-256 `f30c879a…f7b7` | REA `ev_50290aa7…` |
| Firma | Developer ID Application: Wondershare Technology Group Co.,LTD (YZC2T44ZDX), hardened runtime, sin entitlements | REA `ev_50290aa7…` |
| Tamaño | 334 MB | observado |

## 2. Arquitectura de procesos

```
Recoverit (UI AppKit/Swift, ~260 nibs)
   │  gRPC + protobuf  (/dr_engine_service.DrEngineService/*)
   ▼
drengsrv (motor, se lanza como root vía dr_helper.xpc)
   ├─ libscanopreator   operadores de escaneo + parsers de formato
   ├─ libdrstoragedevice enumeración de discos, particiones perdidas, SSH/SFTP (libssh2 1.10)
   ├─ libdrcore / libdatarecovery / libdatamanage   árbol de resultados, filtros, guardado
   ├─ librepaircore / WsVideoRepairLib   reparación de vídeo/foto/documento
   ├─ libDataRecoveryAISearchCore + onnxruntime 1.22   etiquetas IA locales
   └─ ffmpeg (libav* 61), libheif, libde265, REDR3D, libmediainfo
drpreview.app      vista previa aislada
DiskSMARTMonitor   login item con smartmontools embebido (smartctl)
WondershareDriver(_m1).kext   acceso a bajo nivel (requiere bajar seguridad en Apple Silicon)
config.ini [bootable] drpe.iso   imagen WinPE/Linux para el "PC bloqueado"
```

*Observado*: nombres de binarios, dependencias Mach-O (REA `ev_20d46711…` para
`libscanopreator`: depende de `libfilemanage`, `libdrstoragedevice`,
`libdrfoundation`, `libdrlog`, IOKit, DiskArbitration). *Inferido*: el arranque
del motor como root por el XPC `com.datarecovery.helper`.

## 3. API del motor (protobuf embebido en `drengsrv`)

Se decodificaron los `FileDescriptorProto` serializados dentro de `drengsrv`
(texto completo en [`rea/drengsrv-protobuf.txt`](rea/drengsrv-protobuf.txt)). Es la
especificación más fiable de lo que el motor sabe hacer.

RPCs (observado): `EnumDevices`, `Scan`, `RequestScanData`, `Preview`, `Recover`,
`EngineNotify`, `NotifyExchange`, `TestConnectStatus`, `ThirdCloudAuthorization`.

Campos clave:

* `ScanRequset { device_id, sacn_type, deep_scan_type, scan_file_staus, raw_file_types,
  recycle_scan, scan_path, lost_filename_text, advance_type, device_type, cloud_drive_type }`
  → un escaneo admite: tipo (rápido/profundo), lista de formatos RAW a buscar,
  escaneo de papelera, **ruta concreta** (escaneo de carpeta), nombre de archivo perdido
  y modo avanzado (vídeo).
* `Messagetype`: `StopScan/PauseScan/ResumeScan`, `SectorError/SectorFatal`,
  `BitlockerDriveLocked`, `SaveScanRecord*`/`LoadScanRecord*` (historial),
  `AdvancedScan2ndPhase`, `VideoReassemblyStart`, `LoginICloudSucceed`,
  `AiSceneTagStart`, `HighValueFileRecommendation`, `CurrentScanningPath`.
* `DataType`: árbol por ruta/tipo/fecha, filtros, búsqueda, `RequestSimilarFiles`,
  `RequestSDCardInfo`, `RequestBadSectorCount`, `RequestLocateFile`, `CheckFileDamaged`.
* `dr.FilterCondition { file_type, time_start/end, file_size_min/max, detele_flag
  (borrado/existente/todo), file_name, file_tag, ai_scene_tag }` y
  `FilterStatisticsInfo` (<1 MB, 1–100 MB, 100 MB–1 GB, >1 GB; hoy, ayer, semana, mes, año).
* `RecoverRequest { save_path, filter_info, is_cloud_location }` y
  `LocationType { kLocal, kGoogleColud, kOnedrive }` → destino local o nube.

## 4. Motor de escaneo (`libscanopreator`)

Observado en exportaciones/cadenas:

* `CRawScanOperator`, `CreateRawAnalyser`, `RawHeaderCheck`, `RawHeaderCrackSelectStrategy`
  → escaneo por firmas ("RAW"), con análisis por formato.
* `JpegFragScanOperator` ("simplified two-fragment strategy", "JPEG fragment matching")
  → reensamblado de JPEG fragmentado en dos trozos.
* `MxfContentPackageFragment`, `mp4::FragmentSample`, `MdatStreamAnalyzer`,
  "Created first fragment with accurate offset… samples=[…]", `matchFragmentToContainers`,
  `calculateGapSize` → **reensamblado de vídeo**: se toma el índice de muestras
  (`stsz`/`stco`) y se buscan los trozos de `mdat` que encajan.
* `createFileTypeScanOperatorControl`, `CreateAdvancedScan`, `dir_scan_param`, `drdeepscan`.
* Parsers embebidos: BBC libMXF/libMXF++ (`mxf_read_header_metadata`…), `SonyXmlParser`,
  `PanasonicXmlParser`, `GoProCustomMetaParser`, `DjiCustomMetaParser`, `Mp4UserDataParser`,
  `ExifParser`, `FtypParser`, H.264 slice parser, Poco::Zip, JPEG XL.
* Insta360: "Detected Insta360 file, changing extension from jpg to insp".

Sistemas de archivos (cadenas en `libdrstoragedevice`/`libdrcore`): APFS, HFS+, NTFS,
exFAT, FAT16/32, ext2/3/4, Btrfs; `MacExploreLostPartition(Ex)`, `MacTryGetLostPartition`.

## 5. Formatos

`Resources/extensions.plist` (observado): Photo 201, Document 188, Video 103,
Audio 84, Archive 42, DataBase 27, Email 19, InternetWeb 12; listas `*_Raw`
(los que se encuentran por firma): Photo 43, Document 38, Video 30, Audio 11,
Archive 8, Email 6, Database 4; `VideoAdvanceRecovery` 14.
`VideoRepairSupportFormats.plist`: reparación de documentos (zip, psd, pdf, doc/x,
xls/x, ppt/x, pages, numbers, key, hwp, skp…) y de imagen para IA (png, jpg, webp, bmp).

## 6. Modalidades de la interfaz

Extraídas de `en.lproj/Localizable.strings` (1795 cadenas) y de los nibs:

| Modalidad Recoverit | Evidencia (nib / cadena) |
|---|---|
| Discos duros y ubicaciones (rápido + profundo, Escritorio, Papelera, carpeta) | `FunctionSelectViewCtrl`, `ScanPathSelectViewCtrl`, "Hard Drives and Locations Recovery" |
| Papelera | "Trash", `recycle_scan` |
| Tarjeta SD / cámaras (perfiles Canon, Sony, DJI, GoPro, Insta360, Nikon, Panasonic, Fujifilm, Leica, Olympus, Ricoh) | `SDCardViewCtrl`, "Detects DCIM and PRIVATE camera folders", "… Camera Scan Profile Ready" |
| Recuperación avanzada de vídeo (reensamblado) | `AdvanceVideoRecoverySelectWnd`, `VideoReassemblyTipsViewCtrl`, "… Video Deep Recombination AI Algorithm" |
| Particiones perdidas | "Analysing disk partition", `MacExploreLostPartition` |
| Time Machine | `TimeMachineSelectWndCtrl`, "Select one Time Machine backup file to scan" |
| Imagen de disco (crear / usar) | `CreateImage*ViewCtrl`, `DiskImage*`, "Stop this process and use a disk image for recovery?" |
| NAS y Linux | `NASConnectViewCtrl`, `NASAutoSearchViewCtrl`, `SSHConnector` (libssh2) |
| Ordenador bloqueado (USB de arranque) | `MakeMacPE`, `SystemCrashIntroduceCtrl`, `config.ini [bootable] drpe.iso` |
| iCloud | `ICloud*ViewCtrl`, "Recover files deleted within the past 30 days" |
| Reparación de vídeo (rápida / avanzada con muestra) | `VideoRepairCtrl`, `VideoAdvanceSampleCtrl`, "Add Sample Video to Repair" |
| Reparación de fotos / documentos | `CorruptedFilesRepairCtrl`, "Repair corrupted photos, including raw formats" |
| S.M.A.R.T. | `DeviceSmartViewController`, `DiskSMARTMonitor.app` |
| Sectores dañados | `BadSectorShowWndCtrl`, "Try to skip bad sectors" |
| Historial de escaneos / reanudar | `ScanRecordViewCtrl`, "Incomplete scan detected. Resume scanning?" |
| Recuperar a red / iCloud Drive | `SaveToRemoteDeviceWndCtrl`, "Recover to Network Drive" |
| Filtros, búsqueda, vista previa, fotos similares, IA | `Filter*PanelViewCtrl`, `SimilarPhotoViewCtrl`, `AISearch*` |

## 7. Qué replica RecoFix y qué no

RecoFix reimplementa las **modalidades**, no el código: es un motor propio en Python
(solo biblioteca estándar) más ffmpeg para vista previa y reparación de vídeo.

Diferencias deliberadas:

* Sin kext ni bajar la seguridad del Mac: se lee `/dev/rdisk*` como root.
* Sin licencias, telemetría (QuickTracking/AB test) ni subidas a la nube.
* iCloud: solo la carpeta local "Eliminados recientemente" de iCloud Drive; no se
  inicia sesión en la cuenta de Apple.
* PC bloqueado: kit de rescate en USB con un script de shell para el Terminal de
  macOS Recovery (imagen `dd` / copia `ditto`) y guía de "Compartir disco"; no hay ISO de arranque propia.
* Etiquetas de IA / mejora de fotos con IA: no incluidas.

## 8. Desconocidos

* Algoritmo exacto de "Deep Recombination" (pesos ONNX no analizados).
* Esquema de los archivos de historial de escaneo (formato binario del motor).
* Comportamiento del kext en Apple Silicon (no se cargó).
