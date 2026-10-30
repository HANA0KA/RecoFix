"""Minimal .DS_Store reader (Finder's buddy-allocator B-tree).

Only what the Trash mode needs: Finder stores the "Put Back" location of every
trashed item as ``ptbL`` (original folder, relative to the volume root) and
``ptbN`` (original name) records in ``~/.Trash/.DS_Store``.
"""
import struct


def _ustr(b, p):
    n = struct.unpack_from(">I", b, p)[0]
    return b[p + 4:p + 4 + 2 * n].decode("utf-16-be", "replace"), p + 4 + 2 * n


def _value(b, p):
    typ = b[p:p + 4]
    p += 4
    if typ in (b"long", b"shor"):
        return struct.unpack_from(">I", b, p)[0], p + 4
    if typ == b"bool":
        return bool(b[p]), p + 1
    if typ in (b"type",):
        return b[p:p + 4].decode("latin-1"), p + 4
    if typ == b"ustr":
        return _ustr(b, p)
    if typ == b"blob":
        n = struct.unpack_from(">I", b, p)[0]
        return b[p + 4:p + 4 + n], p + 4 + n
    if typ in (b"comp", b"dutc"):
        return struct.unpack_from(">Q", b, p)[0], p + 8
    raise ValueError("tipo DS_Store desconocido %r" % typ)


def records(data):
    """Yield (filename, code, value) for every record in the file."""
    if len(data) < 36 or data[4:8] != b"Bud1":
        return
    root_off, root_size = struct.unpack_from(">II", data, 8)
    buf = data[4:]  # offsets are relative to byte 4

    def block(addr):
        off = addr & ~0x1F
        size = 1 << (addr & 0x1F)
        return buf[off:off + size]

    root = buf[root_off:root_off + root_size]
    nblocks = struct.unpack_from(">I", root, 0)[0]
    addrs = [struct.unpack_from(">I", root, 8 + 4 * i)[0] for i in range(nblocks)]
    p = 8 + 4 * (-(-nblocks // 256) * 256)
    ndirs = struct.unpack_from(">I", root, p)[0]
    p += 4
    dirs = {}
    for _ in range(ndirs):
        n = root[p]
        name = root[p + 1:p + 1 + n].decode("latin-1")
        dirs[name] = struct.unpack_from(">I", root, p + 1 + n)[0]
        p += 5 + n
    if "DSDB" not in dirs:
        return
    hdr = block(addrs[dirs["DSDB"]])
    top = struct.unpack_from(">I", hdr, 0)[0]

    def walk(node_id, depth=0):
        if depth > 32:
            return
        node = block(addrs[node_id])
        mode, count = struct.unpack_from(">II", node, 0)
        q = 8
        for _ in range(count):
            if mode:
                child = struct.unpack_from(">I", node, q)[0]
                q += 4
                yield from walk(child, depth + 1)
            name, q = _ustr(node, q)
            code = node[q:q + 4].decode("latin-1")
            val, q = _value(node, q + 4)
            yield name, code, val
        if mode:
            yield from walk(mode, depth + 1)

    yield from walk(top)


def put_back_info(path):
    """{trashed name: {"folder": original folder, "name": original name}} from a Trash .DS_Store."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return {}
    out = {}
    try:
        for name, code, val in records(data):
            if code == "ptbL":
                out.setdefault(name, {})["folder"] = val
            elif code == "ptbN":
                out.setdefault(name, {})["name"] = val
    except (ValueError, struct.error, IndexError):
        pass
    return out
