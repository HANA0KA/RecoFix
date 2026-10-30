import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixtures  # noqa: E402
from recofix import mhl  # noqa: E402
from recofix.formats import mxf, probe_any  # noqa: E402
from recofix.recover import Recovery  # noqa: E402
from recofix.scanner import Scan  # noqa: E402
from recofix.source import Source  # noqa: E402

SAMPLES = fixtures.build()


def rb(path):
    with open(path, "rb") as f:
        return f.read()


def wb(path, data):
    with open(path, "wb") as f:
        f.write(data)


def md5(path):
    return hashlib.md5(rb(path)).hexdigest()


class Formats(unittest.TestCase):
    def test_every_sample_has_exact_size(self):
        for name, p in SAMPLES.items():
            with self.subTest(name=name), Source(p) as s:
                f = probe_any(s, 0)
                self.assertIsNotNone(f, name)
                self.assertEqual(f.length, s.size, name)
                self.assertTrue(f.complete, name)

    def test_mxf_metadata(self):
        with Source(SAMPLES["op1a_mpeg2.mxf"]) as s:
            f = mxf.MXFHandler().probe(s, 0)
        self.assertEqual(f.meta["op"], "OP1a")
        self.assertEqual(f.meta["fps"], 25.0)
        self.assertEqual((f.meta["ancho"], f.meta["alto"]), (640, 360))
        self.assertEqual(f.meta["timecode"], "10:00:00:00")
        self.assertEqual(f.meta["codec"], "MPEG-2")


@unittest.skipUnless(shutil.which("ffprobe"), "ffprobe no disponible")
class MXFRepair(unittest.TestCase):
    def test_truncated_clip_is_finalized_and_decodes(self):
        tmp = tempfile.mkdtemp()
        try:
            for src in ("op1a_mpeg2.mxf", "op1a_dnxhr.mxf", "op1a_prores.mxf"):
                p = os.path.join(tmp, src)
                data = rb(SAMPLES[src])
                wb(p, data[:int(len(data) * 0.6)])
                self.assertTrue(mxf.needs_finalize(p))
                rep = mxf.finalize(p)
                self.assertTrue(rep["cambiado"])
                self.assertFalse(mxf.needs_finalize(p))
                with Source(p) as s:
                    f = mxf.MXFHandler().probe(s, 0)
                self.assertTrue(f.complete)
                self.assertEqual(f.length, os.path.getsize(p))
                r = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-f", "null", "-"], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr.strip(), "", src)
        finally:
            shutil.rmtree(tmp)


class Carving(unittest.TestCase):
    """Files embedded in random noise at sector boundaries must come back byte-exact."""

    @classmethod
    def setUpClass(cls):
        rnd = random.Random(7)
        cls.tmp = tempfile.mkdtemp()
        cls.blob = os.path.join(cls.tmp, "noise.raw")
        cls.placed = {}
        with open(cls.blob, "wb") as w:
            for name in sorted(SAMPLES):
                gap = rnd.randrange(1, 64) * 512
                w.write(bytes(rnd.getrandbits(8) for _ in range(gap)))
                pad = -w.tell() % 512
                w.write(b"\0" * pad)
                cls.placed[w.tell()] = name
                w.write(rb(SAMPLES[name]))
            w.write(b"\0" * 4096)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_all_found_exact(self):
        s = Scan(self.blob)
        s.run()
        self.assertEqual(s.error, "")
        got = {it.start: it for it in s.items}
        for off, name in self.placed.items():
            with self.subTest(name=name):
                self.assertIn(off, got, name)
                self.assertEqual(got[off].size, os.path.getsize(SAMPLES[name]), name)


class Filesystems(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import make_images
        cls.images = make_images.all_images()
        cls.out = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out)

    def _scan(self, key):
        s = Scan(self.images[key])
        s.run()
        self.assertEqual(s.error, "")
        return s

    def _recover(self, key, s, items):
        dest = os.path.join(self.out, key)
        rec = Recovery(self.images[key], items, dest)
        rec.run()
        self.assertEqual(rec.error, "")
        return rec

    def check_deleted(self, key):
        s = self._scan(key)
        deleted = {it.path.rsplit("/", 1)[-1].lower(): it for it in s.items
                   if it.group == "deleted" and it.category != "system"}
        want = {"a057c002_261015_r0y8.mxf": "op1a_dnxhr.mxf", "clip_xavc.mp4": "clip_xavc.mp4",
                "photo.jpg": "photo.jpg"}
        for k in want:
            if k not in deleted and k == "photo.jpg":
                k2 = next(n for n in deleted if n.endswith("hoto.jpg"))  # FAT 8.3 deleted names lose 1st char
                deleted[k] = deleted[k2]
            self.assertIn(k, deleted)
        rec = self._recover(key, s, [deleted[k] for k in want])
        for r, (k, sample) in zip(rec.results, want.items()):
            self.assertTrue(r["ok"], r)
            self.assertEqual(md5(r["path"]), md5(SAMPLES[sample]), k)
        self.assertTrue(any(f.startswith("informe_recuperacion_") for f in os.listdir(rec.dest)))

    def test_exfat_deleted(self):
        self.check_deleted("exfat_borrados")

    def test_fat32_deleted(self):
        self.check_deleted("fat32_borrados")

    def test_exfat_reformatted_orphans_keep_names(self):
        s = self._scan("exfat_formateada")
        names = {it.name for it in s.items if it.group == "orphan"}
        for n in ("photo.jpg", "clip_xavc.mp4", "clip_prores.mov", "audio.wav", "doc.pdf"):
            self.assertIn(n, names)
        mov = next(it for it in s.items if it.name == "clip_prores.mov")
        rec = self._recover("exfat_formateada", s, [mov])
        self.assertEqual(md5(rec.results[0]["path"]), md5(SAMPLES["clip_prores.mov"]))


class MHL(unittest.TestCase):
    def test_verify_md5_and_xxh64(self):
        tmp = tempfile.mkdtemp()
        try:
            clip = os.path.join(tmp, "A.mxf")
            shutil.copy(SAMPLES["op1a_mpeg2.mxf"], clip)
            body = '<?xml version="1.0"?><hashlist><hash><file>A.mxf</file><size>%d</size><md5>%s</md5></hash>' \
                   '<hash><file>B.mxf</file><md5>00</md5></hash></hashlist>' % (os.path.getsize(clip), md5(clip))
            m = os.path.join(tmp, "x.mhl")
            wb(m, body.encode())
            rows = {r["file"]: r["status"] for r in mhl.verify([m], [clip])}
            self.assertEqual(rows, {"A.mxf": "OK", "B.mxf": "no recuperado"})
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
