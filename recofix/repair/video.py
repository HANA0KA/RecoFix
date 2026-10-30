"""Corrupted video repair.

* **Quick repair** (Recoverit "Quick Repair"): remux with ffmpeg ignoring
  errors, regenerate timestamps and the index; MXF clips get a closed footer
  partition + RIP.
* **Advanced repair** (Recoverit "Advanced Repair" with a *sample video*):
  for MP4/MOV whose index (``moov``) is missing or broken. The sample —
  another clip from the same camera and settings — provides the codec
  configuration; RecoFix walks the damaged ``mdat`` validating every
  H.264/HEVC NAL unit or ProRes frame, skips interleaved audio and junk, and
  rebuilds a playable file. LPCM audio is recovered too.
"""
import mmap
import os
import re
import shutil
import subprocess
import tempfile

from ..formats import mxf
from ..util import chown_user, tool, unique
from . import mp4

FFMPEG = tool("ffmpeg")
FFPROBE = tool("ffprobe")
MAX_GAP = 64 << 20


def probe(path):
    """Health report: container/streams from ffprobe plus decode errors in the first seconds."""
    rep = {"path": path, "size": os.path.getsize(path), "playable": False, "streams": [], "errors": []}
    if not FFPROBE:
        rep["errors"].append("ffprobe no está instalado (brew install ffmpeg)")
        return rep
    import json
    p = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                       capture_output=True, text=True, timeout=120)
    try:
        d = json.loads(p.stdout or "{}")
    except ValueError:
        d = {}
    rep["format"] = (d.get("format") or {}).get("format_long_name", "")
    rep["duration"] = float((d.get("format") or {}).get("duration") or 0)
    for s in d.get("streams", []):
        rep["streams"].append({"type": s.get("codec_type"), "codec": s.get("codec_name"),
                               "width": s.get("width"), "height": s.get("height"),
                               "fps": s.get("avg_frame_rate"), "channels": s.get("channels"),
                               "frames": s.get("nb_frames")})
    if p.stderr.strip():
        rep["errors"] += p.stderr.strip().splitlines()[:10]
    if any(s["type"] == "video" for s in rep["streams"]) and FFMPEG:
        q = subprocess.run([FFMPEG, "-v", "error", "-xerror", "-t", "5", "-i", path, "-map", "0:v:0", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=300)
        rep["playable"] = q.returncode == 0
        if q.stderr.strip():
            rep["errors"] += q.stderr.strip().splitlines()[:10]
    return rep


def _out(path, dest, suffix):
    base = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1] or ".mp4"
    d = dest or os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    return unique(os.path.join(d, base + suffix + ext))


def quick_repair(path, dest=None):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mxf":
        out = _out(path, dest, "_reparado")
        shutil.copyfile(path, out)
        if mxf.needs_finalize(out):
            rep = mxf.finalize(out)
            chown_user(out)
            return {"ok": True, "out": out, "method": "MXF: footer y RIP reconstruidos", "detail": rep}
        os.remove(out)
    if not FFMPEG:
        return {"ok": False, "error": "ffmpeg no está instalado (brew install ffmpeg)"}
    out = _out(path, dest, "_reparado")
    cmd = [FFMPEG, "-v", "error", "-y", "-fflags", "+genpts+discardcorrupt+igndts", "-err_detect", "ignore_err",
           "-i", path, "-map", "0", "-c", "copy", "-ignore_unknown"]
    if ext in (".mp4", ".mov", ".m4v", ".3gp"):
        cmd += ["-movflags", "+faststart"]
    p = subprocess.run(cmd + [out], capture_output=True, text=True, timeout=3600)
    if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 1024:
        if os.path.exists(out):
            os.remove(out)
        msg = p.stderr.strip().splitlines()[-1:] or ["ffmpeg no pudo leer el archivo"]
        hint = " Prueba la reparación avanzada con un vídeo de muestra." if "moov atom not found" in p.stderr else ""
        return {"ok": False, "error": msg[0] + hint, "needs_sample": "moov atom not found" in p.stderr}
    chown_user(out)
    return {"ok": True, "out": out, "method": "remux con índice y marcas de tiempo regenerados", "check": probe(out)}


# ---------------------------------------------------------------- advanced (sample-based)

_AVC_CAND = re.compile(rb"\x00[\x00-\x3f][\x00-\xff]{2}[\x01-\x17\x21-\x37\x41-\x57\x61-\x77]", re.S)
_HEVC_CAND = re.compile(rb"\x00[\x00-\x3f][\x00-\xff]{2}[\x00-\x7f][\x01-\x07]", re.S)


