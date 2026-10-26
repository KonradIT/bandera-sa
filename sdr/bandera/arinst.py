"""Arinst SCN20 framing and a receive-only serial adapter for UHD sweepers.

The transport follows Frontera 2.22's c8/l and c8/o parsers. Measurement
settings belong to the selected radio profile; this is not a complete Arinst
instrument emulator. See docs/protocol-research.md for the remaining gaps.
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass

import numpy as np

MAX_POINTS = 2048
MAX_LINE = 256
STEP_HZ = 3_000_000


@dataclass(frozen=True)
class ScanRequest:
    start_hz: int
    stop_hz: int
    step_hz: int
    synth_timeout: int
    samples: int
    intermediate_hz: int
    attenuation: int
    index: int

    @property
    def points(self) -> int:
        return (self.stop_hz - self.start_hz) // self.step_hz + 1

    @classmethod
    def parse(cls, line: bytes | str) -> "ScanRequest":
        if isinstance(line, bytes):
            line = line.decode("ascii")
        fields = line.split()
        if len(fields) != 9 or fields[0] != "scn20":
            raise ValueError("expected scn20 and eight unsigned decimal arguments")
        if any(re.fullmatch(r"[0-9]+", value) is None for value in fields[1:]):
            raise ValueError("scan arguments must be unsigned decimal integers")
        req = cls(*(int(value) for value in fields[1:]))
        if req.step_hz == 0 or req.stop_hz < req.start_hz or req.stop_hz > 2**64 - 1:
            raise ValueError("invalid scan range")
        if req.points > MAX_POINTS or req.index > 2**31 - 1:
            raise ValueError("scan exceeds point or sequence-index limit")
        if req.samples == 0:
            raise ValueError("sample count must be positive")
        return req

    def header(self) -> bytes:
        return f"\r\nscn20 {self.start_hz} {self.index}\r\n".encode("ascii")


def encode_reply(req: ScanRequest, levels, elapsed_ms: int) -> bytes:
    """Encode 0.1 dB levels without permitting NaNs or a false FF FF sentinel."""
    db = np.asarray(levels, dtype=np.float64)
    if db.shape != (req.points,) or not np.isfinite(db).all():
        raise ValueError("one finite level is required for every requested point")
    if not 0 <= elapsed_ms <= 2**31 - 1:
        raise ValueError("elapsed time is outside the protocol integer range")
    # Positive half-up rounding matches C lround after clamping at zero.
    words = np.floor(np.clip(800.0 - db * 10.0, 0, 2047) + 0.5).astype(">u2")
    return (req.header() + words.tobytes() + b"\xff\xff" +
            f"{elapsed_ms}\r\ncomplete\r\n".encode("ascii"))


def decode_reply(req: ScanRequest, packet: bytes) -> tuple[np.ndarray, int]:
    """Validate a complete reply using its known point count, not byte delimiters.

    CR/LF may occur inside a binary level. A serial reader must collect all
    points before interpreting the ASCII trailer.
    """
    header = req.header()
    if not packet.startswith(b"\r\n"):
        header = header[2:]  # early Bandera firmware omitted the official leading CRLF
    if not packet.startswith(header):
        raise ValueError("reply start frequency or sequence index does not match")
    end = len(header) + 2 * req.points
    if packet[end:end + 2] != b"\xff\xff":
        raise ValueError("missing end-of-sweep marker or incorrect point count")
    trailer = re.fullmatch(rb"([0-9]+)\r\ncomplete\r\n", packet[end + 2:])
    if trailer is None:
        raise ValueError("invalid completion trailer")
    words = np.frombuffer(packet[len(header):end], dtype=">u2")
    levels = (800 - (words.astype(np.int32) & 0x7FF)) / 10.0
    return levels.astype(np.float32), int(trailer[1])


class ArinstAdapter:
    """Serve Frontera's model grid and discovery probe using a UHD backend.

    SCN20's legacy instrument settings are retained in the request for audit.
    The SDR's RBW/dwell/gain are selected by CLI options and are not inferred
    from undocumented analog-instrument control fields.
    """

    def __init__(self, sweeper):
        self.sweeper = sweeper

    def handle(self, line: bytes) -> bytes:
        if line.strip() == b"test":
            return b"test 0 complete 0\r\n"
        req = ScanRequest.parse(line)
        if (req.start_hz, req.stop_hz, req.step_hz, req.samples) == (
                100_000_000, 200_000_000, 10_000_000, 1):
            # Frontera probes the instrument on a different grid before opening
            # the model's SCN20 stream. Measure the actual requested frequencies;
            # a backend with no coverage there reports its explicit floor.
            results = []
            duration = 0.0
            ranges = getattr(self.sweeper, "freq_ranges_mhz", None)
            for hz in range(req.start_hz, req.stop_hz + 1, req.step_hz):
                mhz = hz / 1e6
                if ranges and not any(lo <= mhz <= hi for lo, hi in ranges):
                    results.append(-120.0)
                    continue
                result = self.sweeper.sweep(mhz, n_points=1)
                self._validate_result(result)
                if np.asarray(result.db).shape != (1,):
                    raise ValueError("radio returned an incorrect discovery point count")
                results.append(result.db[0])
                duration += result.duration_s
            return encode_reply(req, results, round(duration * 1000))
        if req.step_hz != STEP_HZ:
            raise ValueError("UHD adapter supports the 3 MHz model grid and Frontera discovery only")
        result = self.sweeper.sweep(req.start_hz / 1e6, n_points=req.points)
        self._validate_result(result)
        return encode_reply(req, result.db, round(result.duration_s * 1000))

    @staticmethod
    def _validate_result(result) -> None:
        if result.bad_bursts:
            raise ValueError("radio returned an incomplete sweep")
        if not math.isfinite(result.duration_s) or result.duration_s < 0:
            raise ValueError("radio returned an invalid sweep duration")


def serve_stream(sweeper, stream, *, max_requests: int = 0, report=None) -> int:
    """Run over a pyserial-like byte stream; keep partial lines across timeouts.

    Invalid/unsupported requests are logged to the host console and produce no
    invented protocol response. Generator commands are not implemented.
    """
    if max_requests < 0:
        raise ValueError("max_requests must be nonnegative")
    if report is None:
        report = lambda message: print(message, file=sys.stderr)
    adapter = ArinstAdapter(sweeper)
    pending = bytearray()
    discard = False
    handled = 0
    while max_requests == 0 or handled < max_requests:
        chunk = stream.read(max(1, min(getattr(stream, "in_waiting", 0), 4096)))
        for byte in chunk:
            if byte in (10, 13):
                if discard:
                    discard = False
                    pending.clear()
                    continue
                if not pending:
                    continue
                line = bytes(pending)
                pending.clear()
                handled += 1
                try:
                    packet = adapter.handle(line)
                except (ValueError, UnicodeError, RuntimeError) as exc:
                    # A bad UHD burst invalidates this request. The client can
                    # time out and retry without requiring a server restart.
                    report(f"Serial request failed: {exc}")
                else:
                    sent = 0
                    while sent < len(packet):
                        count = stream.write(packet[sent:])
                        if not count or count < 0:
                            raise OSError("serial write made no progress")
                        sent += count
                    stream.flush()
                if max_requests and handled >= max_requests:
                    return 0
            elif not discard:
                pending.append(byte)
                if len(pending) > MAX_LINE:
                    report("Discarded overlong serial request")
                    pending.clear()
                    discard = True
    return 0


def serve_serial(sweeper, port: str, *, baudrate: int = 115200,
                 timeout: float = 0.1, max_requests: int = 0) -> int:
    """Open the server end of a serial link (or paired virtual COM ports)."""
    if baudrate <= 0 or not math.isfinite(timeout) or timeout <= 0 or max_requests < 0:
        raise ValueError("baudrate/timeout must be positive and max_requests nonnegative")
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("serial serving requires pyserial: pip install pyserial") from exc
    print(f"Arinst receive adapter on {port} at {baudrate} baud; Ctrl+C to stop", file=sys.stderr)
    print("SCN20 uses the configured SDR RBW, readings and gain; legacy control fields "
          "do not change those settings.", file=sys.stderr)
    try:
        with serial.serial_for_url(port, baudrate=baudrate, timeout=timeout, write_timeout=2) as stream:
            return serve_stream(sweeper, stream, max_requests=max_requests)
    except KeyboardInterrupt:
        return 0
