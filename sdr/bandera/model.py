"""Frontera 2.22 region detector "model B" (RY_0_1_6_s_soup8b), ported from the APK.

Pipeline, mirroring com.example.frontera_chart.data.ml.region:
  W sweeps x 600 bins (dBm, 3 MHz/bin)
    -> encode()         RegionEncoding4: 3 floor-relative channels      [1, W, 600, 3]
    -> rowsums model    conv encoder + sum/sumsq/lag/max over sweeps    [1, 1, 300, 128]
    -> features()       RegionBAccumulator (lambda = 0)                 [1, 1, 300, 129]
    -> detect model     1-D FPN, 263 positions x (obj, 9 cls, 2 dist)   [1, 1, 263, 12]
    -> decode()         RegionBDecoder: sigmoid, thresholds, NMS        list[Box]
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

MODEL_TAG = "RY_0_1_6_s_soup8b"
MODELS_DIR = Path(__file__).resolve().parent.parent.parent / "models"  # repo-root/models

CLASSES = ["analog", "dji", "autel", "siyi", "wifi", "lte", "other_signals", "gnss", "wfb_ng"]
N_BINS = 600
STEP_MHZ = 3.0
SPAN_MHZ = N_BINS * STEP_MHZ
SUPPORTED_W = (8, 10, 12, 16)
OBJ_THRESHOLD = {8: 0.4, 10: 0.3, 12: 0.25, 16: 0.25}
CLASS_MIN_SCORE = {"siyi": 0.6, "wfb_ng": 0.5}
NMS_IOU = 0.4
STRIDES = (4, 8, 16)
GOLDEN_LOGIT_TOL = 0.005
GOLDEN_DIST_TOL = 0.02


@dataclass
class Box:
    cls: str
    lo_mhz: float
    hi_mhz: float
    score: float

    @property
    def center_mhz(self) -> float:
        return (self.lo_mhz + self.hi_mhz) / 2

    def iou(self, other: "Box") -> float:
        inter = max(0.0, min(self.hi_mhz, other.hi_mhz) - max(self.lo_mhz, other.lo_mhz))
        union = (self.hi_mhz - self.lo_mhz) + (other.hi_mhz - other.lo_mhz) - inter
        return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------- encoding

def _windows(x: np.ndarray, half: int) -> np.ndarray:
    """Rows of 2*half+1 neighbours with the APK's reflect() edge rule (numpy 'reflect')."""
    return sliding_window_view(np.pad(x, half, mode="reflect"), 2 * half + 1)


