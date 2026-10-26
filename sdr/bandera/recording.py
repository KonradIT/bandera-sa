"""Sweep recordings: float32 (n_sweeps, 600) .npy plus a JSON sidecar.

Layout follows the APK's dataset_regions export (raw binned levels, start/stop/step, sdr_device,
boxes [{lo_mhz, hi_mhz, label}]) so recordings can be labelled the same way. Levels here are
SDR dB (power relative to full scale at the given gain), not calibrated dBm; the model only
sees floor-relative values, so the absolute offset does not matter to it.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .model import MODEL_TAG, N_BINS, STEP_MHZ


def utc_stamp(t: float | None = None) -> str:
    return datetime.fromtimestamp(t or time.time(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class WindowRecorder:
    def __init__(self, directory: Path, start_mhz: float, device: dict, tag: str = "", flush_every: int = 10):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        name = f"{tag + '_' if tag else ''}{start_mhz:.0f}-{start_mhz + (N_BINS - 1) * STEP_MHZ:.0f}mhz_{utc_stamp()}"
        self.npy = self.dir / f"{name}.npy"
        self.sidecar = self.dir / f"{name}.json"
        self.rows: list[np.ndarray] = []
        self.meta = {
            "format": "bandera-sweeps-v1",
            "start_mhz": start_mhz,
            "stop_mhz": start_mhz + (N_BINS - 1) * STEP_MHZ,
            "step_mhz": STEP_MHZ,
            "n_bins": N_BINS,
            "n_sweeps": 0,
            "units": "dB re SDR full scale (uncalibrated unless a measured level offset was supplied)",
            "tag": tag,
            "sdr_device": device,
            "model": MODEL_TAG,
            "created_utc": utc_stamp(),
            "sweep_t": [],
            "sweep_duration_s": [],
            "overload_frac": [],
            "bad_bursts": [],
            "sweep_extra": [],
            "boxes": [],
        }
        self.flush_every = flush_every

    def add(self, result) -> None:
        self.rows.append(result.db)
        self.meta["sweep_t"].append(round(result.t_start, 3))
        self.meta["sweep_duration_s"].append(round(result.duration_s, 3))
        self.meta["overload_frac"].append(float(f"{result.overload_frac:.3g}"))
        self.meta["bad_bursts"].append(int(result.bad_bursts))
        self.meta["sweep_extra"].append(dict(result.extra))
        if len(self.rows) % self.flush_every == 0:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        self.meta["n_sweeps"] = len(self.rows)
        tmp = self.npy.with_suffix(".tmp.npy")
        np.save(tmp, np.asarray(self.rows, np.float32))
        os.replace(tmp, self.npy)
        self.sidecar.write_text(json.dumps(self.meta, indent=1))


def load_recording(path: Path) -> tuple[np.ndarray, dict]:
    path = Path(path)
    sweeps = np.load(path)
    side = path.with_suffix(".json")
    meta = json.loads(side.read_text()) if side.exists() else {}
    if sweeps.ndim != 2 or sweeps.shape[1] != N_BINS or len(sweeps) == 0:
        raise ValueError(f"recording must contain one or more {N_BINS}-point sweeps")
    for key in ("overload_frac", "bad_bursts", "sweep_extra"):
        if key in meta and len(meta[key]) != len(sweeps):
            raise ValueError(f"recording {key} metadata length does not match sweeps")
    return sweeps.astype(np.float32), meta


def valid_sweep_mask(sweeps: np.ndarray, meta: dict, overload_limit: float) -> np.ndarray:
    """Quality mask shared by offline replay and plots; old files default clean."""
    overload = np.asarray(meta.get("overload_frac", np.zeros(len(sweeps))), dtype=float)
    bad = np.asarray(meta.get("bad_bursts", np.zeros(len(sweeps))), dtype=float)
    return (np.isfinite(sweeps).all(axis=1) & np.isfinite(overload) & (overload >= 0) &
            (overload <= overload_limit) & (bad == 0))
