"""UHD device selection and a portable, sequential spectrum sweeper.

The B210 profile keeps the measured 56/28 MHz, paired-DDC implementation. Other
frontends use their actual sample rate and bandwidth, with a separate time slot
for each 3 MHz grid point. UHD selects RF/DSP tuning for the generic profile.
"""
from __future__ import annotations

import math
import time

import numpy as np

from .b210 import B210Sweeper, SweepResult
from .model import N_BINS, STEP_MHZ

FLOOR_DB = -120.0


def range_segments(meta_range, scale=1.0) -> list[tuple[float, float]]:
    """Keep disjoint UHD ranges; start()/stop() alone can hide gaps."""
    try:
        segments = list(meta_range)
    except TypeError:
        segments = [meta_range]
    return [(float(r.start()) / scale, float(r.stop()) / scale) for r in segments]


def with_defaults(args: str, **defaults) -> str:
    """Append profile defaults only when a UHD key was not supplied explicitly."""
    keys = {part.split("=", 1)[0].strip() for part in args.split(",") if part.strip()}
    parts = [args] if args else []
    parts.extend(f"{key}={value}" for key, value in defaults.items() if key not in keys)
    return ",".join(parts)


def sequential_power(iq, settle, readings, fft_len, offsets_hz, rate, averaging="log"):
    """Windowed exact-frequency DFTs, one independent time slot per point.

    Evaluating at the requested frequencies avoids FFT-bin rounding when UHD
    coerces a requested sample rate to a device-supported value.
    """
    count = len(offsets_hz)
    window = np.hanning(fft_len + 1)[:-1]
    frames = iq[settle:settle + count * readings * fft_len].reshape(count, readings, fft_len)
    kernel = np.exp(-2j * np.pi * np.asarray(offsets_hz)[:, None] * np.arange(fft_len) / rate) * window
    power = np.abs(np.einsum("prn,pn->pr", frames, kernel)) ** 2 / np.sum(window ** 2)
    if averaging == "log":
        return (10 * np.log10(power + 1e-20)).mean(axis=1)
    return 10 * np.log10(power.mean(axis=1) + 1e-20)


