"""NTFS: walk every MFT record (live and deleted), rebuild paths, decode data runs."""
import datetime
import struct

from . import FileEntry


def runlist(b, p):
    """Decode an NTFS mapping-pairs array into [(lcn or None, clusters)]."""
    out, lcn = [], 0
    while p < len(b) and b[p]:
        h = b[p]
        ln_sz, off_sz = h & 0xF, h >> 4
        p += 1
        if ln_sz == 0 or ln_sz > 8 or off_sz > 8:
            break
        length = int.from_bytes(b[p:p + ln_sz], "little")
        p += ln_sz
        if off_sz:
            delta = int.from_bytes(b[p:p + off_sz], "little", signed=True)
            lcn += delta
            out.append((lcn, length))
        else:
            out.append((None, length))  # sparse
        p += off_sz
    return out


def filetime(v):
    if not v:
        return ""
    try:
        return (datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=v // 10)).strftime("%Y-%m-%d %H:%M:%S")
    except OverflowError:
        return ""


class NTFS:
    name = "NTFS"

    def __init__(self, src, base):
        self.src = src
        self.base = base
        bs = src.read(base, 512)
        if bs[3:11] != b"NTFS    ":
            raise ValueError("no es NTFS")
        self.bps = struct.unpack_from("<H", bs, 11)[0]
        spc = bs[13]
        self.cs = self.bps * (spc if spc <= 0x80 else 1 << (256 - spc))
        mft_lcn = struct.unpack_from("<Q", bs, 48)[0]
        c = struct.unpack_from("<b", bs, 64)[0]
        self.rec = self.cs * c if c > 0 else 1 << -c
        self.label = ""
        first = self._fixup(src.read(base + mft_lcn * self.cs, self.rec))
        if not first:
            raise ValueError("MFT ilegible")
        data = self._attrs(first).get(0x80)
        self.mft_runs = data["runs"] if data and data.get("runs") else [(mft_lcn, 1 << 20)]
        self.mft_size = data["size"] if data else 0

    def _fixup(self, r):
        if len(r) < 48 or r[:4] != b"FILE":
            return None
        uo, un = struct.unpack_from("<HH", r, 4)
        b = bytearray(r)
        usn = r[uo:uo + 2]
        for i in range(1, un):
            end = i * self.bps - 2
            if end + 2 > len(b):
                break
            if b[end:end + 2] != usn:
                return None  # torn record
            b[end:end + 2] = r[uo + 2 * i:uo + 2 * i + 2]
        return bytes(b)

    def _attrs(self, r):
        out = {}
        p = struct.unpack_from("<H", r, 20)[0]
        while p + 16 <= len(r):
            typ, ln = struct.unpack_from("<II", r, p)
            if typ == 0xFFFFFFFF or ln < 16 or p + ln > len(r):
                break
            nonres, name_len = r[p + 8], r[p + 9]
            if typ == 0x30 and not nonres:
                vo = struct.unpack_from("<H", r, p + 20)[0]
                v = r[p + vo:p + ln]
                if len(v) >= 66:
                    ns = v[65]
                    name = v[66:66 + 2 * v[64]].decode("utf-16-le", "replace")
                    parent = int.from_bytes(v[0:6], "little")
                    prev = out.get(0x30)
                    if not prev or prev["ns"] == 2:  # prefer Win32/POSIX names over DOS 8.3
                        out[0x30] = {"name": name, "parent": parent, "ns": ns,
                                     "mtime": struct.unpack_from("<Q", v, 16)[0]}
            elif typ == 0x10 and not nonres:
                vo = struct.unpack_from("<H", r, p + 20)[0]
                out[0x10] = {"mtime": struct.unpack_from("<Q", r, p + vo + 8)[0]}
            elif typ == 0x20:
                out[0x20] = True  # attribute list: data may live in extension records
            elif typ == 0x80 and name_len == 0:
                if nonres:
                    ro = struct.unpack_from("<H", r, p + 32)[0]
                    size = struct.unpack_from("<Q", r, p + 48)[0]
                    start_vcn = struct.unpack_from("<Q", r, p + 16)[0]
                    if start_vcn == 0:
                        out[0x80] = {"runs": runlist(r[p:p + ln], ro), "size": size}
                else:
                    vl, vo = struct.unpack_from("<IH", r, p + 16)
                    out[0x80] = {"resident": r[p + vo:p + vo + vl], "size": vl}
            elif typ == 0x60 and not nonres:
                vl, vo = struct.unpack_from("<IH", r, p + 16)
                out[0x60] = r[p + vo:p + vo + vl].decode("utf-16-le", "replace")
            p += ln
        return out

    def _mft_records(self):
        """Yield (index, raw record) for the whole MFT following its own data runs."""
        idx = 0
        total = self.mft_size // self.rec if self.mft_size else None
        for lcn, clusters in self.mft_runs:
            if lcn is None:
                idx += clusters * self.cs // self.rec
                continue
            off = self.base + lcn * self.cs
            left = clusters * self.cs
            while left > 0:
                chunk = self.src.read(off, min(left, 4 << 20))
                if not chunk:
                    return
                for i in range(0, len(chunk) - self.rec + 1, self.rec):
                    if total is not None and idx >= total:
                        return
                    yield idx, chunk[i:i + self.rec]
                    idx += 1
                off += len(chunk)
                left -= len(chunk)

    def walk(self, on_entry=None, max_entries=5_000_000):
        names = {}
        files = []
        for idx, raw in self._mft_records():
            r = self._fixup(raw)
            if not r:
                continue
            flags = struct.unpack_from("<H", r, 22)[0]
            base_ref = int.from_bytes(r[32:38], "little")
            if base_ref:
                continue  # extension record
            a = self._attrs(r)
            fn = a.get(0x30)
            if idx == 3 and 0x60 in a:
                self.label = a[0x60]
            if not fn:
                continue
            names[idx] = (fn["name"], fn["parent"])
            if flags & 2 or idx < 24:
                continue
            data = a.get(0x80)
            if not data:
                continue
            files.append((idx, not flags & 1, fn, data, a))
            if len(files) >= max_entries:
                break
        out = []
        for idx, deleted, fn, data, a in files:
            path = self._path(idx, names)
            mt = filetime((a.get(0x10) or fn)["mtime"])
            note = "lista de atributos: puede estar incompleto" if a.get(0x20) and not data.get("size") else ""
            if "resident" in data:
                fe = FileEntry(path, data["size"], deleted, [], mt, note, resident=data["resident"], origin="fs", fs="NTFS")
            else:
                ext, total = [], 0
                for lcn, n in data["runs"]:
                    ln = min(n * self.cs, data["size"] - total)
                    if ln <= 0:
                        break
                    ext.append((None if lcn is None else self.base + lcn * self.cs, ln))
                    total += ln
                if total < data["size"]:
                    note = note or "runlist incompleta"
                fe = FileEntry(path, data["size"], deleted, ext, mt, note, origin="fs", fs="NTFS")
            out.append(fe)
            if on_entry:
                on_entry(fe)
        return out

    def _path(self, idx, names):
        parts = []
        seen = set()
        while idx in names and idx != 5 and idx not in seen:
            seen.add(idx)
            n, parent = names[idx]
            parts.append(n)
            idx = parent & 0xFFFFFFFFFFFF
        if idx != 5:
            parts.append("_huerfanos")
        return "/" + "/".join(reversed(parts))
