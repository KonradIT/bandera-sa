"""Scan window plan, after the APK's k7/g: 1800 MHz windows, 1660 MHz stride, 140 MHz overlap."""
from __future__ import annotations

from dataclasses import dataclass

from .b210 import FREQ_MAX_MHZ, FREQ_MIN_MHZ
from .model import N_BINS, SPAN_MHZ, STEP_MHZ

WINDOW_STRIDE_MHZ = 1660.0
LAST_POINT_MHZ = (N_BINS - 1) * STEP_MHZ  # 1797: the app drops the point at start + 1800


@dataclass
class Window:
    index: int
    start_mhz: float
    own_lo_mhz: float  # boxes centred outside [own_lo, own_hi) belong to a neighbouring window
    own_hi_mhz: float

    @property
    def stop_mhz(self) -> float:
        return self.start_mhz + LAST_POINT_MHZ

    def owns(self, center_mhz: float) -> bool:
        return self.own_lo_mhz <= center_mhz < self.own_hi_mhz

    def label(self) -> str:
        return f"{self.start_mhz:.0f}-{self.stop_mhz:.0f}"


def plan_windows(lo_mhz: float, hi_mhz: float,
                 fmin: float = FREQ_MIN_MHZ, fmax: float = FREQ_MAX_MHZ) -> list[Window]:
    if hi_mhz <= lo_mhz:
        raise ValueError("empty band")
    max_start = fmax - SPAN_MHZ  # keeps the last LO tune (start + 1798.5) inside the AD9361 range
    clamp = lambda s: min(max(s, fmin), max_start)
    if hi_mhz - lo_mhz <= SPAN_MHZ:
        starts = [clamp((lo_mhz + hi_mhz) / 2 - SPAN_MHZ / 2)]
    else:
        starts = []
        s = lo_mhz
        while s + SPAN_MHZ < hi_mhz:
            starts.append(clamp(s))
            s += WINDOW_STRIDE_MHZ
        starts.append(clamp(hi_mhz - SPAN_MHZ))
        starts = sorted(set(starts))
    windows = []
    for i, s in enumerate(starts):
        own_lo = -1e9 if i == 0 else (s + starts[i - 1] + LAST_POINT_MHZ) / 2
        own_hi = 1e9 if i == len(starts) - 1 else (starts[i + 1] + s + LAST_POINT_MHZ) / 2
        windows.append(Window(i, s, own_lo, own_hi))
    return windows