class UHDSweeper:
    """Portable receive-only backend; unsupported window bins are explicit floor."""

    def __init__(self, usrp, uhd, *, gain=None, antenna=None, channel=0,
                 readings=24, averaging="log", rate=10e6, bandwidth=None,
                 settle_us=1000.0, level_offset_db=0.0):
        if not 0 <= channel < usrp.get_rx_num_channels():
            raise ValueError(f"RX channel {channel} does not exist")
        if readings <= 0 or not math.isfinite(settle_us) or settle_us < 0:
            raise ValueError("readings must be positive and settle time must be finite and nonnegative")
        if averaging not in ("log", "lin"):
            raise ValueError("averaging must be log or lin")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("sample rate must be positive and finite")
        if not math.isfinite(level_offset_db):
            raise ValueError("level offset must be finite")
        self.usrp, self.uhd, self.ch = usrp, uhd, channel
        usrp.set_rx_rate(rate, channel)
        self.rate = float(usrp.get_rx_rate(channel))
        if self.rate < 1e6:
            raise ValueError(f"actual rate {self.rate:g} is too low for the approximately 200 kHz RBW")
        if bandwidth is not None and (not math.isfinite(bandwidth) or bandwidth <= 0):
            raise ValueError("bandwidth must be positive and finite")
        # Some fixed-filter daughterboards do not implement bandwidth controls.
        bandwidth_control = True
        try:
            usrp.set_rx_bandwidth(self.rate if bandwidth is None else bandwidth, channel)
        except RuntimeError:
            bandwidth_control = False
        bandwidth_reported = True
        try:
            self.bandwidth = float(usrp.get_rx_bandwidth(channel))
        except RuntimeError:
            # Fixed-filter frontends may not expose the filter bandwidth. A
            # user-specified limit is required so we do not invent a passband.
            if bandwidth is None:
                raise ValueError("this frontend does not report RX bandwidth; specify --bandwidth")
            self.bandwidth = bandwidth
            bandwidth_reported = False
        if self.bandwidth <= 0 or not math.isfinite(self.bandwidth):
            raise ValueError("UHD returned no usable RX bandwidth; specify --bandwidth")
        self.freq_ranges_mhz = range_segments(usrp.get_fe_rx_freq_range(channel), 1e6)
        self.freq_min_mhz = min(lo for lo, _ in self.freq_ranges_mhz)
        self.freq_max_mhz = max(hi for _, hi in self.freq_ranges_mhz)
        self.gain_min_db = float(usrp.get_rx_gain_range(channel).start())
        self.gain_max_db = float(usrp.get_rx_gain_range(channel).stop())
        available = list(usrp.get_rx_antennas(channel))
        antenna = antenna or ("RX2" if "RX2" in available else usrp.get_rx_antenna(channel))
        if antenna not in available:
            raise ValueError(f"antenna {antenna!r} is unavailable on channel {channel}; choose {available}")
        usrp.set_rx_antenna(antenna, channel)
        self.readings, self.averaging = readings, averaging
        self.level_offset_db = level_offset_db
        self.settle = round(settle_us * 1e-6 * self.rate)
        self.fft_len = max(8, round(self.rate / 125000.0))  # Hann 3 dB width approximately 180 kHz
        self.edge_hz = 0.4 * min(self.rate, self.bandwidth)
        # Even groups put DC halfway between grid points. Slow receivers use one
        # point at a nonzero offset instead of widening the model's input grid.
        possible = int(2 * self.edge_hz // (STEP_MHZ * 1e6)) + 1
        self.points_per_tune = max(1, possible - possible % 2)
        stream_args = uhd.usrp.StreamArgs("fc32", "sc16")
        stream_args.channels = [channel]
        self.stream = usrp.get_rx_stream(stream_args)
        self.md = uhd.types.RXMetadata()
        try:
            self.sensor_names = set(usrp.get_rx_sensor_names(channel))
        except RuntimeError:
            self.sensor_names = set()
        self.info = {"profile": "generic", "mboard": usrp.get_mboard_name(),
                     "channel": channel, "rx_rate": self.rate,
                     "rx_bandwidth": self.bandwidth, "antenna": antenna,
                     "bandwidth_control": bandwidth_control, "bandwidth_reported": bandwidth_reported,
                     "frequency_ranges_mhz": self.freq_ranges_mhz,
                     "gain_range_db": [self.gain_min_db, self.gain_max_db],
                     "level_offset_db": level_offset_db,
                     "fft_len": self.fft_len, "readings_per_point": readings,
                     "averaging": averaging, "settle_us": settle_us,
                     "points_per_tune": self.points_per_tune,
                     "flatness_correction": False, "image_check": False}
        try:
            self.info.update({"rx_info": dict(usrp.get_usrp_rx_info(channel)),
                              "master_clock_rate": usrp.get_master_clock_rate()})
        except RuntimeError:
            pass
        self.set_gain(np.clip(60.0, self.gain_min_db, self.gain_max_db) if gain is None else gain)

    def set_gain(self, gain):
        if not math.isfinite(gain) or not self.gain_min_db <= gain <= self.gain_max_db:
            raise ValueError(f"gain must be {self.gain_min_db:g}-{self.gain_max_db:g} dB for this RX frontend")
        self.usrp.set_rx_gain(gain, self.ch)
        self.gain = self.info["gain_db"] = float(self.usrp.get_rx_gain(self.ch))

    def plan(self, lo, hi):
        from .plan import plan_windows
        if not any(a <= lo < hi <= b for a, b in self.freq_ranges_mhz):
            raise ValueError(f"requested band {lo:g}-{hi:g} MHz is outside RX ranges {self.freq_ranges_mhz}")
        return plan_windows(lo, hi, self.freq_min_mhz, self.freq_max_mhz)

    def _receive(self, count):
        n = self.settle + count * self.readings * self.fft_len
        buf = np.zeros((1, n), np.complex64)
        cmd = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.num_done)
        cmd.num_samps, cmd.stream_now = n, True
        self.stream.issue_stream_cmd(cmd)
        got = 0
        while got < n:
            received = self.stream.recv(buf[:, got:], self.md, max(0.5, n / self.rate + 0.1))
            if self.md.error_code != self.uhd.types.RXMetadataErrorCode.none or received <= 0:
                raise RuntimeError(f"UHD receive failed after {got}/{n} samples: {self.md.error_code}")
            got += received
        return buf[0]

    def _tune(self, center_hz):
        self.usrp.set_rx_freq(self.uhd.types.TuneRequest(center_hz), self.ch)
        actual = float(self.usrp.get_rx_freq(self.ch))
        if "lo_locked" in self.sensor_names:
            deadline = time.monotonic() + 0.5
            while not self.usrp.get_rx_sensor("lo_locked", self.ch).to_bool():
                if time.monotonic() > deadline:
                    raise RuntimeError(f"RX LO did not lock at {center_hz / 1e6:g} MHz")
                time.sleep(0.001)
        return actual

    def sweep(self, start_mhz, n_points=N_BINS):
        if not math.isfinite(start_mhz) or n_points <= 0:
            raise ValueError("a sweep needs a finite start and a positive point count")
        freqs = start_mhz + np.arange(n_points) * STEP_MHZ
        supported = np.zeros(n_points, bool)
        for lo, hi in self.freq_ranges_mhz:
            supported |= (freqs >= lo) & (freqs <= hi)
        if not supported.any():
            raise ValueError(f"sweep contains no frequencies in RX ranges {self.freq_ranges_mhz}")
        db = np.full(n_points, FLOOR_DB, np.float32)
        started, clock = time.time(), time.perf_counter()
        over = total = tunes = 0
        for low, high in self.freq_ranges_mhz:
            indices = np.flatnonzero((freqs >= low) & (freqs <= high))
            for begin in range(0, len(indices), self.points_per_tune):
                ks = indices[begin:begin + self.points_per_tune]
                # For an odd tail, move the LO half a grid step, unless only one
                # point fits in the passband (then use one quarter of the rate).
                center = float((freqs[ks[0]] + freqs[ks[-1]]) / 2)
                shift = min(STEP_MHZ / 2, self.edge_hz / 2e6)
                if len(ks) % 2:
                    center += shift if center + shift <= high else -shift
                center = float(np.clip(center, low, high))
                actual = self._tune(center * 1e6)
                offsets = freqs[ks] * 1e6 - actual
                if np.max(np.abs(offsets)) > self.edge_hz + 1 or np.min(np.abs(offsets)) < 1:
                    raise RuntimeError("UHD coerced the tune outside the usable passband or onto DC")
                iq = self._receive(len(ks))
                db[ks] = sequential_power(iq, self.settle, self.readings, self.fft_len,
                                          offsets, self.rate, self.averaging)
                over += int(np.count_nonzero((np.abs(iq.real) > 0.9) | (np.abs(iq.imag) > 0.9)))
                total += iq.size
                tunes += 1
        db[supported] = np.round(db[supported] + self.level_offset_db, 1)
        return SweepResult(start_mhz, db, started, time.perf_counter() - clock,
                           over / max(total, 1), 0,
                           {"measured_points": int(supported.sum()), "floor_points": int((~supported).sum()),
                            "tunes": tunes})


