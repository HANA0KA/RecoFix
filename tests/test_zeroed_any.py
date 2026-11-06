"""Rescue on any disk: FAT32, volumes without a readable tree (raw), plain checksum lists, files absent from the tree."""
import hashlib
import mmap
import os
import shutil
import struct
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import exfat_builder  # noqa: E402
import make_images  # noqa: E402
import test_zeroed as tz  # noqa: E402
from test_zeroed import CLIPS, FOLDER, NAME, SAMPLES, Base, make_image, md5, read, run_scan  # noqa: E402
from recofix import mhl  # noqa: E402
from recofix.fs import fat as fatfs  # noqa: E402
from recofix.source import Source  # noqa: E402
from recofix.zeroed import preflight  # noqa: E402

MXF = {"A057C001": CLIPS["A057C001"], "A057C003": CLIPS["A057C003"]}


def md5_list(rows, path, fmt="gnu"):
    with open(path, "w") as f:
        for name, data in rows:
            h = hashlib.md5(data).hexdigest()
            f.write("%s  *%s\n" % (h, name) if fmt == "gnu" else "MD5 (%s) = %s\n" % (name, h))


def sha_list(rows, path):
    with open(path, "w") as f:
        for name, data in rows:
            f.write("%s  %s\n" % (hashlib.sha256(data).hexdigest(), name))


def zero_fat_entries(raw, sizes, clear_chain=True):
    """Reset the directory entries of the files with these sizes to size 0 / cluster 0 (and free their FAT chains)."""
    with Source(raw) as src:
        fs = fatfs.FAT(src, 0)
        fat_off, root_off, nfats = fs.fat_off, fs.root_off, 1
        boot = src.read(0, 512)
        nfats = boot[16]
        stride = (root_off - fat_off) // nfats
        chains = {}
        with open(raw, "r+b") as f, mmap.mmap(f.fileno(), 0) as mm:
            for size in sizes:
                pat = struct.pack("<I", size)
                i = mm.find(pat)
                while i >= 0:
                    e = i - 28
                    if e >= 0 and e % 32 == 0 and mm[e + 11] == 0x20 and mm[e] != 0xE5:
                        first = struct.unpack_from("<H", mm, e + 26)[0] | (struct.unpack_from("<H", mm, e + 20)[0] << 16)
                        if first >= 2:
                            chains[size] = fs.chain(first, 1 << 20)
                            mm[e + 20:e + 22] = b"\0\0"
                            mm[e + 26:e + 28] = b"\0\0"
                            mm[e + 28:e + 32] = b"\0\0\0\0"
                            break
                    i = mm.find(pat, i + 1)
            if clear_chain:
                for ch in chains.values():
                    for c in ch:
                        for k in range(nfats):
                            o = fat_off + k * stride + c * 4
                            mm[o:o + 4] = b"\0\0\0\0"
    return chains


class Fat32(Base):
    @classmethod
    def setUpClass(cls):
        cls.raw = make_images.make("MS-DOS FAT32", "fat32_borrados", make_images_deleted())

    def volume(self, clear_chain):
        img = os.path.join(self.tmp, "fat.img")
        shutil.copy(self.raw, img)
        zero_fat_entries(img, [len(d) for d in MXF.values()], clear_chain)
        ext = os.path.join(self.tmp, "mhl")
        os.makedirs(ext)
        md5_list([(NAME % n, d) for n, d in MXF.items()], os.path.join(ext, "rodaje.md5"))
        return img, ext

    def check(self, clear_chain):
        img, ext = self.volume(clear_chain)
        s = run_scan(img, mhl_paths=[ext], search="all")
        self.assertEqual(s.error, "")
        self.assertEqual(sorted(i.name for i in s.items), sorted(NAME % n for n in MXF))
        self.assertEqual(s.unresolved, [])
        out = self.recover(s)
        for n, d in MXF.items():
            r = out[NAME % n]
            self.assertEqual((r["ok"], r["mhl"], r["estado"]), (True, "OK", "verificado"), r)
            self.assertEqual(md5(read(r["path"])), md5(d))

    def test_fat32_clips_reset_to_zero_with_their_chain_freed(self):
        self.check(True)

    def test_fat32_clips_reset_to_zero_with_the_chain_kept(self):
        self.check(False)

    def test_fat32_preflight_reads_the_tree(self):
        img, ext = self.volume(True)
        d = preflight(img, mhl_paths=[ext])
        self.assertEqual(d["verdict"], "listo")
        self.assertEqual(d["volume"]["mode"], "tabla de asignación")
        self.assertTrue(all(t["hash_only"] for t in d["targets"]))


def make_images_deleted():
    return make_images.DELETED


