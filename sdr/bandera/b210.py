"""B210 sweeper that emulates the Arinst SSA `scn20` sweep the Frontera model was trained on.

What the model expects per sweep (see README): 600 points on a 3 MHz grid, each point a
~200 kHz RBW power reading averaged over ~20 log-detector samples, points measured one
after another in time. A wideband FFT snapshot is NOT equivalent: bursty links (DJI, WFB-ng)
show up on the analyzer as dotted blocks because every point sees a different instant.

Hardware plan per LO tune (master clock 56 MHz, 28 MS/s DDC output):

    LO = base + 22.5 MHz        (DC sits 1.5 MHz from the nearest grid point)
    DDC A at LO - 12 MHz  ->  grid points base +  0 ... +21 MHz  (offsets -10.5..+10.5)
    DDC B at LO + 12 MHz  ->  grid points base + 24 ... +45 MHz

DDC retunes are FPGA-only (~0.2 ms). LO tunes cost ~10 ms, and UHD re-runs the AD9361 RF DC
calibration (~120 ms) whenever the LO is >100 MHz from the last calibration point, so LO tunes
are visited in blocks of <=192 MHz, each block entered at its centre.

Mirror (IQ image) suppression: the AD9361 leaks a copy of every signal at 2*LO - f, measured
typically ~35 dB down (11 dB seen on single sweeps of an analog VTX). That lands on the point's
partner in the other DDC of the same tune. Points much weaker than a strong partner are measured
again with the LO moved 24 MHz, which moves their mirror 48 MHz away, and the clean value is kept.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .model import N_BINS, STEP_MHZ, instrument_floor

MCR = 56e6
FS = 28e6
DDC_OFFSET = 12e6
POINTS_PER_DDC = 8
LO_STEP_MHZ = 2 * POINTS_PER_DDC * STEP_MHZ  # 48
LO_OFFSET_MHZ = 22.5
FFT_LEN = 224  # 125 kHz bins; Hann 3 dB width ~180 kHz, close to the Arinst 200 kHz RBW
CAL_WINDOW_MHZ = 100.0  # AD9361_CAL_VALID_WINDOW in UHD
FREQ_MIN_MHZ = 70.0
FREQ_MAX_MHZ = 6000.0
SLOTS = 2 * POINTS_PER_DDC  # grid points per LO tune; the passband ripple repeats with this period
FLATNESS_FILE = Path(__file__).resolve().parent.parent / "flatness.json"
IMAGE_STRONG_DB = 20.0  # a partner this far above the floor can leave a visible mirror image
IMAGE_MARGIN_DB = 6.0  # points this far below their partner's strength get re-measured
IMAGE_SHIFT_MHZ = 24.0  # re-measure LO offset; moves the point's mirror by 48 MHz (16 grid points)


def mirror_index(k: int, first: int) -> int:
    """Grid index of the IQ-image partner (2*LO - f) of point k, in the LO tune starting at `first`."""
    return 2 * first + SLOTS - 1 - k


def image_suspects(levels: np.ndarray, floor: np.ndarray, firsts) -> dict[tuple[int, int], list[int]]:
    """{(first, side): [k, ...]}: points that may be dominated by the mirror of a strong partner.
    side 0 = DDC A (below the LO), 1 = DDC B (above).

    The mirror is created while point k is measured, but the partner was measured at another
    instant, and e.g. analog FM moves around inside a 180 kHz slice. So the partner's strength is
    the max over it and its neighbours, and a modest margin is enough: re-measuring a point that
    turns out to be real costs a tune, not accuracy (the fresh reading simply replaces the old one)."""
    padded = np.pad(np.nan_to_num(levels, nan=-np.inf), 1, constant_values=-np.inf)
    strength = np.maximum(np.maximum(padded[:-2], padded[1:-1]), padded[2:])
    out: dict[tuple[int, int], list[int]] = {}
    for first in firsts:
        for k in range(first, first + SLOTS):
            m = mirror_index(k, first)
            if not (np.isfinite(levels[k]) and np.isfinite(levels[m])):
                continue
            if strength[m] - floor[m] >= IMAGE_STRONG_DB and strength[m] - levels[k] >= IMAGE_MARGIN_DB:
                out.setdefault((first, int(k >= first + POINTS_PER_DDC)), []).append(k)
    return out


def load_flatness(path: Path = FLATNESS_FILE) -> np.ndarray | None:
    """Per-slot dB offsets written by `python -m bandera calibrate`."""
    try:
        data = json.loads(Path(path).read_text())
        offsets = np.asarray(data["slot_offsets_db"], np.float32)
        return offsets if offsets.shape == (SLOTS,) else None
    except (OSError, ValueError, KeyError):
        return None


@dataclass
class SweepResult:
    start_mhz: float
    db: np.ndarray  # float32 [n_points]
    t_start: float  # unix time
    duration_s: float
    overload_frac: float  # fraction of samples with |I| or |Q| > 0.9 FS
    bad_bursts: int
    extra: dict = field(default_factory=dict)


def point_bins() -> np.ndarray:
    """FFT bin of each point relative to its DDC centre: -10.5, -7.5 ... +10.5 MHz."""
    offsets = (np.arange(POINTS_PER_DDC) - (POINTS_PER_DDC - 1) / 2) * STEP_MHZ * 1e6
    return np.round(offsets / (FS / FFT_LEN)).astype(int) % FFT_LEN


def burst_to_points(iq: np.ndarray, settle: int, readings: int, averaging: str = "log") -> np.ndarray:
    """8 point levels (dB) from one DDC burst; point j uses its own time slot, as a swept SA would."""
    window = np.hanning(FFT_LEN + 1)[:-1].astype(np.float32)
    frames = iq[settle:settle + POINTS_PER_DDC * readings * FFT_LEN]
    frames = frames.reshape(POINTS_PER_DDC, readings, FFT_LEN) * window
    spec = np.fft.fft(frames, axis=-1)
    p = np.abs(spec[np.arange(POINTS_PER_DDC), :, point_bins()]) ** 2 / float(np.sum(window ** 2))
    if averaging == "log":
        return (10 * np.log10(p + 1e-20)).mean(axis=1)
    return 10 * np.log10(p.mean(axis=1) + 1e-20)


def lo_plan(start_mhz: float, n_points: int = N_BINS) -> list[tuple[float, int]]:
    """(LO MHz, first grid index) per tune, ordered to minimise AD9361 recalibrations."""
    los = [(start_mhz + LO_OFFSET_MHZ + i * LO_STEP_MHZ, i * 2 * POINTS_PER_DDC)
           for i in range(int(np.ceil(n_points / (2 * POINTS_PER_DDC))))]
    per_block = int((2 * CAL_WINDOW_MHZ - 10) // LO_STEP_MHZ) + 1  # 4 LOs span 144 MHz
    order = []
    for b in range(0, len(los), per_block):
        block = los[b:b + per_block]
        mid = len(block) // 2
        order += [block[mid]] + block[:mid] + block[mid + 1:]
    return order


class B210Sweeper:
    def __init__(self, args: str = "type=b200", gain: float = 60.0, antenna: str = "RX2",
                 channel: int = 0, readings: int = 24, settle_us: float = 100.0,
                 averaging: str = "log", level_offset_db: float = 0.0, flatten: bool = True,
                 image_check: bool = True, _usrp=None):
        import uhd

        self.uhd = uhd
        dev_args = f"{args},master_clock_rate={MCR:.0f},num_recv_frames=2048,recv_frame_size=16360"
        self.usrp = _usrp if _usrp is not None else uhd.usrp.MultiUSRP(dev_args)
        if not 0 <= channel < self.usrp.get_rx_num_channels():
            raise ValueError(f"RX channel {channel} does not exist")
        if readings <= 0 or not math.isfinite(settle_us) or settle_us < 0:
            raise ValueError("readings must be positive and settle time must be finite and nonnegative")
        if averaging not in ("log", "lin"):
            raise ValueError("averaging must be log or lin")
        if not math.isfinite(level_offset_db):
            raise ValueError("level offset must be finite")
        self.gain_min_db = float(self.usrp.get_rx_gain_range(channel).start())
        self.gain_max_db = float(self.usrp.get_rx_gain_range(channel).stop())
        if not math.isfinite(gain) or not self.gain_min_db <= gain <= self.gain_max_db:
            raise ValueError(f"gain must be {self.gain_min_db:g}-{self.gain_max_db:g} dB")
        available = list(self.usrp.get_rx_antennas(channel))
        if antenna not in available:
            raise ValueError(f"antenna {antenna!r} is unavailable; choose {available}")
        self.freq_min_mhz, self.freq_max_mhz = FREQ_MIN_MHZ, FREQ_MAX_MHZ
        self.freq_ranges_mhz = [(self.freq_min_mhz, self.freq_max_mhz)]
        self.ch = channel
        self.usrp.set_rx_rate(FS, channel)
        self.usrp.set_rx_bandwidth(56e6, channel)
        if not math.isclose(self.usrp.get_rx_rate(channel), FS, rel_tol=1e-6):
            raise ValueError("B210 profile requires exactly 28 MS/s; use --profile generic")
        self.usrp.set_rx_antenna(antenna, channel)
        self.usrp.set_rx_gain(gain, channel)
        stream_args = uhd.usrp.StreamArgs("fc32", "sc16")
        stream_args.channels = [channel]
        self.stream = self.usrp.get_rx_stream(stream_args)
        self.md = uhd.types.RXMetadata()
        self.readings = readings
        self.averaging = averaging
        self.level_offset_db = level_offset_db
        self.flatness = load_flatness() if flatten else None
        self.image_check = image_check
        self._floors: dict[tuple[float, int], np.ndarray] = {}  # start and point count; discovery uses short sweeps
        self.settle = int(round(settle_us * 1e-6 * FS))
        self.n_samps = self.settle + POINTS_PER_DDC * readings * FFT_LEN
        self.buf = np.zeros(self.n_samps + self.stream.get_max_num_samps(), np.complex64)
        self.gain = self.usrp.get_rx_gain(channel)
        try:
            rx_info = dict(self.usrp.get_usrp_rx_info(channel))
        except Exception:
            rx_info = {}
        self.info = {
            "profile": "b210",
            "channel": channel,
            "mboard": self.usrp.get_mboard_name(),
            "mboard_id": rx_info.get("mboard_id", ""),
            "serial": rx_info.get("mboard_serial", ""),
            "master_clock_rate": self.usrp.get_master_clock_rate(),
            "rx_rate": self.usrp.get_rx_rate(channel),
            "rx_bandwidth": self.usrp.get_rx_bandwidth(channel),
            "gain_db": self.gain,
            "level_offset_db": level_offset_db,
            "antenna": antenna,
            "fft_len": FFT_LEN,
            "readings_per_point": readings,
            "averaging": averaging,
            "flatness_correction": self.flatness is not None,
            "image_check": image_check,
            "frequency_ranges_mhz": self.freq_ranges_mhz,
            "gain_range_db": [self.gain_min_db, self.gain_max_db],
        }

    # ------------------------------------------------------------ radio

    def _tune(self, lo_hz: float, ddc_hz: float) -> None:
        t = self.uhd.types.TuneRequest(lo_hz + ddc_hz)
        t.rf_freq_policy = self.uhd.types.TuneRequestPolicy.manual
        t.rf_freq = lo_hz
        t.dsp_freq_policy = self.uhd.types.TuneRequestPolicy.auto
        self.usrp.set_rx_freq(t, self.ch)

    def _burst(self) -> tuple[np.ndarray, bool]:
        uhd = self.uhd
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        cmd.num_samps = self.n_samps
        cmd.stream_now = True
        self.stream.issue_stream_cmd(cmd)
        got, bad = 0, False
        view = self.buf.reshape(1, -1)
        while got < self.n_samps:
            k = self.stream.recv(view[:, got:], self.md, 0.5)
            if self.md.error_code != uhd.types.RXMetadataErrorCode.none:
                bad = True
                if self.md.error_code == uhd.types.RXMetadataErrorCode.timeout:
                    break
            if k == 0:
                break
            got += k
        return self.buf[:self.n_samps], bad or got < self.n_samps

    def set_gain(self, gain: float) -> None:
        if not math.isfinite(gain) or not self.gain_min_db <= gain <= self.gain_max_db:
            raise ValueError(f"gain must be {self.gain_min_db:g}-{self.gain_max_db:g} dB")
        self.usrp.set_rx_gain(gain, self.ch)
        self.gain = self.info["gain_db"] = self.usrp.get_rx_gain(self.ch)
        self._floors.clear()  # floors were measured at the old gain

    # ------------------------------------------------------------ measurement

    def sweep(self, start_mhz: float, n_points: int = N_BINS) -> SweepResult:
        if not math.isfinite(start_mhz) or n_points <= 0:
            raise ValueError("a sweep needs a finite start and a positive point count")
        plan = lo_plan(start_mhz, n_points)
        los = [lo for lo, _ in plan]
        if start_mhz < FREQ_MIN_MHZ or max(los) > FREQ_MAX_MHZ:
            raise ValueError(f"sweep from {start_mhz} MHz needs LO {min(los)}-{max(los)} MHz, "
                             f"outside {FREQ_MIN_MHZ}-{FREQ_MAX_MHZ}")
        # raw levels for every point of every tune; the tail past n_points is only measured so the
        # last tune's points have their mirror partners for the image check
        ext = np.full(len(plan) * SLOTS, np.nan, np.float32)
        t0 = time.time()
        p0 = time.perf_counter()
        over = total = bad = 0
        # With the previous sweep's floor, suspects are re-measured right after their own tune, while
        # the LO is still inside the AD9361 calibration window (no extra ~120 ms recalibrations).
        floor_key = (start_mhz, n_points)
        prev_floor = self._floors.get(floor_key) if self.image_check else None
        img = {"drops": [], "tunes": 0, "s": 0.0}
        for lo_mhz, first in plan:
            for side, ddc in ((0, -DDC_OFFSET), (1, DDC_OFFSET)):
                k0 = first + side * POINTS_PER_DDC
                if k0 >= n_points and not self.image_check:
                    continue
                self._tune(lo_mhz * 1e6, ddc)
                iq, b = self._burst()
                bad += b
                if b:
                    raise RuntimeError("incomplete or corrupt UHD burst; sweep discarded")
                over += int(np.count_nonzero((np.abs(iq.real) > 0.9) | (np.abs(iq.imag) > 0.9)))
                total += iq.size
                ext[k0:k0 + POINTS_PER_DDC] = burst_to_points(iq, self.settle, self.readings, self.averaging)
            if prev_floor is not None:
                self._fix_images(ext, prev_floor, {first: lo_mhz}, n_points, img)
        if self.image_check:
            if prev_floor is None:  # first sweep of this window: one pass at the end
                self._fix_images(ext, instrument_floor(ext), {first: lo for lo, first in plan}, n_points, img)
            self._floors[floor_key] = instrument_floor(ext)
        extra = {"image_points": len(img["drops"]), "image_tunes": img["tunes"],
                 "image_drop_db": round(float(np.median(img["drops"])), 1) if img["drops"] else 0.0,
                 "image_max_drop_db": round(float(np.max(img["drops"])), 1) if img["drops"] else 0.0,
                 "image_s": round(img["s"], 3)} if self.image_check else {}
        db = ext[:n_points].copy()
        if self.flatness is not None:
            db -= self.flatness[np.arange(n_points) % SLOTS]
        db = np.round(db + self.level_offset_db, 1).astype(np.float32)  # Arinst reports 0.1 dB steps
        return SweepResult(start_mhz, db, t0, time.perf_counter() - p0, over / max(total, 1), bad, extra)

    def _fix_images(self, ext: np.ndarray, floor: np.ndarray, lo_of: dict[int, float], n_points: int,
                    img: dict) -> None:
        """Re-measure points of the given tunes that may be mirror images; updates `ext` and `img`."""
        p0 = time.perf_counter()
        groups = image_suspects(ext, floor, sorted(lo_of))
        shift = lambda key: lo_of[key[0]] + (IMAGE_SHIFT_MHZ if key[1] else -IMAGE_SHIFT_MHZ)
        for (first, side), ks in sorted(groups.items(), key=lambda g: shift(g[0])):
            ks = [k for k in ks if k < n_points]
            lo2 = shift((first, side))
            if not ks or not FREQ_MIN_MHZ <= lo2 <= FREQ_MAX_MHZ:
                continue
            # B-side points fall into DDC A of LO+24 and A-side points into DDC B of LO-24, same index j
            self._tune(lo2 * 1e6, -DDC_OFFSET if side else DDC_OFFSET)
            iq, bad = self._burst()
            img["tunes"] += 1
            if bad:
                continue
            pts = burst_to_points(iq, self.settle, self.readings, self.averaging)
            for k in ks:
                j = k - first - side * POINTS_PER_DDC
                new = pts[j]
                if self.flatness is not None:  # measured in the opposite slot: refer it to the nominal one
                    new += self.flatness[k % SLOTS] - self.flatness[j + (0 if side else POINTS_PER_DDC)]
                m2 = mirror_index(k, first) + (SLOTS if side else -SLOTS)  # partner at the new LO
                if 0 <= m2 < len(ext) and np.isfinite(ext[m2]) and ext[m2] - floor[m2] >= IMAGE_STRONG_DB:
                    new = min(new, ext[k])  # strong mirror there too: keep the cleaner reading
                img["drops"].append(float(ext[k] - new))
                ext[k] = new
        img["s"] += time.perf_counter() - p0