def create_sweeper(*, args="type=b200", profile="auto", gain=None, antenna=None,
                   channel=0, readings=24, averaging="log", flatten=True, image_check=True,
                   rate=None, bandwidth=None, master_clock_rate=None, settle_us=None,
                   subdev=None, level_offset_db=0.0):
    """Open one UHD device, selecting B210 optimizations only for that hardware."""
    import uhd
    if profile not in ("auto", "b210", "generic"):
        raise ValueError("profile must be auto, b210, or generic")
    values = dict(part.strip().split("=", 1) for part in args.split(",") if "=" in part)
    portable = profile == "generic" or any(v is not None for v in (rate, bandwidth))
    if master_clock_rate is not None and master_clock_rate != 56e6:
        portable = True
    if "master_clock_rate" in values and float(values["master_clock_rate"]) != 56e6:
        portable = True
    if profile == "b210" and portable:
        raise ValueError("the b210 profile requires its 56 MHz clock, 28 MS/s rate and 56 MHz bandwidth; use --profile generic")
    if master_clock_rate is not None:
        if "master_clock_rate" in values and float(values["master_clock_rate"]) != master_clock_rate:
            raise ValueError("--master-clock-rate conflicts with master_clock_rate in --args")
        args = with_defaults(args, master_clock_rate=master_clock_rate)
    if (values.get("type") == "b200" or profile == "b210") and not portable:
        args = with_defaults(args, master_clock_rate=56000000, num_recv_frames=2048, recv_frame_size=16360)
    usrp = uhd.usrp.MultiUSRP(args)
    if subdev is not None:
        usrp.set_rx_subdev_spec(uhd.usrp.SubdevSpec(subdev))
    name = usrp.get_mboard_name().lower()
    is_b200 = "b200" in name or "b210" in name or "b2" in name and "libresdr" in name
    if profile == "b210" and not is_b200:
        raise ValueError(f"b210 profile requires B200/B210 hardware; found {usrp.get_mboard_name()}")
    if is_b200 and not portable:
        usrp.set_master_clock_rate(56e6)
        return B210Sweeper(args=args, gain=60.0 if gain is None else gain,
                           antenna=antenna or "RX2", channel=channel, readings=readings,
                           averaging=averaging, flatten=flatten, image_check=image_check,
                           settle_us=100.0 if settle_us is None else settle_us,
                           level_offset_db=level_offset_db, _usrp=usrp)
    return UHDSweeper(usrp, uhd, gain=gain, antenna=antenna, channel=channel,
                      readings=readings, averaging=averaging, rate=10e6 if rate is None else rate,
                      bandwidth=bandwidth, settle_us=1000.0 if settle_us is None else settle_us,
                      level_offset_db=level_offset_db)
