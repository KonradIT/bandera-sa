"""Portable receive geometry and failure handling without UHD hardware."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

from bandera.b210 import B210Sweeper, SweepResult
from bandera.plan import plan_windows
from bandera.scanner import ReplaySource, ScanConfig, Scanner
from bandera.recording import WindowRecorder, load_recording, valid_sweep_mask
from bandera.uhd_sweeper import UHDSweeper, create_sweeper, sequential_power, with_defaults


class Range:
    def __init__(self, low, high):
        self.low, self.high = low, high

    def start(self):
        return self.low

    def stop(self):
        return self.high


class FakeStream:
    def __init__(self):
        self.error = False

    def issue_stream_cmd(self, cmd):
        self.count = cmd.num_samps

    def recv(self, buf, md, timeout):
        md.error_code = "timeout" if self.error else "none"
        if self.error:
            return 0
        buf[:] = 0.001
        return buf.shape[-1]

    def get_max_num_samps(self):
        return 1024


class FakeUSRP:
    def __init__(self, args="", *, name="N210", frequency=(2.4e9, 2.5e9)):
        self.args, self.name = args, name
        self.frequency = frequency
        self.rate, self.bandwidth, self.gain, self.center = 9.6e6, 9.6e6, 0, 2.45e9
        self.stream = FakeStream()
        self.antennas = ["RXA"]

    def get_rx_num_channels(self):
        return 2

    def set_rx_rate(self, rate, channel):
        self.rate = 9.6e6 if rate == 10e6 else rate

    def get_rx_rate(self, channel):
        return self.rate

    def set_rx_bandwidth(self, bandwidth, channel):
        self.bandwidth = bandwidth

    def get_rx_bandwidth(self, channel):
        return self.bandwidth

    def get_fe_rx_freq_range(self, channel):
        return [Range(*self.frequency)]

    def get_rx_gain_range(self, channel):
        return Range(-10, 76 if self.name == "B210" else 30)

    def get_rx_antennas(self, channel):
        return self.antennas

    def get_rx_antenna(self, channel):
        return self.antennas[0]

    def set_rx_antenna(self, antenna, channel):
        self.antenna = antenna

    def get_rx_stream(self, args):
        self.stream_args = args
        return self.stream

    def get_rx_sensor_names(self, channel):
        return []

    def get_mboard_name(self):
        return self.name

    def get_usrp_rx_info(self, channel):
        return {}

    def get_master_clock_rate(self):
        return 56e6

    def set_master_clock_rate(self, rate):
        self.master_clock_rate = rate

    def set_rx_gain(self, gain, channel):
        self.gain = gain

    def get_rx_gain(self, channel):
        return self.gain

    def set_rx_freq(self, request, channel):
        self.center = request.target_freq

    def get_rx_freq(self, channel):
        return self.center


def fake_uhd(usrp):
    return types.SimpleNamespace(
        usrp=types.SimpleNamespace(MultiUSRP=lambda args: usrp,
                                  StreamArgs=lambda *args: types.SimpleNamespace()),
        types=types.SimpleNamespace(
            RXMetadata=lambda: types.SimpleNamespace(error_code="none"),
            StreamCMD=lambda mode: types.SimpleNamespace(),
            StreamMode=types.SimpleNamespace(num_done="num_done"),
            RXMetadataErrorCode=types.SimpleNamespace(none="none", timeout="timeout"),
            TuneRequest=lambda freq: types.SimpleNamespace(target_freq=freq),
            TuneRequestPolicy=types.SimpleNamespace(manual="manual", auto="auto")))


class PortableSweepTest(unittest.TestCase):
    def test_actual_rate_antenna_channel_and_gain_are_used(self):
        usrp = FakeUSRP()
        s = UHDSweeper(usrp, fake_uhd(usrp), channel=1)
        self.assertEqual(s.rate, 9.6e6)  # requested 10 MHz was coerced by UHD
        self.assertEqual(usrp.stream_args.channels, [1])
        self.assertEqual(s.info["antenna"], "RXA")
        self.assertEqual(s.gain, 30)  # device default, not an impossible 60 dB
        with self.assertRaises(ValueError):
            s.set_gain(60)
        with self.assertRaises(ValueError):
            s.plan(5650, 5950)

    def test_limited_frontend_pads_only_unmeasured_frequencies(self):
        usrp = FakeUSRP()
        s = UHDSweeper(usrp, fake_uhd(usrp))
        window = s.plan(2400, 2500)[0]
        result = s.sweep(window.start_mhz)
        freqs = window.start_mhz + np.arange(600) * 3
        covered = (freqs >= 2400) & (freqs <= 2500)
        self.assertEqual(result.db.shape, (600,))
        self.assertEqual(result.extra["measured_points"], int(covered.sum()))
        np.testing.assert_equal(result.db[~covered], -120)
        self.assertTrue(np.isfinite(result.db).all())

    def test_bad_burst_cannot_become_a_valid_spectrum(self):
        usrp = FakeUSRP()
        s = UHDSweeper(usrp, fake_uhd(usrp))
        usrp.stream.error = True
        with self.assertRaisesRegex(RuntimeError, "receive failed"):
            s.sweep(2400, 2)

    def test_fixed_filter_without_bandwidth_or_sensor_queries(self):
        usrp = FakeUSRP()
        def unsupported(*args):
            raise RuntimeError("not implemented by this frontend")
        usrp.set_rx_bandwidth = usrp.get_rx_bandwidth = unsupported
        usrp.get_rx_sensor_names = unsupported
        with self.assertRaisesRegex(ValueError, "specify --bandwidth"):
            UHDSweeper(usrp, fake_uhd(usrp))
        s = UHDSweeper(usrp, fake_uhd(usrp), bandwidth=8e6)
        self.assertFalse(s.info["bandwidth_control"])
        self.assertFalse(s.info["bandwidth_reported"])
        self.assertEqual(s.bandwidth, 8e6)
        self.assertEqual(s.sensor_names, set())

    def test_exact_frequency_and_sequential_time_slots_with_coerced_rate(self):
        rate, fft_len, readings, settle = 9.6e6, 77, 4, 17
        offsets = np.array([-1.5e6, 1.5e6])
        n = settle + 2 * readings * fft_len
        rng = np.random.default_rng(9)
        iq = (rng.normal(size=n) + 1j * rng.normal(size=n)) * 1e-4
        slot = slice(settle + readings * fft_len, n)
        iq[slot] += 0.1 * np.exp(2j * np.pi * offsets[1] * np.arange(n)[slot] / rate)
        powers = sequential_power(iq, settle, readings, fft_len, offsets, rate)
        self.assertGreater(powers[1] - powers[0], 50)
        expected = 10 * np.log10(0.01 * np.hanning(fft_len + 1)[:-1].sum() ** 2 /
                                np.sum(np.hanning(fft_len + 1)[:-1] ** 2))
        self.assertAlmostEqual(powers[1], expected, places=2)

    def test_nondefault_profile_is_selected_without_b210_clock_assumptions(self):
        usrp = FakeUSRP()
        with patch.dict("sys.modules", {"uhd": fake_uhd(usrp)}):
            s = create_sweeper(args="addr=192.168.10.2")
            self.assertIsInstance(s, UHDSweeper)
            with self.assertRaises(ValueError):
                create_sweeper(args="addr=192.168.10.2", rate=0)
            with self.assertRaisesRegex(ValueError, "requires B200/B210"):
                create_sweeper(args="addr=192.168.10.2", profile="b210")

    def test_profile_defaults_do_not_override_explicit_device_arguments(self):
        self.assertEqual(with_defaults("type=b200,recv_frame_size=4096", recv_frame_size=16360,
                                       num_recv_frames=2048),
                         "type=b200,recv_frame_size=4096,num_recv_frames=2048")

    def test_b210_discovery_floor_is_not_reused_for_full_sweep(self):
        usrp = FakeUSRP(name="B210", frequency=(70e6, 6e9))
        usrp.antennas = ["RX2"]
        with patch.dict("sys.modules", {"uhd": fake_uhd(usrp)}):
            s = B210Sweeper(_usrp=usrp, readings=1, flatten=False, channel=1)
            self.assertEqual(usrp.stream_args.channels, [1])
            s.sweep(100, n_points=1)
            result = s.sweep(100, n_points=600)
            self.assertEqual(result.db.shape, (600,))
            self.assertEqual(set(s._floors), {(100, 1), (100, 600)})


class RejectedSweepTest(unittest.TestCase):
    def test_recording_quality_survives_replay_and_offline_inference(self):
        from bandera.__main__ import _replay
        with tempfile.TemporaryDirectory() as tmp:
            rec = WindowRecorder(Path(tmp), 1600, {"gain_db": 20, "profile": "generic"})
            for i in range(16):
                rec.add(SweepResult(1600, np.full(600, -50, np.float32), i, 0.0,
                                    0.01 if i == 7 else 0, 1 if i == 6 else 0,
                                    {"measured_points": 600}))
            rec.flush()
            rows, meta = load_recording(rec.npy)
            mask = valid_sweep_mask(rows, meta, 1e-3)
            self.assertEqual(np.flatnonzero(~mask).tolist(), [6, 7])
            source = ReplaySource(rec.npy, loop=False)
            replayed = [source.sweep(1600) for _ in range(16)]
            self.assertEqual(replayed[6].bad_bursts, 1)
            self.assertEqual(replayed[7].overload_frac, 0.01)
            self.assertEqual(replayed[0].extra["measured_points"], 600)
            with patch("bandera.__main__.RegionModel") as model_class:
                model_class.return_value.detect.return_value = (None, [])
                _replay(rows, 1600, 8, 1, False, meta)
                self.assertEqual(model_class.return_value.detect.call_count, 1)

    def test_overload_and_nan_reset_consecutive_window_without_model_input(self):
        class Source:
            gain = 10.0
            info = {"gain_range_db": [-10, 30]}
            count = 0

            def plan(self, lo, hi):
                return plan_windows(lo, hi)

            def set_gain(self, gain):
                self.gain = gain

            def sweep(self, start):
                self.count += 1
                db = np.full(600, -50, np.float32)
                if self.count == 9:
                    db[10] = np.nan
                return SweepResult(start, db, float(self.count), 0.1,
                                   0.01 if self.count == 7 else 0, 0)

        model = types.SimpleNamespace(detect=lambda *args: self.fail("invalid rows reached model"))
        scanner = Scanner(Source(), ScanConfig((2400, 2500), w=8, gain=10), model=model)
        events = [scanner.step() for _ in range(9)]
        self.assertEqual([ev["fill"] for ev in events], [1, 2, 3, 4, 5, 6, 0, 1, 0])
        self.assertFalse(events[6]["accepted"])
        self.assertFalse(events[8]["accepted"])
        json.dumps(events, allow_nan=False)
        with self.assertRaises(ValueError):
            scanner.configure(ScanConfig((2400, 2500), gain=60))


if __name__ == "__main__":
    unittest.main()