def _mdat_range(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        tops = mp4.top_level(f, size)
        f.seek(0)
        head = f.read(1 << 16)
    for typ, off, hdr, n in tops:
        if typ == b"mdat":
            return off + hdr, min(size, off + n) if n > hdr else size
    i = head.find(b"mdat")
    if i >= 4:
        return i + 4, size
    return 0, size


def _starts_here(data, p, track, kind, end):
    if kind == "prores":
        return data[p + 4:p + 8] == b"icpf"
    chain = mp4.nal_chain(data, p, track.nal_len, kind, 3)
    if len(chain) >= 2:
        return True
    return bool(chain) and chain[0][2] and chain[0][3] and mp4.plausible_slice(data, p, track.nal_len, kind)


def _resync(data, pos, track, kind, end, hints=()):
    """Next offset >= pos where a run of valid samples/NAL units starts."""
    for c in hints:  # interleaved audio chunks usually have the size they had in the sample clip
        if pos - 1 + c < end and _starts_here(data, pos - 1 + c, track, kind, end):
            return pos - 1 + c
    if kind == "prores":
        i = data.find(b"icpf", pos + 4, end)
        return i - 4 if i >= 0 else -1
    rx = _AVC_CAND if kind == "avc" else _HEVC_CAND
    for m in rx.finditer(data, pos, end):
        p = m.start() - (4 - track.nal_len) if track.nal_len < 4 else m.start()
        if _starts_here(data, p, track, kind, end):
            return p
    return -1


def scan_samples(data, start, end, track, audio=None):
    """Walk a damaged mdat: [(offset, size, key)] video samples and [(offset, length)] gaps between them."""
    kind = "prores" if track.codec in mp4.PRORES else "avc" if track.codec in mp4.AVC else \
        "hevc" if track.codec in mp4.HEVC else None
    if not kind:
        raise ValueError("códec de la muestra no soportado en reparación avanzada: %s" % track.codec.decode("latin-1"))
    samples, gaps = [], []
    pos = start
    gap_start = None
    audio_chunks = []
    if audio is not None and audio.chunk_bytes:
        from collections import Counter
        audio_chunks = [c for c, _ in Counter(audio.chunk_bytes).most_common(4) if c]
    while pos < end:
        if kind == "prores":
            n = int.from_bytes(data[pos:pos + 4], "big")
            if data[pos + 4:pos + 8] == b"icpf" and 8 < n <= end - pos:
                if gap_start is not None:
                    gaps.append((gap_start, pos - gap_start))
                    gap_start = None
                samples.append((pos, n, True))
                pos += n
                continue
        else:
            chain = mp4.nal_chain(data, pos, track.nal_len, kind, 4096)
            if chain:
                if gap_start is not None:
                    gaps.append((gap_start, pos - gap_start))
                    gap_start = None
                cur_off, cur_len, cur_key, has_vcl = pos, 0, False, False
                for off, ln, vcl, start_pic, key in chain:
                    if has_vcl and (start_pic or not vcl):
                        samples.append((cur_off, cur_len, cur_key))
                        cur_off, cur_len, cur_key, has_vcl = off, 0, False, False
                    cur_len += ln
                    cur_key = cur_key or key
                    has_vcl = has_vcl or vcl
                if has_vcl:
                    samples.append((cur_off, cur_len, cur_key))
                elif samples:
                    o, s, k = samples[-1]
                    samples[-1] = (o, s + cur_len, k)  # trailing SEI/param sets belong to the next frame; keep bytes
                pos = chain[-1][0] + chain[-1][1]
                continue
        if gap_start is None:
            gap_start = pos
        nxt = _resync(data, pos + 1, track, kind, min(end, pos + MAX_GAP), audio_chunks)
        if nxt < 0:
            break
        pos = nxt
    if gap_start is not None and pos > gap_start:
        gaps.append((gap_start, min(pos, end) - gap_start))
    return samples, gaps, kind, audio_chunks


def advanced_repair(path, sample, dest=None, progress=None):
    """Rebuild a clip whose index is lost, using a healthy ``sample`` from the same camera."""
    tracks = mp4.read_tracks(sample)
    video = next((t for t in tracks if t.handler == b"vide"), None)
    if not video:
        return {"ok": False, "error": "la muestra no tiene pista de vídeo"}
    audio = next((t for t in tracks if t.handler == b"soun"), None)
    start, end = _mdat_range(path)
    f = open(path, "rb")
    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)  # clips can be many GB: never load them whole
    try:
        return _advanced(path, data, start, min(end, len(data)), video, audio, dest, progress)
    finally:
        data.close()
        f.close()