def instrument_floor(prof: np.ndarray) -> np.ndarray:
    n = len(prof)
    m = min(151, (n // 2) * 2 - 1)
    floor = np.percentile(_windows(prof, max(1, m // 2)), 20, axis=1).astype(np.float32)
    floor = np.minimum(floor, np.float32(np.median(prof)))
    m2 = min(31, (n // 2) * 2 - 1)
    return _windows(floor, max(1, m2 // 2)).mean(axis=1, dtype=np.float32)


def encode(sweeps: np.ndarray) -> np.ndarray:
    """RegionEncoding4.encodeNhwc: [W, 600] dBm -> [1, W, 600, 3] float32."""
    sw = np.asarray(sweeps, np.float32)
    med = np.median(sw, axis=0).astype(np.float32)
    floor = instrument_floor(med)
    plateau = np.median(_windows(med, 25), axis=1).astype(np.float32)
    level = np.clip((sw - floor) / 20.0, -0.3, 3.0)
    burst = np.clip((sw - med) / 10.0, -1.0, 3.0)
    plat = np.broadcast_to(np.clip((plateau - floor) / 20.0, -0.3, 3.0), sw.shape)
    return np.stack([level, burst, plat], axis=-1)[None].astype(np.float32)


def features(rowsums: np.ndarray, w: int) -> np.ndarray:
    """RegionBAccumulator.features for a single visit: [300*128] -> [1, 1, 300, 129]."""
    r = rowsums.reshape(300, 4, 32)  # sum, sum of squares, lag-1 product sum, max
    n, n1 = float(w), float(max(w - 1, 0))
    mean = r[:, 0] / n
    var = r[:, 1] / n - mean ** 2
    lag = r[:, 2] / n1 - mean ** 2 if n1 > 0 else np.zeros_like(mean)
    inv = np.full((300, 1), 1.0 / np.sqrt(n), np.float32)
    return np.concatenate([mean, var, r[:, 3], lag, inv], axis=1)[None, None].astype(np.float32)


# ---------------------------------------------------------------- decoding

def level_centers(n_freq: int = N_BINS) -> np.ndarray:
    conv = lambda n: (n + 2 - 3) // 2 + 1  # k=3, s=2, p=1
    lens = [conv(conv(n_freq))]
    lens += [conv(lens[-1])]
    lens += [conv(lens[-1])]
    return np.concatenate([(np.arange(l) + 0.5) * s for l, s in zip(lens, STRIDES)])


_CENTERS = level_centers()


def _sigmoid(v):
    return 1.0 / (1.0 + np.exp(-np.clip(v, -20.0, 20.0)))


def decode(out: np.ndarray, start_mhz: float, w: int, threshold: float | None = None) -> list[Box]:
    o = np.asarray(out, np.float32).reshape(-1, 12)
    thr = OBJ_THRESHOLD[w] if threshold is None else threshold
    end = start_mhz + N_BINS * STEP_MHZ
    obj = _sigmoid(o[:, 0])
    scores = _sigmoid(o[:, 1:10]) * obj[:, None]
    boxes = []
    for p, k in zip(*np.nonzero((scores >= thr) & (obj[:, None] >= thr))):
        cls = CLASSES[k]
        if scores[p, k] < CLASS_MIN_SCORE.get(cls, 0.0):
            continue
        lo, hi = sorted((_CENTERS[p] - o[p, 10], _CENTERS[p] + o[p, 11]))
        lo = max(start_mhz, lo * STEP_MHZ + start_mhz)
        hi = min(end, hi * STEP_MHZ + start_mhz)
        if hi > lo:
            boxes.append(Box(cls, float(lo), float(hi), float(scores[p, k])))
    return sorted(nms(boxes, NMS_IOU), key=lambda b: -b.score)


def nms(boxes: list[Box], iou_thr: float) -> list[Box]:
    keep = []
    for cls in {b.cls for b in boxes}:
        group = sorted((b for b in boxes if b.cls == cls), key=lambda b: -b.score)
        alive = [True] * len(group)
        for i, b in enumerate(group):
            if not alive[i]:
                continue
            keep.append(b)
            for j in range(i + 1, len(group)):
                if alive[j] and b.iou(group[j]) > iou_thr:
                    alive[j] = False
    return keep


# ---------------------------------------------------------------- runtime

class RegionModel:
    """LiteRT runner for the APK's rowsums_w{W} + detect graphs."""

    def __init__(self, models_dir: Path = MODELS_DIR, threads: int = 2):
        from ai_edge_litert.interpreter import Interpreter

        self._interp_cls = Interpreter
        self.models_dir = Path(models_dir)
        self.threads = threads
        self._cache: dict[str, object] = {}

    def _run(self, name: str, x: np.ndarray) -> np.ndarray:
        it = self._cache.get(name)
        if it is None:
            it = self._interp_cls(model_path=str(self.models_dir / name), num_threads=self.threads)
            it.allocate_tensors()
            self._cache[name] = it
        it.set_tensor(it.get_input_details()[0]["index"], x)
        it.invoke()
        return it.get_tensor(it.get_output_details()[0]["index"])

    def rowsums(self, w: int, enc: np.ndarray) -> np.ndarray:
        return self._run(f"{MODEL_TAG}_rowsums_w{w}_fp32.tflite", enc)

    def detect_raw(self, feats: np.ndarray) -> np.ndarray:
        return self._run(f"{MODEL_TAG}_detect_fp32.tflite", feats).reshape(-1, 12)

    @staticmethod
    def pick_w(n_sweeps: int) -> int | None:
        fits = [w for w in SUPPORTED_W if w <= n_sweeps]
        return max(fits) if fits else None

    def infer(self, sweeps: np.ndarray, w: int | None = None) -> tuple[int, np.ndarray]:
        """Raw [263, 12] output for the newest W rows (LiteRtBBackend.detect)."""
        sweeps = np.asarray(sweeps, np.float32)
        w = w or self.pick_w(len(sweeps))
        if w is None:
            raise ValueError(f"need >= {min(SUPPORTED_W)} sweeps, got {len(sweeps)}")
        return w, self.detect_raw(features(self.rowsums(w, encode(sweeps[-w:])), w))

    def detect(self, sweeps: np.ndarray, start_mhz: float, w: int | None = None) -> tuple[int, list[Box]]:
        w, out = self.infer(sweeps, w)
        return w, decode(out, start_mhz, w)

    def self_test(self) -> dict[int, tuple[float, float]]:
        """Golden check from LiteRtBBackend.start: returns {W: (max logit err, max dist err)}."""
        sweeps, outputs = load_golden(self.models_dir / f"{MODEL_TAG}_golden.bin")
        result = {}
        for w, ref in outputs.items():
            off = (len(sweeps) - w) // 2  # RegionBGolden.crop
            _, out = self.infer(sweeps[off:off + w], w)
            err = np.abs(out - ref)
            result[w] = (float(err[:, :10].max()), float(err[:, 10:].max()))
        return result


def load_golden(path: Path) -> tuple[np.ndarray, dict[int, np.ndarray]]:
    data = Path(path).read_bytes()
    if data[:4] != b"RBG1":
        raise ValueError("not a model-B golden file")
    n_sweeps, n_freq, n_pos, per_pos = struct.unpack("<4i", data[4:20])
    off = 20
    sweeps = np.frombuffer(data, "<f4", n_sweeps * n_freq, off).reshape(n_sweeps, n_freq)
    off += n_sweeps * n_freq * 4
    outputs = {}
    while off + 4 <= len(data):
        (w,) = struct.unpack_from("<i", data, off)
        off += 4
        outputs[w] = np.frombuffer(data, "<f4", n_pos * per_pos, off).reshape(n_pos, per_pos)
        off += n_pos * per_pos * 4
    return sweeps.copy(), outputs