class NoFilesystem(Base):
    """A raw disk, or a volume whose filesystem cannot be read: found by header, recognised by hash."""

    def raw_disk(self, with_decoy=True):
        img = os.path.join(self.tmp, "raw.img")
        offsets = {"A057C001": 1 << 20, "A057C003": 9 << 20}
        with open(img, "wb") as f:
            f.truncate(80 << 20)
            for n, off in offsets.items():
                f.seek(off)
                f.write(CLIPS[n])
            if with_decoy:
                f.seek(30 << 20)
                f.write(tz.DECOY)
        return img

    def manifest(self, kind):
        ext = os.path.join(self.tmp, "mhl")
        os.makedirs(ext, exist_ok=True)
        rows = [(NAME % n, CLIPS[n]) for n in ("A057C001", "A057C003")]
        if kind == "mhl":
            with open(os.path.join(ext, "0001.mhl"), "wb") as f:
                f.write(tz.asc_mhl([("A057R0Y8/" + n, d) for n, d in rows]))
        elif kind == "md5":
            md5_list(rows, os.path.join(ext, "rodaje.md5"))
        elif kind == "bsd":
            md5_list(rows, os.path.join(ext, "rodaje.md5"), "bsd")
        else:
            sha_list(rows, os.path.join(ext, "rodaje.sha256"))
        return ext

    def check(self, kind):
        img = self.raw_disk()
        s = run_scan(img, mhl_paths=[self.manifest(kind)])
        self.assertEqual(s.error, "")
        self.assertEqual(sorted(i.name for i in s.items), [NAME % "A057C001", NAME % "A057C003"], s.unresolved)
        self.assertEqual(s.volume_info[0]["mode"], "bloques")
        out = self.recover(s)
        for n in ("A057C001", "A057C003"):
            r = out[NAME % n]
            self.assertEqual((r["ok"], r["mhl"]), (True, "OK"), r)
            self.assertEqual(md5(read(r["path"])), md5(CLIPS[n]))

    def test_raw_disk_with_an_asc_mhl(self):
        self.check("mhl")

    def test_raw_disk_with_a_plain_md5_list(self):
        self.check("md5")

    def test_raw_disk_with_a_bsd_style_md5_list(self):
        self.check("bsd")

    def test_raw_disk_with_a_sha256_list(self):
        self.check("sha")

    def test_decoy_with_another_hash_is_never_used(self):
        img = self.raw_disk()
        ext = os.path.join(self.tmp, "mhl")
        os.makedirs(ext)
        md5_list([(NAME % "A057C002", CLIPS["A057C002"])], os.path.join(ext, "x.md5"))  # not on the disk
        s = run_scan(img, mhl_paths=[ext])
        self.assertEqual(s.items, [])
        self.assertTrue(all(u["code"] in ("hash_none", "no_header_all", "data_gone") for u in s.unresolved))

    def test_without_any_manifest_it_says_what_is_missing(self):
        s = run_scan(self.raw_disk())
        self.assertEqual(s.items, [])
        self.assertIn("manifiesto", s.summary)

    def test_preflight_on_raw_disk(self):
        img = self.raw_disk()
        d = preflight(img, mhl_paths=[self.manifest("md5")])
        self.assertEqual(d["verdict"], "listo")
        self.assertEqual(len(d["targets"]), 2)
        self.assertTrue(all(t["absent"] and t["hash_only"] for t in d["targets"]))
        self.assertTrue(any("sin mapa de asignación" in w for w in d["warnings"]))


class ExfatAny(Base):
    def test_hash_only_list_on_exfat(self):
        make_image(self.img)
        ext = os.path.join(self.tmp, "mhl")
        os.makedirs(ext)
        md5_list([(NAME % n, CLIPS[n]) for n in ("A057C001", "A057C003")], os.path.join(ext, "rodaje.md5"))
        s = run_scan(self.img, mhl_paths=[ext])
        self.assertEqual(len(s.items), 2)
        out = self.recover(s)
        self.assertTrue(all(r["ok"] and r["mhl"] == "OK" for r in out.values()))

    def test_files_absent_from_the_tree_need_the_option(self):
        """The volume's MHL lists A057C004 but its directory entry is gone and its data sits in free space."""
        im = exfat_builder.ExfatImage(self.img, 32768, 4096)
        for d in ("RDC", FOLDER, "RDC/ascmhl"):
            im.add_dir(d)
        for n in ("A057C001", "A057C002", "A057C003"):
            im.add_file("%s/%s" % (FOLDER, NAME % n), CLIPS[n])
        im.remnant(CLIPS["A057C004"])
        im.add_file("RDC/ascmhl/0001.mhl", tz.asc_mhl(tz.mhl_rows(CLIPS)))
        im.save()
        self.assertEqual(run_scan(self.img).targets_total, 0)  # not touched by default: the MHL may cover other disks
        s = run_scan(self.img, include_missing=True, search="all")
        self.assertEqual([i.name for i in s.items], [NAME % "A057C004"])
        r = self.recover(s)[NAME % "A057C004"]
        self.assertEqual((r["ok"], r["mhl"]), (True, "OK"))


class Manifests(unittest.TestCase):
    def test_list_formats(self):
        rows = mhl.parse_list("d41d8cd98f00b204e9800998ecf8427e  *dir/a.mov\n"
                              "MD5 (b.mxf) = d41d8cd98f00b204e9800998ecf8427e\n"
                              "da39a3ee5e6b4b0d3255bfef95601890afd80709 c.mp4\n"
                              "0123456789abcdef  d.r3d\n# comment\n")
        self.assertEqual([(r["file"], r["algo"]) for r in rows],
                         [("a.mov", "md5"), ("b.mxf", "md5"), ("c.mp4", "sha1"), ("d.r3d", "xxhash64")])
        self.assertTrue(all(r["size"] is None for r in rows))

    def test_parse_any_picks_xml_or_list(self):
        self.assertEqual(len(mhl.parse_any(tz.asc_mhl([("x/a.mxf", b"abc")]))), 1)
        self.assertEqual(len(mhl.parse_any(b"d41d8cd98f00b204e9800998ecf8427e  a.bin\n")), 1)


if __name__ == "__main__":
    unittest.main()
