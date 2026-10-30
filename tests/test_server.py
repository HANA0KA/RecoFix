"""HTTP API end to end: security guards, scan -> browse -> preview -> save/load -> recover, repair jobs."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixtures  # noqa: E402
import make_images  # noqa: E402
from recofix import server  # noqa: E402

TOKEN = "testtoken"


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.Handler.token = TOKEN
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.img = make_images.make("ExFAT", "exfat_borrados", make_images.DELETED)
        cls.samples = fixtures.build()
        cls.tmp = tempfile.mkdtemp(prefix="recofix_srv_")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, path, body=None, token=TOKEN, host=None, raw=False):
        req = urllib.request.Request("http://127.0.0.1:%d/api/%s" % (self.port, path),
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        if host:
            req.add_header("Host", host)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            return (r.headers.get("Content-Type"), data) if raw else json.loads(data)

    def wait(self, path, key="done", timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.call(path)
            if st.get(key):
                return st
            time.sleep(0.3)
        self.fail("timeout esperando " + path)

    def test_guards(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call("env", token="bad")
        self.assertEqual(e.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call("env", host="evil.example:80")
        self.assertEqual(e.exception.code, 403)
        self.assertIn("version", self.call("env"))

    def test_scan_browse_recover(self):
        sid = self.call("scan", {"path": self.img, "deep": True, "label": "prueba", "mode": "image"})["id"]
        st = self.wait("scan/" + sid)
        self.assertEqual(st["phase"], "terminado")
        self.assertGreater(st["found"], 10)
        deleted = self.call("scan/%s/items?status=deleted&category=video" % sid)
        names = {i["name"] for i in deleted["items"]}
        self.assertIn("A057C002_261015_R0Y8.mxf", names)
        self.assertIn("clip_xavc.mp4", names)
        self.assertIn("video", deleted["facets"]["categories"])
        tree = self.call("scan/%s/tree?folder=" % sid)
        self.assertIn("DCIM", {n["name"] for n in tree})
        sub = self.call("scan/%s/items?folder=/DCIM/100CANON" % sid)
        self.assertTrue(all(i["path"].startswith("/DCIM/100CANON/") for i in sub["items"]))
        jpg = next(i for i in sub["items"] if i["ext"] == "jpg")
        ctype, data = self.call("scan/%s/preview/%d" % (sid, jpg["id"]), raw=True)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        # save and reopen without rescanning
        saved = self.call("scan/%s/save" % sid, {"path": os.path.join(self.tmp, "s.rfscan")})["path"]
        sid2 = self.call("sessions/load", {"path": saved})["id"]
        self.assertEqual(self.call("scan/" + sid2)["found"], st["found"])
        # recover the deleted videos
        dest = os.path.join(self.tmp, "out")
        rid = self.call("recover", {"scan": sid2, "filter": {"status": "deleted", "category": "video"}, "dest": dest})["id"]
        rs = self.wait("job/" + rid)
        self.assertEqual(rs["error"], "")
        self.assertTrue(all(r["ok"] for r in rs["results"]), rs["results"])
        with open(self.samples["clip_xavc.mp4"], "rb") as a:
            want = a.read()
        got = [r["path"] for r in rs["results"] if r["name"] == "clip_xavc.mp4"][0]
        with open(got, "rb") as b:
            self.assertEqual(b.read(), want)

    def test_repair_job(self):
        jpg = open(self.samples["photo.jpg"], "rb").read()
        bad = os.path.join(self.tmp, "cut.jpg")
        open(bad, "wb").write(jpg[:len(jpg) // 2])
        jid = self.call("repair", {"kind": "photo", "files": [bad], "dest": self.tmp})["id"]
        st = self.wait("job/" + jid)
        self.assertEqual(st["result"], {"ok": 1, "total": 1})
        self.assertTrue(os.path.exists(st["results"][0]["out"]))

    def test_listing_and_static(self):
        d = self.call("ls?path=" + HERE)
        self.assertIn("test_server.py", {e["name"] for e in d["entries"]})
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port) as r:
            self.assertIn(b"RecoFix", r.read())
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen("http://127.0.0.1:%d/static/../server.py" % self.port)


if __name__ == "__main__":
    unittest.main()
