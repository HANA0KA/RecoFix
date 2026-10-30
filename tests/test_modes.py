"""Recovery modes, repair and tools: lost partitions, sessions, reassembly, repair, imaging, SSH, rescue kit."""
import hashlib
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixtures  # noqa: E402
import make_images  # noqa: E402
from recofix import imaging, partitions, reassembly, remote, rescue, sessions  # noqa: E402
from recofix.fs import volumes  # noqa: E402
from recofix.modes import camera, dsstore, locations  # noqa: E402
from recofix.repair import document, mp4, photo, video  # noqa: E402
from recofix.scanner import Scan  # noqa: E402
from recofix.source import Source  # noqa: E402

SAMPLES = fixtures.build()
V = ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25"]
A = ["-f", "lavfi", "-i", "sine=f=440:sample_rate=48000"]


def ff(path, args):
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + args + [path], check=True)
    return path


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def cut_moov(path):
    with open(path, "rb") as f:
        tops = mp4.top_level(f, os.path.getsize(path))
    moov = [t for t in tops if t[0] == b"moov"][0]
    with open(path, "r+b") as f:
        f.truncate(moov[1])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recofix_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, name):
        return os.path.join(self.tmp, name)


class LostPartitionTest(Base):
    def test_finds_exfat_without_partition_table(self):
        vol = make_images.make("ExFAT", "exfat_borrados", make_images.DELETED)
        img = self.p("disk.raw")
        with open(img, "wb") as w, open(vol, "rb") as r:
            w.write(b"\0" * (3 << 20))
            shutil.copyfileobj(r, w)
            w.write(b"\0" * (1 << 20))
        with Source(img) as src:
            self.assertEqual(volumes(src), [])
            lost = partitions.find_lost(src, [])
        self.assertEqual([(v.fstype, v.offset) for v in lost], [("exfat", 3 << 20)])
        s = Scan(img, deep=False)
        s.run()
        names = {it.name for it in s.items}
        self.assertIn("A057C002_261015_R0Y8.mxf", names)  # a deleted clip, with its name, inside the lost volume


class SessionTest(Base):
    def test_save_load_and_resume(self):
        img = make_images.make("ExFAT", "exfat_borrados", make_images.DELETED)
        s = Scan(img, deep=True)
        s.run()
        path = sessions.save(s, self.p("s.rfscan"))
        back = sessions.load(path)
        self.assertEqual(len(back.items), len(s.items))
        self.assertEqual({(i.name, i.start, i.size) for i in back.items}, {(i.name, i.start, i.size) for i in s.items})
        # interrupted deep pass resumes from its offset and keeps prior items
        half = Scan(img, deep=True)
        half.resume_offset = 0
        half.run()
        half.phase = "cancelado"
        half.resume_offset = 64 << 20
        p2 = sessions.save(half, self.p("half.rfscan"))
        r = sessions.resume(p2)
        self.assertEqual(r.resume_from, 64 << 20)
        before = len(r.items)
        r.run()
        self.assertGreaterEqual(len(r.items), before)
        self.assertEqual(r.phase, "terminado")


class FolderScopeTest(Base):
    def test_scope_limits_metadata_results(self):
        img = make_images.make("ExFAT", "exfat_borrados", make_images.DELETED)
        s = Scan(img, deep=False, scope="/DCIM")
        s.run()
        self.assertTrue(s.items)
        self.assertTrue(all(it.path.startswith("/DCIM/") for it in s.items))


