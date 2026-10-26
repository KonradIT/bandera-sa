"""Scan loop shared by the CLI (`run`) and the web UI (`web`).

A Scanner owns the window plan, one ring buffer of W sweeps per window, the tracker and any
recorders. Each step() sweeps the next window round-robin and returns a JSON-ready dict.
Sources: UHD sweepers (live) or ReplaySource (recorded .npy, same interface).
"""
from __future__ import annotations

import collections
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .b210 import SweepResult
from .model import CLASSES, N_BINS, SUPPORTED_W, RegionModel, instrument_floor
from .plan import Window, plan_windows
from .recording import WindowRecorder, load_recording
from .tracker import Tracker, merge_boxes

OVERLOAD_WARN = 1e-3
AUTO_GAIN_OK = 1e-4
AUTO_GAIN_STEP_DB = 6.0


@dataclass
class ScanConfig:
    band: tuple = (2300.0, 2550.0)
    w: int = 16
    gain: float = 60.0
    auto_gain: bool = False
    detect_wifi: bool = False
    whole_window: bool = False  # track/alert on the whole 1800 MHz model window, not just the band

    def validated(self) -> "ScanConfig":
        lo, hi = (float(x) for x in self.band)
        if not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi):
            raise ValueError(f"bad band {lo}-{hi}")
        if int(self.w) not in SUPPORTED_W:
            raise ValueError(f"W must be one of {SUPPORTED_W}")
        if not math.isfinite(float(self.gain)):
            raise ValueError("gain must be finite")
        return ScanConfig((lo, hi), int(self.w), float(self.gain), bool(self.auto_gain), bool(self.detect_wifi),
                          bool(self.whole_window))

    def in_band(self, center_mhz: float) -> bool:
        return self.whole_window or self.band[0] <= center_mhz < self.band[1]


class ReplaySource:
    """Plays a recording through the scanner at roughly its original sweep rate."""

    def __init__(self, path: Path, speed: float = 1.0, loop: bool = True):
        self.path = Path(path)
        self.rows, meta = load_recording(self.path)
        self.overload = meta.get("overload_frac", [0.0] * len(self.rows))
        self.bad_bursts = meta.get("bad_bursts", [0] * len(self.rows))
        self.extra = meta.get("sweep_extra", [{}] * len(self.rows))
        self.start_mhz = float(meta.get("start_mhz", 0.0))
        durations = meta.get("sweep_duration_s") or [1.3]
        self.period = float(np.median(durations)) / max(speed, 1e-3)
        self.loop = loop
        self.i = 0
        self.gain = float(meta.get("sdr_device", {}).get("gain_db", 0.0))
        self.info = {"mboard": "replay", "file": self.path.name, "gain_db": self.gain}

    def plan(self, lo: float, hi: float) -> list[Window]:
        return [Window(0, self.start_mhz, -1e9, 1e9)]  # a recording has exactly one window

    def set_gain(self, gain: float) -> None:
        pass  # recorded levels are fixed

    def sweep(self, start_mhz: float) -> SweepResult:
        if self.i >= len(self.rows):
            if not self.loop:
                raise EOFError("end of recording")
            self.i = 0
        t0 = time.time()
        time.sleep(self.period)
        index = self.i
        row = self.rows[index]
        self.i += 1
        return SweepResult(self.start_mhz, row, t0, self.period,
                           self.overload[index], self.bad_bursts[index], self.extra[index])


class LiveSource:
    """Adapter giving a UHD sweeper the source interface (plan + sweep + gain)."""

    def __init__(self, sweeper):
        self.sweeper = sweeper
        self.info = sweeper.info

    @property
    def gain(self) -> float:
        return self.sweeper.gain

    def plan(self, lo: float, hi: float) -> list[Window]:
        if hasattr(self.sweeper, "plan"):
            return self.sweeper.plan(lo, hi)
        return plan_windows(lo, hi, self.sweeper.freq_min_mhz, self.sweeper.freq_max_mhz)

    def set_gain(self, gain: float) -> None:
        self.sweeper.set_gain(gain)

    def sweep(self, start_mhz: float) -> SweepResult:
        return self.sweeper.sweep(start_mhz)


@dataclass
class WindowState:
    window: Window
    gain: float
    buffer: collections.deque
    visits: int = 0
    last_sweep_s: float = 0.0
    last_overload: float = 0.0
    recorder: WindowRecorder | None = None
    last_boxes: list = field(default_factory=list)


