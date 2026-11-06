"""Rescue of emptied clips, second layer: fragmented clips (FAT chain), diagnosis with remedies, pre-check, space."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import exfat_builder  # noqa: E402
import test_zeroed as tz  # noqa: E402
from test_zeroed import CLIPS, FOLDER, NAME, Base, make_image, md5, read, run_scan  # noqa: E402
from recofix import recover  # noqa: E402
from recofix.zeroed import preflight  # noqa: E402


def fragmented_image(path, head=3):
    """C002 stored in two runs with another live file between them; its entry is then reset to size 0."""
    im = exfat_builder.ExfatImage(path, 32768, 4096)
    for d in ("RDC", FOLDER, "RDC/ascmhl"):
        im.add_dir(d)
    im.add_file("%s/%s" % (FOLDER, NAME % "A057C001"), CLIPS["A057C001"])
    n = -(-len(CLIPS["A057C002"]) // im.cs)
    c0 = im.reserve(head)
    im.add_file("%s/%s" % (FOLDER, NAME % "A057C004"), CLIPS["A057C004"])
    c1 = im.reserve(n - head)
    im.add_fragmented("%s/%s" % (FOLDER, NAME % "A057C002"), CLIPS["A057C002"], [(c0, head), (c1, n - head)])
    im.add_file("RDC/ascmhl/0001.mhl", tz.asc_mhl(tz.mhl_rows({k: CLIPS[k] for k in ("A057C001", "A057C002", "A057C004")})))
    im.zero("%s/%s" % (FOLDER, NAME % "A057C002"))
    im.save()
    return im, (c0, c1, n)


class Fragmented(Base):
    def test_fragmented_clip_is_followed_through_the_fat_chain(self):
        fragmented_image(self.img)
        s = run_scan(self.img)
        self.assertEqual(s.error, "")
        self.assertEqual([i.name for i in s.items], [NAME % "A057C002"])
        it = s.items[0]
        self.assertEqual(len(it.extents), 2)
        self.assertIn("cadena FAT", it.meta["disposicion"])
        self.assertEqual(it.meta["hash_en_busqueda"], "coincide")
        self.assertTrue(it.complete)
        r = self.recover(s)[NAME % "A057C002"]
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["mhl"], "OK")
        self.assertEqual(r["estado"], "verificado")
        self.assertEqual(md5(read(r["path"])), md5(CLIPS["A057C002"]))

    def test_log_explains_what_was_done(self):
        fragmented_image(self.img)
        s = run_scan(self.img)
        text = "\n".join(e["msg"] for e in s.events)
        self.assertIn("Origen abierto en solo lectura", text)
        self.assertIn("la cadena FAT lo reparte en 2 tramos", text)
        self.assertIn("verificado con el hash del manifiesto", text)
        st = s.status()["zeroed"]
        self.assertEqual(st["stage"], "fin")
        self.assertEqual(st["events_total"], len(s.events))


class Diagnosis(Base):
    def test_missing_mhl_says_what_to_do(self):
        make_image(self.img, mhl_in_volume=False)
        s = run_scan(self.img)
        self.assertEqual({u["code"] for u in s.unresolved}, {"no_mhl"})
        u = s.unresolved[0]
        self.assertEqual(u["action"], "add_mhl")
        self.assertIn(".mhl", u["fix"])

    def test_wiped_free_space_is_reported_as_gone(self):
        im, first = make_image(self.img)
        with open(self.img, "r+b") as f:
            for n in ("A057C001", "A057C003"):
                f.seek(im.heap + (first[n] - 2) * im.cs)
                f.write(b"\0" * (-(-len(CLIPS[n]) // im.cs) * im.cs))
            f.seek(im.heap + (im.decoy - 2) * im.cs)
            f.write(b"\0" * (-(-len(tz.DECOY) // im.cs) * im.cs))
        s = run_scan(self.img, search="all")
        self.assertEqual(s.items, [])
        self.assertEqual({u["code"] for u in s.unresolved}, {"data_gone"})
        self.assertIn("otra copia", s.unresolved[0]["fix"])
        self.assertTrue(s.survey_info["sampled"] >= 20 and s.survey_info["data"] == 0)

    def test_nearby_search_failure_offers_the_wide_search(self):
        im, first = make_image(self.img)
        with open(self.img, "r+b") as f:  # keep some data in free space so it is not "gone", but not the clip's header
            f.seek(im.heap + (first["A057C001"] - 2) * im.cs)
            f.write(b"\0" * im.cs)
        s = run_scan(self.img)
        u = [x for x in s.unresolved if x["name"] == NAME % "A057C001"][0]
        self.assertEqual(u["code"], "no_header_near")
        self.assertEqual(u["action"], "search_all")

    def test_cluster_range_limits_the_search(self):
        im, first = make_image(self.img)
        lo, hi = first["A057C001"] - 2, first["A057C003"] + 200
        s = run_scan(self.img, cluster_range=(lo, hi))
        self.assertEqual(len(s.items), 2)
        s = run_scan(self.img, cluster_range=(im.next + 10, im.next + 400))
        self.assertEqual(s.items, [])
        self.assertTrue(all(u["code"] in ("no_header_all", "data_gone") for u in s.unresolved))

    def test_overwritten_part_is_reported_with_its_location(self):
        def over(im, first):
            im.add_file("%s/._%s" % (FOLDER, NAME % "A057C003"), b"AppleDouble" * 10, at=first["A057C003"] + 5)
        make_image(self.img, zero=("A057C003",), late=over)
        s = run_scan(self.img)
        it = s.items[0]
        self.assertEqual(it.meta["problema"]["code"], "overwritten")
        self.assertEqual(len(it.meta["huecos"]), 1)
        self.assertEqual(it.meta["huecos"][0]["offset"], 5 * 4096)
        self.assertFalse(it.complete)


class Preflight(Base):
    def test_ready_volume(self):
        make_image(self.img)
        d = preflight(self.img)
        self.assertEqual(d["verdict"], "listo")
        self.assertEqual(len(d["targets"]), 2)
        self.assertTrue(all(t["has_mhl"] and t["near_ok"] for t in d["targets"]))
        self.assertEqual(d["volume"]["label"], "TEST")

    def test_volume_without_mhl(self):
        make_image(self.img, mhl_in_volume=False)
        d = preflight(self.img)
        self.assertEqual(d["verdict"], "sin_mhl")
        self.assertTrue(any(".mhl" in w for w in d["warnings"]))

    def test_nothing_to_rescue(self):
        make_image(self.img, zero=())
        self.assertEqual(preflight(self.img)["verdict"], "nada")

    def test_not_exfat(self):
        with open(self.img, "wb") as f:
            f.write(os.urandom(1 << 20))
        d = preflight(self.img)
        self.assertIn(d["verdict"], ("no_exfat", "error"))
        self.assertTrue(d["advice"])


class Space(Base):
    def test_not_enough_room_is_refused_before_copying(self):
        with self.assertRaises(recover.NoSpace) as cm:
            recover.check_space(self.tmp, 1 << 60)
        self.assertIn("No hay espacio suficiente", str(cm.exception))
        recover.check_space(self.tmp, 1)

    def test_recovery_reports_it_as_an_error(self):
        make_image(self.img)
        s = run_scan(self.img)
        s.items[0].size = 1 << 60
        rec = recover.Recovery(s.path, s.items[:1], os.path.join(self.tmp, "out"))
        rec.run()
        self.assertIn("No hay espacio suficiente", rec.error)


class Api(tz.Api):
    def test_preflight_endpoint(self):
        make_image(self.img)
        d = self.call("zeroed/preflight", {"path": self.img})
        self.assertEqual(d["verdict"], "listo")
        self.assertEqual(len(d["targets"]), 2)

    def test_space_endpoint(self):
        d = self.call("dest/space", {"path": self.tmp, "need": 1})
        self.assertTrue(d["ok"])
        d = self.call("dest/space", {"path": self.tmp, "need": 1 << 60})
        self.assertFalse(d["ok"])

    def test_scan_with_range_exposes_the_log(self):
        im, first = make_image(self.img)
        r = self.call("scan", {"path": self.img, "mode": "zeroed", "range": [2, im.next + 50]})
        st = self.wait("scan/" + r["id"])
        z = st["zeroed"]
        self.assertEqual(z["resolved"], 2)
        self.assertEqual(z["range"], [2, im.next + 50])
        self.assertTrue(z["events"])
        bad = self.call("scan", {"path": self.img, "mode": "zeroed", "range": ["a", 3]})
        self.assertEqual(bad["_status"], 400)


if __name__ == "__main__":
    unittest.main()
