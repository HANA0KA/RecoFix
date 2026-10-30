"""Format registry."""
from . import audio, docs, images, misc, mxf, video

HANDLERS = [mxf.MXFHandler()] + video.HANDLERS + images.HANDLERS + audio.HANDLERS + docs.HANDLERS + misc.HANDLERS
BY_KEY = {h.key: h for h in HANDLERS}


def probe_any(r, off, keys=None):
    """Try every handler (or ``keys``) whose signature matches at ``off``."""
    best = None
    for h in HANDLERS:
        if keys and h.key not in keys:
            continue
        for sig_off, magic in h.signatures:
            if r.read(off + sig_off, len(magic)) == magic:
                try:
                    f = h.probe(r, off)
                except Exception:
                    f = None
                if f and (best is None or f.confidence > best.confidence):
                    best = f
                break
    return best


def supported():
    """Summary for the UI: handler name, category, main extension."""
    return [{"key": h.key, "name": h.name, "category": h.category, "ext": h.ext} for h in HANDLERS]