class Scanner:
    def __init__(self, source, config: ScanConfig | None = None, model: RegionModel | None = None,
                 status=print):
        self.source = source
        self.model = model or RegionModel()
        self.status = status
        self.rec_dir: Path | None = None
        self.rec_tag = ""
        self.windows: list[WindowState] = []
        self.configure(config or ScanConfig())

    # ------------------------------------------------------------ configuration

    def configure(self, config: ScanConfig) -> None:
        config = config.validated()
        planned = self.source.plan(*config.band)
        gain_range = self.source.info.get("gain_range_db")
        if gain_range and not gain_range[0] <= config.gain <= gain_range[1]:
            raise ValueError(f"gain must be {gain_range[0]:g}-{gain_range[1]:g} dB for this RX frontend")
        self._close_recorders()
        self.config = config
        self.tracker = Tracker(config.w, detect_wifi=config.detect_wifi)
        self.windows = []
        for win in planned:
            gain = self._auto_gain(win) if config.auto_gain else config.gain
            self.source.set_gain(gain)
            gain = self.source.gain
            self.windows.append(WindowState(win, gain, collections.deque(maxlen=config.w)))
        self._next = 0
        if self.rec_dir is not None:
            self._open_recorders()

    def _auto_gain(self, win: Window) -> float:
        g = self.config.gain
        minimum = self.source.info.get("gain_range_db", [0.0, 76.0])[0]
        while True:
            self.source.set_gain(g)
            worst = max(self.source.sweep(win.start_mhz).overload_frac for _ in range(2))
            if worst <= AUTO_GAIN_OK or g <= minimum:
                break
            next_gain = max(minimum, g - AUTO_GAIN_STEP_DB)
            if next_gain == g:
                break
            g = next_gain
        self.status(f"auto-gain [{win.label()}]: {self.source.gain:.0f} dB (overload {worst:.1e})")
        return self.source.gain

    def clear(self) -> None:
        self.tracker = Tracker(self.config.w, detect_wifi=self.config.detect_wifi)
        for ws in self.windows:
            ws.buffer.clear()
            ws.last_boxes = []

    # ------------------------------------------------------------ recording

    def start_recording(self, directory: Path, tag: str = "") -> list[str]:
        self._close_recorders()
        self.rec_dir, self.rec_tag = Path(directory), tag
        return self._open_recorders()

    def stop_recording(self) -> list[str]:
        files = self._close_recorders()
        self.rec_dir = None
        return files

    def _open_recorders(self) -> list[str]:
        files = []
        for ws in self.windows:
            self.source.set_gain(ws.gain)
            info = dict(self.source.info, gain_db=ws.gain)
            ws.recorder = WindowRecorder(self.rec_dir, ws.window.start_mhz, info, self.rec_tag)
            files.append(str(ws.recorder.npy))
        return files

    def _close_recorders(self) -> list[str]:
        files = []
        for ws in self.windows:
            if ws.recorder is not None:
                ws.recorder.flush()
                if ws.recorder.rows:
                    files.append(f"{ws.recorder.npy} ({len(ws.recorder.rows)} sweeps)")
                ws.recorder = None
        return files

    @property
    def recording(self) -> bool:
        return self.rec_dir is not None

    # ------------------------------------------------------------ scanning

    def step(self) -> dict:
        ws = self.windows[self._next]
        self._next = (self._next + 1) % len(self.windows)
        win, w = ws.window, self.config.w
        if self.source.gain != ws.gain:
            self.source.set_gain(ws.gain)
        r = self.source.sweep(win.start_mhz)
        if np.asarray(r.db).shape != (N_BINS,):
            raise ValueError(f"source returned {np.asarray(r.db).shape}, expected ({N_BINS},)")
        ws.visits += 1
        ws.last_sweep_s, ws.last_overload = r.duration_s, r.overload_frac
        accepted = (r.bad_bursts == 0 and math.isfinite(r.overload_frac) and
                    0 <= r.overload_frac <= OVERLOAD_WARN and np.isfinite(r.db).all())
        if accepted:
            ws.buffer.append(r.db)
        else:
            # The encoder needs consecutive undistorted sweeps. Bridging a bad
            # row changes its temporal statistics just as feeding clipping does.
            ws.buffer.clear()
        if ws.recorder is not None:
            ws.recorder.add(r)
        display_db = np.nan_to_num(r.db, nan=-120.0, posinf=-120.0, neginf=-120.0)
        rows = np.asarray(ws.buffer) if ws.buffer else np.asarray([display_db])
        floor = instrument_floor(np.median(rows, axis=0))
        boxes, events = [], []
        if accepted and len(ws.buffer) >= w:
            _, boxes = self.model.detect(rows, win.start_mhz, w)
            boxes = merge_boxes([b for b in boxes if win.owns(b.center_mhz)])
            tracked = [b for b in boxes if self.config.in_band(b.center_mhz)]
            events = self.tracker.update(win.index, ws.visits, tracked, now=r.t_start)
        else:
            events = self.tracker.update(win.index, ws.visits, [], now=r.t_start)
        ws.last_boxes = boxes
        targets = self.tracker.classes
        return {
            "t": round(r.t_start, 3),
            "window": win.index,
            "start": win.start_mhz,
            "visit": ws.visits,
            "fill": len(ws.buffer),
            "accepted": bool(accepted),
            "bad_bursts": r.bad_bursts,
            "w": w,
            "gain": ws.gain,
            "sweep_s": round(r.duration_s, 3),
            "overload": float(f"{r.overload_frac:.3g}"),
            "floor_db": round(float(np.median(floor)), 1),
            "image_fixes": int(r.extra.get("image_points", 0)),
            "rel": np.round((display_db - floor) * 10).astype(int).tolist(),  # 0.1 dB above the noise floor
            "boxes": [{"cls": b.cls, "lo": round(b.lo_mhz, 1), "hi": round(b.hi_mhz, 1),
                       "score": round(b.score, 3), "target": b.cls in targets,
                       "in_band": self.config.in_band(b.center_mhz)} for b in boxes],
            "events": [{"kind": k, **t.to_dict()} for k, t in events],
            "tracks": [t.to_dict() for t in self.tracker.active()],
        }

    def state(self) -> dict:
        return {
            "config": {**asdict(self.config), "band": list(self.config.band)},
            "windows": [{"index": ws.window.index, "start": ws.window.start_mhz, "stop": ws.window.stop_mhz,
                         "own_lo": max(ws.window.own_lo_mhz, -1.0), "own_hi": min(ws.window.own_hi_mhz, 1e5),
                         "gain": ws.gain, "fill": len(ws.buffer), "visits": ws.visits}
                        for ws in self.windows],
            "n_bins": N_BINS,
            "classes": CLASSES,
            "targets": sorted(self.tracker.classes),
            "recording": self.recording,
            "rec_tag": self.rec_tag,
            "device": self.source.info,
        }

    def close(self) -> list[str]:
        return self._close_recorders()