class ReassemblyTest(Base):
    def _scatter(self, clip, cluster=32768):
        data = open(clip, "rb").read()
        n = -(-len(data) // cluster)
        data += b"\0" * (n * cluster - len(data))
        a, b = n // 3, 2 * n // 3
        rnd = random.Random(7)
        fill = lambda k: bytes(rnd.getrandbits(8) for _ in range(k * cluster))  # noqa: E731
        img = bytearray(fill(4))
        start = len(img)
        img += data[:a * cluster] + fill(5) + data[b * cluster:] + fill(3) + data[a * cluster:b * cluster] + fill(2)
        path = self.p("frag.img")
        with open(path, "wb") as f:
            f.write(img)
        return path, start

    def test_h264_and_prores_bit_exact(self):
        for name, args in (("a.mp4", V + A + ["-t", "6", "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac"]),
                           ("p.mov", V + A + ["-t", "2", "-c:v", "prores_ks", "-c:a", "pcm_s16le"])):
            clip = ff(self.p(name), args)
            img, start = self._scatter(clip)
            out = self.p("re_" + name)
            r = reassembly.reassemble(img, start, out)
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["fragments"], 3)
            self.assertEqual(md5(out), md5(clip), name)


class VideoRepairTest(Base):
    def test_advanced_repair_with_sample(self):
        cases = {
            "h264_pcm.mov": V + A + ["-t", "4", "-c:v", "libx264", "-bf", "2", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le"],
            "hevc.mov": V + ["-t", "3", "-c:v", "libx265", "-tag:v", "hvc1", "-x265-params", "log-level=none"],
            "prores.mov": V + A + ["-t", "2", "-c:v", "prores_ks", "-c:a", "pcm_s24le"],
        }
        for name, args in cases.items():
            sample = ff(self.p("sample_" + name), args)
            bad = ff(self.p("bad_" + name), args)
            frames = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                                         "stream=nb_read_packets", "-of", "csv=p=0", bad], capture_output=True, text=True).stdout)
            cut_moov(bad)
            self.assertFalse(video.quick_repair(bad, self.tmp)["ok"])
            r = video.advanced_repair(bad, sample, self.tmp)
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["detail"]["frames"], frames, name)
            self.assertTrue(r["check"]["playable"], r["check"])
            if "pcm" in name or "prores" in name:
                self.assertTrue(any(s["type"] == "audio" for s in r["check"]["streams"]), name)

    def test_quick_repair_truncated_mkv(self):
        clip = ff(self.p("c.mkv"), V + ["-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p"])
        with open(clip, "r+b") as f:
            f.truncate(os.path.getsize(clip) * 2 // 3)
        r = video.quick_repair(clip, self.tmp)
        self.assertTrue(r["ok"], r)


class PhotoDocRepairTest(Base):
    def test_jpeg(self):
        jpg = open(SAMPLES["photo.jpg"], "rb").read()
        t = self.p("t.jpg")
        open(t, "wb").write(jpg[:len(jpg) * 2 // 3])
        self.assertTrue(photo.repair(t, dest=self.tmp)["ok"])
        segs, scan = photo.jpeg_segments(jpg)
        bad = bytearray(jpg)
        bad[2:scan - 20] = os.urandom(scan - 22)
        b = self.p("b.jpg")
        open(b, "wb").write(bytes(bad))
        self.assertTrue(photo.repair(b, dest=self.tmp).get("needs_sample"))
        self.assertTrue(photo.repair(b, SAMPLES["photo.jpg"], self.tmp)["ok"])

    def test_png_truncated(self):
        png = open(SAMPLES["photo.png"], "rb").read()
        t = self.p("t.png")
        open(t, "wb").write(png[:len(png) - 30])
        self.assertTrue(photo.repair(t, dest=self.tmp)["ok"])

    def test_raw_preview(self):
        jpg = open(SAMPLES["photo.jpg"], "rb").read()
        raw = self.p("x.nef")
        open(raw, "wb").write(b"II*\0" + os.urandom(5000) + jpg + os.urandom(3000))
        r = photo.repair(raw, dest=self.tmp)
        self.assertTrue(r["ok"], r)
        self.assertEqual(open(r["out"], "rb").read(), jpg)

    def test_documents(self):
        dx = open(SAMPLES["informe.docx"], "rb").read()
        p = self.p("t.docx")
        open(p, "wb").write(dx[:len(dx) - 60])
        r = document.repair(p, self.tmp)
        self.assertTrue(r["ok"], r)
        with zipfile.ZipFile(r["out"]) as z:
            self.assertIn("word/document.xml", z.namelist())
        pdf = open(SAMPLES["doc.pdf"], "rb").read().replace(b"startxref\n0", b"startxref\n999") + b"\0junk"
        p = self.p("b.pdf")
        open(p, "wb").write(pdf)
        r = document.repair(p, self.tmp)
        self.assertTrue(r["ok"], r)
        self.assertIn(b"/Root 1 0 R", open(r["out"], "rb").read())
        db = open(SAMPLES["datos.sqlite"], "rb").read()
        p = self.p("b.sqlite")
        open(p, "wb").write(db[:len(db) // 2])
        self.assertTrue(document.repair(p, self.tmp)["ok"])


class ImagingTest(Base):
    def test_image_of_file_is_identical(self):
        src = make_images.make("ExFAT", "exfat_borrados", make_images.DELETED)
        out = self.p("copy.img")
        im = imaging.Imager(src, out)
        im.run()
        self.assertEqual(im.status()["error"], "")
        self.assertEqual(md5(out), md5(src))
        self.assertTrue(os.path.exists(out + ".map.json"))
        self.assertEqual(imaging.open_image(out)["path"], out)


class RemoteTest(Base):
    def test_ssh_imaging_with_fake_client(self):
        """A stand-in ``ssh`` that runs the remote command locally exercises the whole pipeline."""
        bindir = self.p("bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "ssh")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\nfor last; do :; done\ncase "$last" in sudo*) read _pw;; esac\n'
                    'exec sh -c "$(printf %s "$last" | sed "s/^sudo -S -p .. //")"\n')
        os.chmod(fake, stat.S_IRWXU)
        disk = self.p("remote_disk.bin")
        with open(disk, "wb") as f:
            f.write(os.urandom(3 << 20))
        old = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + old
        try:
            ssh = remote.SSH("nas.local", "admin", password="x")
            self.assertTrue(ssh.test()["ok"])
            ri = remote.RemoteImager(ssh, disk, self.p("nas.img"), os.path.getsize(disk))
            ri.run()
            self.assertEqual(ri.error, "")
            self.assertEqual(md5(self.p("nas.img")), md5(disk))
        finally:
            os.environ["PATH"] = old


class MiscTest(Base):
    def test_rescue_script_is_valid_shell(self):
        p = self.p("rescate.sh")
        open(p, "w").write(rescue.SCRIPT)
        self.assertEqual(subprocess.run(["sh", "-n", p]).returncode, 0)
        with self.assertRaises(ValueError):
            rescue.make_kit(self.tmp)  # only volume roots under /Volumes

    def test_camera_profiles(self):
        root = self.p("card")
        os.makedirs(os.path.join(root, "PRIVATE", "M4ROOT", "CLIP"))
        os.makedirs(os.path.join(root, "DCIM", "100MSDCF"))
        self.assertEqual(camera.detect(root)["brand"], "Sony")
        root2 = self.p("gopro")
        os.makedirs(os.path.join(root2, "DCIM", "100GOPRO"))
        self.assertEqual(camera.detect(root2)["brand"], "GoPro")
        self.assertIsNone(camera.detect(self.p("nothing")))

    def test_dsstore_reader_on_finder_file(self):
        found = None
        for d in (os.path.expanduser("~/Desktop"), os.path.expanduser("~")):
            if os.path.exists(os.path.join(d, ".DS_Store")):
                found = os.path.join(d, ".DS_Store")
                break
        if not found:
            self.skipTest("no hay .DS_Store real que leer")
        recs = list(dsstore.records(open(found, "rb").read()))
        self.assertTrue(all(len(r[1]) == 4 for r in recs))

    def test_resolve_folder(self):
        r = locations.resolve_folder(HERE)
        self.assertTrue(r["device"].startswith("/dev/r"))
        self.assertTrue(r["scope"].endswith("tests"))


if __name__ == "__main__":
    unittest.main()
