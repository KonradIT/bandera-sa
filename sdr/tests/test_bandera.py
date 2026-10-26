"""Offline tests: model port, sweep geometry, point extraction, window plan, tracker. No radio needed."""
import unittest

import numpy as np

from bandera.b210 import (CAL_WINDOW_MHZ, DDC_OFFSET, FFT_LEN, FS, IMAGE_SHIFT_MHZ, LO_OFFSET_MHZ,
                          POINTS_PER_DDC, SLOTS, burst_to_points, image_suspects, lo_plan, mirror_index)
from bandera.model import GOLDEN_DIST_TOL, GOLDEN_LOGIT_TOL, N_BINS, STEP_MHZ, Box, RegionModel
from bandera.plan import plan_windows
from bandera.tracker import Tracker, merge_boxes


class ModelTest(unittest.TestCase):
    def test_golden(self):
        for w, (logit, dist) in RegionModel().self_test().items():
            self.assertLessEqual(logit, GOLDEN_LOGIT_TOL, w)
            self.assertLessEqual(dist, GOLDEN_DIST_TOL, w)


class SweepGeometryTest(unittest.TestCase):
    def test_every_point_measured_once(self):
        start = 1600.0
        covered = []
        for lo, first in lo_plan(start):
            for side, ddc in ((0, -DDC_OFFSET), (1, DDC_OFFSET)):
                for j in range(POINTS_PER_DDC):
                    k = first + side * POINTS_PER_DDC + j
                    if k >= N_BINS:
                        continue
                    f = lo + ddc / 1e6 + (j - 3.5) * STEP_MHZ
                    self.assertAlmostEqual(f, start + k * STEP_MHZ, places=6)
                    self.assertGreaterEqual(abs(f - lo), 1.5)  # never on the DC spike
                    covered.append(k)
        self.assertEqual(sorted(covered), list(range(N_BINS)))

    def test_lo_order_stays_inside_cal_window(self):
        plan = [lo for lo, _ in lo_plan(100.0)]
        cal = None
        recals = 0
        for lo in plan:
            if cal is None or abs(lo - cal) > CAL_WINDOW_MHZ:
                cal, recals = lo, recals + 1
        self.assertLessEqual(recals, int(np.ceil(N_BINS * STEP_MHZ / 144)) + 1)


class MirrorTest(unittest.TestCase):
    def test_mirror_is_2lo_minus_f_in_the_other_ddc(self):
        start = 4200.0
        for lo, first in lo_plan(start):
            for k in range(first, first + SLOTS):
                m = mirror_index(k, first)
                self.assertAlmostEqual((start + k * STEP_MHZ) + (start + m * STEP_MHZ), 2 * lo)
                self.assertNotEqual(k - first >= POINTS_PER_DDC, m - first >= POINTS_PER_DDC)

    def test_remeasure_lo_moves_the_mirror_off_the_partner(self):
        start = 4200.0
        for lo, first in lo_plan(start)[:5]:
            for k in range(first, first + SLOTS):
                side = int(k - first >= POINTS_PER_DDC)
                lo2 = lo + (IMAGE_SHIFT_MHZ if side else -IMAGE_SHIFT_MHZ)
                f = start + k * STEP_MHZ
                ddc = lo2 + (-DDC_OFFSET if side else DDC_OFFSET) / 1e6
                j = k - first - side * POINTS_PER_DDC
                self.assertAlmostEqual(f, ddc + (j - 3.5) * STEP_MHZ)  # same point index j in the new DDC
                m2 = mirror_index(k, first) + (SLOTS if side else -SLOTS)
                self.assertAlmostEqual(start + m2 * STEP_MHZ, 2 * lo2 - f)
                self.assertGreaterEqual(abs((2 * lo2 - f) - (2 * lo - f)), 48)

    def test_suspects(self):
        levels = np.full(3 * SLOTS, -50.0)
        floor = np.full(3 * SLOTS, -50.0)
        levels[16 + 2] = -10.0  # strong signal at A_2 of the second tune
        levels[16 + 13] = -44.0  # its mirror B_5, 34 dB down
        levels[5] = -35.0  # only 15 dB over the floor: too weak to leave a visible mirror
        # B_5 plus the mirrors of the strong point's neighbours (its skirt can image too)
        self.assertEqual(image_suspects(levels, floor, [0, 16, 32]), {(16, 1): [28, 29, 30]})
        levels[29] = -14.0  # within 6 dB of the strong partner: a real signal, not re-measured...
        # ...but now strong itself, so the floor points mirroring its neighbours (17, 19) are checked
        self.assertEqual(image_suspects(levels, floor, [0, 16, 32]), {(16, 0): [17, 19], (16, 1): [28, 30]})


