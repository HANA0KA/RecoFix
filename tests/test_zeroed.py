"""Clips reset to 0 bytes whose data is still in free space: detection, matching, recovery and verification.

Volumes are built with a pure-Python exFAT formatter (no hdiutil), so the damaged state can be reproduced exactly.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import exfat_builder  # noqa: E402
import fixtures  # noqa: E402
from recofix import mhl, server, sessions  # noqa: E402
from recofix.fs import open_fs, volumes  # noqa: E402
from recofix.recover import Recovery  # noqa: E402
from recofix.source import Source  # noqa: E402
from recofix.util import tool  # noqa: E402
from recofix.zeroed import ZeroedScan  # noqa: E402

SAMPLES = fixtures.build()
V = ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25"]
FOLDER = "RDC/A057R0Y8"
NAME = "%s_261015_R0Y8.mxf"


def read(p):
    with open(p, "rb") as f:
        return f.read()


def md5(b):
    return hashlib.md5(b).hexdigest()


def extra_mxf(name, args):
    d = os.path.join(fixtures.WORK, "extra")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    if not os.path.exists(p):
        subprocess.run(["ffmpeg", "-v", "error", "-y"] + V + args + [p], check=True)
    return read(p)


CLIPS = {
    "A057C001": read(SAMPLES["op1a_mpeg2.mxf"]),
    "A057C002": read(SAMPLES["op1a_dnxhr.mxf"]),
    "A057C003": read(SAMPLES["op1a_prores.mxf"]),
    "A057C004": extra_mxf("b.mxf", ["-t", "2", "-c:v", "mpeg2video", "-b:v", "6M"]),
}
DECOY = extra_mxf("decoy.mxf", ["-t", "1", "-c:v", "mpeg2video", "-b:v", "5M"])  # an old clip left in free space


def asc_mhl(rows, algo="md5"):
    """ASC MHL v2 style list: rows = [(path as written in the MHL, bytes)]."""
    def h(d):
        if algo == "md5":
            return md5(d)
        return subprocess.run([tool("xxhsum"), "-H1"], input=d, capture_output=True).stdout.split()[0].decode()
    body = "".join('<hash><path size="%d">%s</path><%s action="original">%s</%s></hash>' % (len(d), p, algo, h(d), algo)
                   for p, d in rows)
    return ('<?xml version="1.0" encoding="UTF-8"?><hashlist version="2.0" xmlns="urn:ASC:MHL:v2.0">'
            '<hashes>%s</hashes></hashlist>' % body).encode()


def mhl_rows(clips):
    return [("A057R0Y8/" + NAME % n, d) for n, d in clips.items()]


def make_image(path, zero=("A057C001", "A057C003"), mhl_in_volume=True, decoy=True, algo="md5", late=None,
               clusters=32768, cluster_size=4096):
    """The real-world shape: clips written in order, an MHL beside them, then some entries reset to size 0.

    ``late(image, first_clusters)`` runs once every clip is on disk (to add files over a clip's clusters).
    """
    im = exfat_builder.ExfatImage(path, clusters, cluster_size)
    for d in ("RDC", FOLDER, "RDC/ascmhl"):
        im.add_dir(d)
    first = {}
    for n, d in CLIPS.items():
        first[n] = im.add_file("%s/%s" % (FOLDER, NAME % n), d)
    im.decoy = im.remnant(DECOY) if decoy else None
    if late:
        late(im, first)
    if mhl_in_volume:
        im.add_file("RDC/ascmhl/0001.mhl", asc_mhl(mhl_rows(CLIPS), algo))
    for n in zero:
        im.zero("%s/%s" % (FOLDER, NAME % n))
    im.save()
    return im, first


def run_scan(path, **kw):
    s = ZeroedScan(path, **kw)
    s.run()
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recofix_zero_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.img = os.path.join(self.tmp, "vol.img")

    def recover(self, scan, items=None):
        rec = Recovery(scan.path, items if items is not None else scan.items, os.path.join(self.tmp, "out"))
        rec.run()
        self.assertEqual(rec.error, "")
        return {os.path.basename(r["path"]): r for r in rec.results}


class Recovers(Base):
    def test_clips_come_back_identical_and_verified(self):
        make_image(self.img)
        s = run_scan(self.img)
        self.assertEqual(s.error, "")
        self.assertEqual(s.targets_total, 2)
        self.assertEqual(sorted(i.name for i in s.items), [NAME % "A057C001", NAME % "A057C003"])
        self.assertEqual(s.unresolved, [])
        for it in s.items:
            self.assertEqual(it.group, "zeroed")
            self.assertEqual(it.meta["ajuste"], "exacto")
            self.assertTrue(it.complete)
            self.assertEqual(it.meta["clusters_ocupados"], 0)
        out = self.recover(s)
        for n in ("A057C001", "A057C003"):
            r = out[NAME % n]
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["mhl"], "OK")
            self.assertEqual(md5(read(r["path"])), md5(CLIPS[n]))
            self.assertIn("Rescatados_MHL", r["path"])
            self.assertTrue(r["path"].split("Rescatados_MHL")[1].startswith("/RDC/A057R0Y8/"))  # original folder kept

    def test_an_old_clip_left_in_free_space_is_never_used(self):
        im, first = make_image(self.img)
        for search in ("near", "all"):
            s = run_scan(self.img, search=search)
            self.assertEqual(len(s.items), 2, search)
            self.assertNotIn(im.decoy, {i.meta["cluster_inicial"] for i in s.items})
            self.assertEqual({i.meta["cluster_inicial"] for i in s.items}, {first["A057C001"], first["A057C003"]})

    def test_external_mhl_when_the_volume_has_none(self):
        make_image(self.img, mhl_in_volume=False)
        s = run_scan(self.img)
        self.assertEqual(len(s.items), 0)
        self.assertEqual(len(s.unresolved), 2)
        self.assertTrue(all("no figura en ningún manifiesto" in u["reason"] for u in s.unresolved))
        ext = os.path.join(self.tmp, "mhl")
        os.makedirs(ext)
        with open(os.path.join(ext, "0001.mhl"), "wb") as f:
            f.write(asc_mhl(mhl_rows(CLIPS)))
        s = run_scan(self.img, mhl_paths=[ext])
        self.assertEqual(len(s.items), 2)
        out = self.recover(s)
        self.assertTrue(all(r["ok"] and r["mhl"] == "OK" for r in out.values()))

    @unittest.skipUnless(tool("xxhsum"), "xxhsum no instalado")
    def test_xxh64_mhl(self):
        make_image(self.img, algo="xxh64")
        s = run_scan(self.img)
        out = self.recover(s)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(r["ok"] and r["mhl"] == "OK" for r in out.values()), out)

    def test_camera_sized_clusters(self):
        # the real disk uses 256 KiB clusters: every cluster/offset computation must hold at that geometry
        im, first = make_image(self.img, clusters=1024, cluster_size=262144)
        self.assertEqual(im.cs, 262144)
        s = run_scan(self.img)
        self.assertEqual(s.error, "")
        self.assertEqual({i.meta["cluster_inicial"] for i in s.items}, {first["A057C001"], first["A057C003"]})
        out = self.recover(s)
        self.assertTrue(all(r["ok"] and r["mhl"] == "OK" for r in out.values()), out)

    def test_trailing_zero_padding_is_accepted_but_other_tails_are_not(self):
        padded = CLIPS["A057C003"] + b"\0" * 3000  # the MHL size is longer than the MXF structure, all zeros after it
        clips = dict(CLIPS, A057C003=padded)
        im = exfat_builder.ExfatImage(self.img)
        for d in ("RDC", FOLDER, "RDC/ascmhl"):
            im.add_dir(d)
        for n, d in clips.items():
            im.add_file("%s/%s" % (FOLDER, NAME % n), d)
        im.add_file("RDC/ascmhl/0001.mhl", asc_mhl(mhl_rows(clips)))
        im.zero("%s/%s" % (FOLDER, NAME % "A057C003"))
        im.save()
        s = run_scan(self.img)
        self.assertEqual([i.meta["ajuste"] for i in s.items], ["con relleno"])
        out = self.recover(s)
        r = out[NAME % "A057C003"]
        self.assertTrue(r["ok"] and r["mhl"] == "OK", r)
        self.assertEqual(md5(read(r["path"])), md5(padded))

    def test_whole_volume_search_finds_the_same_clips(self):
        make_image(self.img)
        near = run_scan(self.img)
        wide = run_scan(self.img, search="all")
        self.assertEqual({(i.name, i.start) for i in near.items}, {(i.name, i.start) for i in wide.items})
        self.assertEqual(wide.status()["zeroed"]["search"], "all")


class Matching(Base):
    def test_same_size_clips_are_told_apart_by_hash(self):
        a = extra_mxf("tie.mxf", ["-t", "2", "-c:v", "mpeg2video", "-b:v", "4M"])
        b = bytearray(a)
        b[len(b) // 2] ^= 0xFF
        b = bytes(b)
        self.assertEqual(len(a), len(b))
        # on disk: b first, then a; the MHL says D001 is a and D002 is b, so order alone would swap them
        im = exfat_builder.ExfatImage(self.img)
        for d in ("RDC", FOLDER, "RDC/ascmhl"):
            im.add_dir(d)
        im.add_file("%s/%s" % (FOLDER, NAME % "A057C009"), CLIPS["A057C002"])  # live anchor
        im.add_file("%s/%s" % (FOLDER, NAME % "D002"), b)
        im.add_file("%s/%s" % (FOLDER, NAME % "D001"), a)
        im.add_file("RDC/ascmhl/0001.mhl", asc_mhl([("A057R0Y8/" + NAME % "A057C009", CLIPS["A057C002"]),
                                                    ("A057R0Y8/" + NAME % "D001", a), ("A057R0Y8/" + NAME % "D002", b)]))
        im.zero("%s/%s" % (FOLDER, NAME % "D001"))
        im.zero("%s/%s" % (FOLDER, NAME % "D002"))
        im.save()
        s = run_scan(self.img)
        self.assertEqual(len(s.items), 2, s.unresolved)
        out = self.recover(s)
        self.assertEqual(md5(read(out[NAME % "D001"]["path"])), md5(a))
        self.assertEqual(md5(read(out[NAME % "D002"]["path"])), md5(b))
        self.assertTrue(all(r["mhl"] == "OK" for r in out.values()))

    def test_wiped_header_is_reported_not_guessed(self):
        im, first = make_image(self.img)
        with open(self.img, "r+b") as f:
            f.seek(im.heap + (first["A057C001"] - 2) * im.cs)
            f.write(b"\0" * im.cs)
        s = run_scan(self.img)
        self.assertEqual([i.name for i in s.items], [NAME % "A057C003"])
        self.assertEqual(len(s.unresolved), 1)
        self.assertEqual(s.unresolved[0]["name"], NAME % "A057C001")
        self.assertIn("no se encontró", s.unresolved[0]["reason"])

    def test_cluster_taken_by_another_file_is_flagged_and_hash_fails(self):
        # a small live file written over the middle of the zeroed clip, as the ._ files were on the real disk
        def over(im, first):
            im.add_file("%s/._%s" % (FOLDER, NAME % "A057C003"), b"AppleDouble" * 10, at=first["A057C003"] + 5)
        make_image(self.img, zero=("A057C003",), late=over)
        s = run_scan(self.img)
        self.assertEqual(len(s.items), 1)
        it = s.items[0]
        self.assertEqual(it.meta["clusters_ocupados"], 1)
        self.assertFalse(it.complete)
        self.assertIn("pertenecen hoy a otros archivos", it.note)
        out = self.recover(s)
        self.assertNotIn(NAME % "A057C003", out)  # never left under the real name
        r = out[(NAME % "A057C003")[:-4] + "_NO_VERIFICADO.mxf"]
        self.assertEqual(r["mhl"], "distinto")
        self.assertEqual(r["estado"], "no_verificado")
        self.assertFalse(r["ok"])
        self.assertTrue(os.path.exists(r["path"]))  # kept for inspection

    def test_without_mhl_a_media_file_is_not_guessed(self):
        make_image(self.img, mhl_in_volume=False)
        s = run_scan(self.img)
        # both zeroed clips are media (.mxf) with no MHL: they are targets, but nothing proves which header is whose
        self.assertEqual(s.targets_total, 2)
        self.assertEqual(len(s.items), 0)
        self.assertEqual(len(s.unresolved), 2)

    def test_nothing_to_do_says_so(self):
        make_image(self.img, zero=())
        s = run_scan(self.img)
        self.assertEqual((s.targets_total, len(s.items), len(s.unresolved)), (0, 0, 0))
        self.assertIn("nada que reconstruir", s.summary)

    def test_truly_empty_files_are_left_alone(self):
        im = exfat_builder.ExfatImage(self.img)
        for d in ("RDC", FOLDER):
            im.add_dir(d)
        im.add_file("%s/notas.txt" % FOLDER, b"")
        im.add_file("%s/%s" % (FOLDER, NAME % "A057C002"), CLIPS["A057C002"])
        im.zero("%s/notas.txt" % FOLDER)
        im.save()
        s = run_scan(self.img)
        self.assertEqual((s.targets_total, len(s.items), len(s.unresolved)), (0, 0, 0))

    def test_mhl_that_says_empty_is_respected(self):
        im = exfat_builder.ExfatImage(self.img)
        for d in ("RDC", FOLDER, "RDC/ascmhl"):
            im.add_dir(d)
        im.add_file("%s/%s" % (FOLDER, NAME % "A057C002"), CLIPS["A057C002"])
        im.add_file("%s/vacio.mxf" % FOLDER, b"")
        im.add_file("RDC/ascmhl/0001.mhl", asc_mhl([("A057R0Y8/vacio.mxf", b"")]))
        im.zero("%s/vacio.mxf" % FOLDER)
        im.save()
        s = run_scan(self.img)
        self.assertEqual((len(s.items), len(s.unresolved)), (0, 0))

    def test_contradicting_mhls_are_reported(self):
        make_image(self.img, mhl_in_volume=False)
        ext = os.path.join(self.tmp, "mhl")
        os.makedirs(ext)
        for i, d in enumerate((CLIPS["A057C001"], CLIPS["A057C001"] + b"x")):
            with open(os.path.join(ext, "%d.mhl" % i), "wb") as f:
                f.write(asc_mhl([("A057R0Y8/" + NAME % "A057C001", d)]))
        s = run_scan(self.img, mhl_paths=[ext])
        self.assertTrue(any("distintos" in u["reason"] for u in s.unresolved))
        self.assertEqual(s.items, [])


class Plumbing(Base):
    def test_non_exfat_source_is_a_clear_error(self):
        with open(self.img, "wb") as f:
            f.write(os.urandom(1 << 20))
        s = run_scan(self.img)
        self.assertIn("volumen utilizable", s.error)
        self.assertEqual(s.phase, "error")

    def test_exfat_bitmap_tells_free_from_used(self):
        im, first = make_image(self.img)
        with Source(self.img) as src:
            fs = open_fs(src, volumes(src)[0])
            c1, c2 = first["A057C001"], first["A057C002"]
            n1 = -(-len(CLIPS["A057C001"]) // fs.cs)
            n2 = -(-len(CLIPS["A057C002"]) // fs.cs)
            self.assertEqual(fs.allocated_count(c1, n1), 0)  # zeroed: marked free
            self.assertEqual(fs.allocated_count(c2, n2), n2)  # live: all in use
            self.assertEqual(fs.cluster_of(fs.cl_off(c2)), c2)

    def test_mhl_parse_keeps_the_path(self):
        rows = mhl.parse(asc_mhl([("A057R0Y8/" + NAME % "A057C001", b"abc")]))
        self.assertEqual(rows[0]["path"], "A057R0Y8/" + NAME % "A057C001")
        self.assertEqual(rows[0]["file"], NAME % "A057C001")
        self.assertEqual(rows[0]["size"], 3)

    def test_cancel_stops_cleanly(self):
        make_image(self.img)
        s = ZeroedScan(self.img)
        s.cancel.set()
        s.run()
        self.assertEqual(s.phase, "cancelado")
        self.assertEqual(s.items, [])

    def test_saved_session_roundtrip_and_no_resume(self):
        make_image(self.img)
        s = run_scan(self.img)
        path = sessions.save(s, os.path.join(self.tmp, "z.rfscan"))
        back = sessions.load(path)
        self.assertEqual(back.mode, "zeroed")
        self.assertEqual([(i.name, i.size, i.extents) for i in back.items], [(i.name, i.size, i.extents) for i in s.items])
        self.assertEqual(back.items[0].meta["mhl"]["algo"], "md5")
        with self.assertRaises(ValueError):
            sessions.resume(path)
        out = self.recover(back)  # a reloaded session recovers and verifies just the same
        self.assertTrue(all(r["ok"] and r["mhl"] == "OK" for r in out.values()))

    def test_command_line(self):
        make_image(self.img)
        dest = os.path.join(self.tmp, "cli")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-B", "-m", "recofix", "zeroed", self.img, dest],
                           cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("recuperados 2/2", r.stdout)
        self.assertIn("2 verificados con su hash del MHL", r.stdout)
        got = [os.path.join(dp, n) for dp, _, ns in os.walk(dest) for n in ns if n.endswith(".mxf")]
        self.assertEqual(sorted(md5(read(p)) for p in got), sorted(md5(CLIPS[n]) for n in ("A057C001", "A057C003")))

    def test_command_line_exit_code_flags_unresolved(self):
        im, first = make_image(self.img)
        with open(self.img, "r+b") as f:
            f.seek(im.heap + (first["A057C001"] - 2) * im.cs)
            f.write(b"\0" * im.cs)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-B", "-m", "recofix", "zeroed", self.img, os.path.join(self.tmp, "cli")],
                           cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SIN RESOLVER", r.stdout)


class Api(Base):
    TOKEN = "zt"

    @classmethod
    def setUpClass(cls):
        server.Handler.token = cls.TOKEN
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, path, body=None):
        req = urllib.request.Request("http://127.0.0.1:%d/api/%s" % (self.port, path),
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": self.TOKEN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return {"_status": e.code, **json.loads(e.read())}

    def wait(self, path):
        t0 = time.time()
        while time.time() - t0 < 60:
            st = self.call(path)
            if st.get("done"):
                return st
            time.sleep(0.2)
        self.fail("timeout " + path)

    def test_scan_and_recover_through_the_api(self):
        make_image(self.img)
        jid = self.call("scan", {"path": self.img, "mode": "zeroed", "label": "prueba"})["id"]
        st = self.wait("scan/" + jid)
        self.assertEqual(st["error"], "")
        self.assertEqual(st["mode"], "zeroed")
        self.assertEqual(st["groups"], {"zeroed": 2})
        self.assertEqual(st["zeroed"]["resolved"], 2)
        self.assertEqual(st["zeroed"]["unresolved"], [])
        self.assertEqual(st["zeroed"]["mhl_files"][0]["entries"], 4)
        items = self.call("scan/%s/items?group=zeroed" % jid)
        self.assertEqual(items["total"], 2)
        dest = os.path.join(self.tmp, "api")
        rid = self.call("recover", {"scan": jid, "filter": {"group": "zeroed"}, "dest": dest})["id"]
        job = self.wait("job/" + rid)
        self.assertEqual(job["error"], "")
        self.assertEqual([r["mhl"] for r in job["results"]], ["OK", "OK"])
        self.assertTrue(all(r["ok"] for r in job["results"]))

    def test_api_rejects_bad_mhl_argument(self):
        make_image(self.img)
        r = self.call("scan", {"path": self.img, "mode": "zeroed", "mhl": "no-es-lista"})
        self.assertEqual(r["_status"], 400)


if __name__ == "__main__":
    unittest.main()