def _advanced(path, data, start, end, video, audio, dest, progress):
    samples, gaps, kind, _ = scan_samples(data, start, end, video, audio)
    if progress:
        progress(0.7)
    if len(samples) < 2:
        return {"ok": False, "error": "no se encontraron fotogramas válidos con la configuración de la muestra "
                                      "(¿misma cámara, códec y resolución?)"}
    pcm_ok = bool(audio and audio.pcm and audio.sample_size)
    audio_gaps = [(o, n - n % audio.sample_size) for o, n in gaps if n >= audio.sample_size] if pcm_ok else []
    out = _out(path, dest, "_reparado")
    fps_num = video.timescale
    fps_den = video.delta or 1
    info = {"frames": len(samples), "keyframes": sum(1 for s in samples if s[2]), "codec": video.codec.decode("latin-1"),
            "resolution": "%dx%d" % (video.width, video.height), "fps": round(fps_num / fps_den, 3),
            "audio": ("PCM %d canales" % audio.channels) if audio_gaps else
                     ("no recuperado (%s)" % audio.codec.decode("latin-1") if audio else "sin audio en la muestra"),
            "skipped_bytes": sum(n for _, n in gaps)}
    if kind == "prores":
        _write_mov(data, out, video, samples, audio if audio_gaps else None, audio_gaps)
    else:
        err = _mux_annexb(data, out, video, samples, audio if audio_gaps else None, audio_gaps, kind)
        if err:
            return {"ok": False, "error": err, "detail": info}
    chown_user(out)
    if progress:
        progress(1.0)
    return {"ok": True, "out": out, "method": "reconstrucción con vídeo de muestra", "detail": info, "check": probe(out)}


def _write_mov(data, out, video, samples, audio, audio_gaps):
    """New MOV: ftyp + mdat (copied sample data, contiguous) + rebuilt moov."""
    ftyp = mp4.box(b"ftyp", b"qt  " + b"\0\0\x02\0" + b"qt  ")
    body = []
    for o, n, k in samples:
        body.append(("v", o, n, k))
    for o, n in audio_gaps:
        body.append(("a", o, n, False))
    body.sort(key=lambda x: x[1])
    total = sum(x[2] for x in body)
    big = total + 16 > 0xFFFFFFFF
    mdat_hdr = (16 if big else 8)
    with open(out, "wb") as w:
        w.write(ftyp)
        w.write(mp4.struct.pack(">I4sQ", 1, b"mdat", total + 16) if big else mp4.struct.pack(">I4s", total + 8, b"mdat"))
        pos = len(ftyp) + mdat_hdr
        vs, ac = [], []
        for kind, o, n, k in body:
            w.write(data[o:o + n])
            if kind == "v":
                vs.append((pos, n, k))
            else:
                ac.append((pos, n // audio.sample_size))
            pos += n
        traks = []
        vdur = len(vs) * (video.delta or 1)
        traks.append((mp4.trak_for(video, 1, mp4.stbl_for(video, vs), vdur, 1000), vdur * 1000 // video.timescale))
        if audio and ac:
            adur = sum(k for _, k in ac)
            traks.append((mp4.trak_for(audio, 2, mp4.stbl_for(audio, None, ac), adur, 1000),
                          adur * 1000 // max(1, audio.timescale)))
        w.write(mp4.moov(traks))


def _mux_annexb(data, out, video, samples, audio, audio_gaps, kind):
    """H.264/HEVC: Annex B elementary stream with the sample's parameter sets, muxed by ffmpeg."""
    if not FFMPEG:
        return "ffmpeg no está instalado (brew install ffmpeg)"
    tmp = tempfile.mkdtemp(prefix="recofix_rep_")
    try:
        es = os.path.join(tmp, "video." + ("h264" if kind == "avc" else "hevc"))
        sc = b"\0\0\0\1"
        with open(es, "wb") as w:
            for i, (o, n, key) in enumerate(samples):
                if key or i == 0:
                    for p in video.params:
                        w.write(sc + p)
                pos, end = o, o + n
                while pos + video.nal_len <= end:
                    ln = int.from_bytes(data[pos:pos + video.nal_len], "big")
                    w.write(sc + data[pos + video.nal_len:pos + video.nal_len + ln])
                    pos += video.nal_len + ln
        cmd = [FFMPEG, "-v", "error", "-y", "-fflags", "+genpts", "-r", "%d/%d" % (video.timescale, video.delta or 1),
               "-f", "h264" if kind == "avc" else "hevc", "-i", es]
        if audio:
            pcm = os.path.join(tmp, "audio.raw")
            with open(pcm, "wb") as w:
                for o, n in audio_gaps:
                    w.write(data[o:o + n])
            cmd += ["-f", audio.pcm, "-ar", str(audio.rate), "-ac", str(audio.channels), "-i", pcm]
        cmd += ["-map", "0:v"] + (["-map", "1:a"] if audio else []) + ["-c", "copy"]
        if kind == "hevc":
            cmd += ["-tag:v", "hvc1"]
        p = subprocess.run(cmd + [out], capture_output=True, text=True, timeout=7200)
        if p.returncode != 0:
            return "ffmpeg: " + (p.stderr.strip().splitlines() or ["error"])[-1]
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