class PointExtractionTest(unittest.TestCase):
    def test_tone_lands_in_its_point_and_time_slot(self):
        readings, settle = 24, 50
        n = settle + POINTS_PER_DDC * readings * FFT_LEN
        rng = np.random.default_rng(0)
        iq = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex64) * 1e-3
        j = 5
        f_off = (j - 3.5) * STEP_MHZ * 1e6
        t = np.arange(n) / FS
        slot = slice(settle + j * readings * FFT_LEN, settle + (j + 1) * readings * FFT_LEN)
        iq[slot] += 0.1 * np.exp(2j * np.pi * f_off * t[slot]).astype(np.complex64)
        pts = burst_to_points(iq, settle, readings)
        self.assertEqual(int(np.argmax(pts)), j)
        self.assertGreater(pts[j] - np.median(pts), 30)

    def test_tone_outside_slot_is_not_seen(self):
        readings, settle = 24, 50
        n = settle + POINTS_PER_DDC * readings * FFT_LEN
        iq = np.full(n, 1e-4, np.complex64)
        f_off = (2 - 3.5) * STEP_MHZ * 1e6
        t = np.arange(n) / FS
        slot = slice(settle + 6 * readings * FFT_LEN, settle + 7 * readings * FFT_LEN)  # point 2's tone in slot 6
        iq[slot] += 0.1 * np.exp(2j * np.pi * f_off * t[slot]).astype(np.complex64)
        pts = burst_to_points(iq, settle, readings)
        self.assertLess(pts[2] - np.median(pts), 3)


class PlanTest(unittest.TestCase):
    def test_full_band(self):
        wins = plan_windows(70, 6000)
        self.assertEqual([w.start_mhz for w in wins], [70.0, 1730.0, 3390.0, 4200.0])
        self.assertLessEqual(wins[-1].stop_mhz, 6000)
        for a, b in zip(wins, wins[1:]):
            self.assertAlmostEqual(a.own_hi_mhz, b.own_lo_mhz)
            self.assertTrue(b.start_mhz < a.own_hi_mhz < a.stop_mhz)

    def test_narrow_band_is_centred_and_clamped(self):
        (w,) = plan_windows(2400, 2500)
        self.assertEqual(w.start_mhz, 2450 - 900)
        (w,) = plan_windows(5650, 5950)
        self.assertEqual(w.stop_mhz, 5997)


class TrackerTest(unittest.TestCase):
    def test_instant_and_two_hit_confirm(self):
        tr = Tracker(w=16)
        ev = tr.update(0, 16, [Box("dji", 2400, 2420, 0.55)])
        self.assertIn("confirmed", [k for k, _ in ev])
        ev = tr.update(0, 16, [Box("autel", 5800, 5820, 0.3)])
        self.assertEqual([k for k, _ in ev], ["new"])
        for v in range(17, 32):
            ev = tr.update(0, v, [Box("autel", 5801, 5821, 0.3)])
            self.assertNotIn("confirmed", [k for k, _ in ev])
        ev = tr.update(0, 32, [Box("autel", 5801, 5821, 0.3)])
        self.assertIn("confirmed", [k for k, _ in ev])

    def test_siyi_never_instant_and_w8_threshold(self):
        tr = Tracker(w=8)
        self.assertEqual([k for k, _ in tr.update(0, 8, [Box("siyi", 1400, 1460, 0.95)])], ["new"])
        self.assertEqual([k for k, _ in tr.update(0, 8, [Box("dji", 2400, 2420, 0.6)])], ["new"])

    def test_duplicate_boxes_do_not_spawn_tracks(self):
        tr = Tracker(w=16)
        ev = tr.update(0, 16, [Box("wfb_ng", 2880, 2924, 0.9), Box("wfb_ng", 2884, 2900, 0.6)])
        self.assertEqual([k for k, _ in ev], ["new", "confirmed"])
        self.assertEqual(len(tr.tracks), 1)

    def test_merge_boxes(self):
        merged = merge_boxes([Box("dji", 2400, 2420, 0.5), Box("dji", 2405, 2425, 0.7), Box("lte", 2400, 2420, 0.9)])
        dji = [b for b in merged if b.cls == "dji"]
        self.assertEqual(len(merged), 2)
        self.assertEqual((dji[0].lo_mhz, dji[0].hi_mhz, dji[0].score), (2400, 2425, 0.7))

    def test_non_targets_ignored_and_ageing(self):
        tr = Tracker(w=8)
        self.assertEqual(tr.update(0, 8, [Box("lte", 1800, 1880, 0.9)]), [])
        tr.update(0, 8, [Box("analog", 5800, 5806, 0.3)])
        self.assertEqual(tr.update(0, 24, []), [])
        self.assertEqual([k for k, _ in tr.update(0, 25, [])], ["lost"])


if __name__ == "__main__":
    unittest.main()
