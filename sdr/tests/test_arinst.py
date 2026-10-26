"""Arinst wire compatibility and serial fragmentation tests; no SDR required."""
import struct
import unittest
from collections import deque
from types import SimpleNamespace

import numpy as np

from bandera.arinst import ArinstAdapter, ScanRequest, decode_reply, encode_reply, serve_stream


REQUEST = b"scn20 4200000000 5997000000 3000000 200 20 10700000 10000 7"


class FakeSweeper:
    def __init__(self):
        self.calls = []

    def sweep(self, start_mhz, n_points=600):
        self.calls.append((start_mhz, n_points))
        return SimpleNamespace(db=np.full(n_points, -50.0), duration_s=0.123, bad_bursts=0)


class FakeSerial:
    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.output = bytearray()

    def read(self, count):
        if not self.chunks:
            raise AssertionError("server failed to finish bounded requests")
        return self.chunks.popleft()

    def write(self, data):
        count = min(13, len(data))  # force partial writes across binary/trailer boundaries
        self.output.extend(data[:count])
        return count

    def flush(self):
        pass


class ArinstTest(unittest.TestCase):
    def test_canonical_packet_against_independent_bytes(self):
        req = ScanRequest.parse(REQUEST)
        self.assertEqual(req.points, 600)
        packet = encode_reply(req, np.full(600, -50.0), 123)
        expected = b"\r\nscn20 4200000000 7\r\n" + b"\x05\x14" * 600 + b"\xff\xff123\r\ncomplete\r\n"
        self.assertEqual(packet, expected)
        levels, elapsed = decode_reply(req, expected)
        np.testing.assert_array_equal(levels, np.full(600, -50.0))
        self.assertEqual(elapsed, 123)

    def test_binary_newlines_flags_and_limits(self):
        req = ScanRequest.parse("scn20 1 7 1 200 20 10700000 0 0")
        levels = [100, 80, 79, 78.7, -50, -124.7, -200]
        packet = encode_reply(req, levels, 0)
        payload = packet[len(req.header()):len(req.header()) + 14]
        self.assertEqual(payload, struct.pack(">7H", 0, 0, 10, 13, 1300, 2047, 2047))
        # App masks non-level bits before conversion.
        flagged = req.header() + struct.pack(">7H", *([0xA514] * 7)) + b"\xff\xff0\r\ncomplete\r\n"
        np.testing.assert_array_equal(decode_reply(req, flagged)[0], [-50] * 7)

    def test_malformed_or_oversize_requests_rejected(self):
        for line in (b"scn20foo 1 7 1 200 20 10700000 0 0", b"scn20 1 7 1",
                     REQUEST + b" extra", REQUEST.replace(b"3000000", b"0"),
                     b"scn20 -1 7 1 200 20 10700000 0 0",
                     b"scn20 1 99999999 1 200 20 10700000 0 0"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                ScanRequest.parse(line)

    def test_bad_packets_rejected(self):
        req = ScanRequest.parse(REQUEST)
        packet = encode_reply(req, np.full(600, -50), 1)
        for broken in (packet[:-1], packet.replace(b"\xff\xff", b"\x00\x00"),
                       packet.replace(b"4200000000", b"4200000001"), packet + b"junk"):
            with self.assertRaises(ValueError):
                decode_reply(req, broken)
        with self.assertRaises(ValueError):
            encode_reply(req, [float("nan")] * 600, 1)

    def test_fragmentation_timeouts_multiple_commands_and_partial_writes(self):
        radio = FakeSweeper()
        stream = FakeSerial([b"te", b"", b"st\r", b"\nscn", b"20 " + REQUEST[6:24],
                             b"", REQUEST[24:] + b"\r\n"])
        self.assertEqual(serve_stream(radio, stream, max_requests=2), 0)
        self.assertTrue(stream.output.startswith(b"test 0 complete 0\r\n"))
        reply = bytes(stream.output[len(b"test 0 complete 0\r\n"):])
        levels, elapsed = decode_reply(ScanRequest.parse(REQUEST), reply)
        self.assertEqual((len(levels), elapsed), (600, 123))
        self.assertEqual(radio.calls, [(4200.0, 600)])

    def test_bad_input_does_not_poison_next_command(self):
        stream = FakeSerial([b"x" * 400 + b"\r\n", b"testjunk\r\ntest\n"])
        warnings = []
        serve_stream(FakeSweeper(), stream, max_requests=2, report=warnings.append)
        self.assertEqual(stream.output, b"test 0 complete 0\r\n")
        self.assertEqual(len(warnings), 2)

    def test_non_model_grid_rejected_before_radio_access(self):
        radio = FakeSweeper()
        with self.assertRaises(ValueError):
            ArinstAdapter(radio).handle(REQUEST.replace(b"3000000", b"1000000"))
        self.assertEqual(radio.calls, [])

    def test_frontera_discovery_grid(self):
        radio = FakeSweeper()
        line = b"scn20 100000000 200000000 10000000 200 1 10700000 10000 0"
        levels, elapsed = decode_reply(ScanRequest.parse(line), ArinstAdapter(radio).handle(line))
        np.testing.assert_array_equal(levels, [-50] * 11)
        self.assertEqual(radio.calls, [(float(mhz), 1) for mhz in range(100, 201, 10)])
        self.assertEqual(elapsed, 1353)

    def test_discovery_outside_radio_range_is_floor(self):
        radio = FakeSweeper()
        radio.freq_ranges_mhz = [(400, 6000)]
        line = b"scn20 100000000 200000000 10000000 200 1 10700000 10000 0"
        levels, elapsed = decode_reply(ScanRequest.parse(line), ArinstAdapter(radio).handle(line))
        np.testing.assert_array_equal(levels, [-120] * 11)
        self.assertEqual(radio.calls, [])

    def test_extracted_golden_sweeps_survive_wire_roundtrip_exactly(self):
        from bandera.model import MODEL_TAG, MODELS_DIR, load_golden
        sweeps, _ = load_golden(MODELS_DIR / f"{MODEL_TAG}_golden.bin")
        req = ScanRequest.parse("scn20 1600000000 3397000000 3000000 200 20 10700000 10000 1")
        for row in sweeps:
            np.testing.assert_array_equal(decode_reply(req, encode_reply(req, row, 1000))[0], row)

    def test_transient_receive_failure_does_not_stop_server(self):
        class FailingOnce(FakeSweeper):
            def sweep(self, start_mhz, n_points=600):
                if not self.calls:
                    self.calls.append((start_mhz, n_points))
                    raise RuntimeError("incomplete or corrupt UHD burst; sweep discarded")
                return super().sweep(start_mhz, n_points)

        stream = FakeSerial([REQUEST + b"\r\n" + REQUEST + b"\r\n"])
        warnings = []
        serve_stream(FailingOnce(), stream, max_requests=2, report=warnings.append)
        levels, elapsed = decode_reply(ScanRequest.parse(REQUEST), bytes(stream.output))
        self.assertEqual((len(levels), elapsed, len(warnings)), (600, 123, 1))


if __name__ == "__main__":
    unittest.main()
