"""Scanner and web UI tests driven by a replayed recording (the golden capture). No radio needed."""
import http.client
import json
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

from bandera.model import MODEL_TAG, MODELS_DIR, load_golden
from bandera.scanner import ReplaySource, ScanConfig, Scanner
from bandera.web import Hub, Runner, make_handler


def golden_recording(directory: Path) -> Path:
    sweeps, _ = load_golden(MODELS_DIR / f"{MODEL_TAG}_golden.bin")
    path = directory / "golden_1600.npy"
    np.save(path, sweeps)
    path.with_suffix(".json").write_text(json.dumps({"start_mhz": 1600.0, "sweep_duration_s": [0.0]}))
    return path


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.rec = golden_recording(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_fill_then_detect(self):
        sc = Scanner(ReplaySource(self.rec), ScanConfig((1600, 3397), 8, 60, False, False), status=lambda m: None)
        evs = [sc.step() for _ in range(8)]
        self.assertEqual([e["fill"] for e in evs], list(range(1, 9)))
        self.assertEqual(evs[0]["boxes"], [])
        self.assertEqual(len(evs[-1]["rel"]), 600)
        lte = [b for b in evs[-1]["boxes"] if b["cls"] == "lte"]
        self.assertTrue(any(1790 < b["lo"] < 1820 for b in lte))  # LTE B3, as in the golden check
        self.assertFalse(any(b["target"] for b in lte))

    def test_recording_round_trip(self):
        sc = Scanner(ReplaySource(self.rec), ScanConfig((1600, 3397), 8, 60, False, False), status=lambda m: None)
        sc.start_recording(self.dir / "out", "unit")
        for _ in range(3):
            sc.step()
        files = sc.stop_recording()
        self.assertEqual(len(files), 1)
        saved = list((self.dir / "out").glob("unit_*.npy"))
        self.assertEqual(np.load(saved[0]).shape, (3, 600))

    def test_tracking_limited_to_band_unless_whole_window(self):
        def wifi_tracks(whole):
            sc = Scanner(ReplaySource(self.rec), ScanConfig((1700, 1900), 8, 60, False, True, whole),
                         status=lambda m: None)
            ev = [sc.step() for _ in range(8)][-1]
            wifi = [b for b in ev["boxes"] if b["cls"] == "wifi"]
            self.assertTrue(wifi and all(b["in_band"] == whole for b in wifi))  # 2.4 GHz is outside 1700-1900
            return [t for t in ev["tracks"] if t["cls"] == "wifi"]
        self.assertEqual(wifi_tracks(False), [])
        self.assertTrue(wifi_tracks(True))

    def test_bad_config_rejected(self):
        with self.assertRaises(ValueError):
            ScanConfig((2500, 2400), 8).validated()
        with self.assertRaises(ValueError):
            ScanConfig((2400, 2500), 9).validated()


class WebTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        sc = Scanner(ReplaySource(golden_recording(self.dir)), ScanConfig((1600, 3397), 8, 60, False, False),
                     status=lambda m: None)
        self.hub = Hub()
        self.runner = Runner(sc, self.hub, "replay", self.dir / "rec")
        sc.status = self.runner.status
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.hub, self.runner))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.runner.start()

    def tearDown(self):
        self.runner.stop()
        self.runner.join(5)
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _events(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("GET", "/events")
        resp = conn.getresponse()
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        kind = None
        while True:
            line = resp.fp.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                yield kind, json.loads(line[6:])

    def _post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        r = conn.getresponse()
        return r.status, json.loads(r.read())

    def test_page_state_sweeps_and_config(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        page = conn.getresponse().read().decode()
        self.assertIn("EventSource", page)
        ev = self._events()
        kind, st = next(ev)
        self.assertEqual(kind, "state")
        self.assertEqual(st["mode"], "replay")
        self.assertEqual(st["windows"][0]["start"], 1600.0)
        detected = None
        for kind, data in ev:
            if kind == "sweep" and data["boxes"]:
                detected = data
                break
        self.assertIn("lte", {b["cls"] for b in detected["boxes"]})
        self.assertEqual(self._post("/api/config", {"w": 16})[0], 202)
        for kind, data in ev:
            if kind == "state" and data["config"]["w"] == 16:
                break
        self.assertEqual(self._post("/api/config", {"w": 9})[0], 400)
        self.assertEqual(self._post("/api/control", {"op": "explode"})[0], 400)
        self.assertEqual(self._post("/api/control", {"op": "pause"})[0], 202)
        for kind, data in ev:
            if kind == "state" and not data["running"]:
                break


if __name__ == "__main__":
    unittest.main()
