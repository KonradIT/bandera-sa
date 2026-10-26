"""Simplified port of the app's temporal confirmation (k7/z + RegionGatePolicy.modelB).

Kept from the APK:
  * only target classes are tracked (analog, dji, autel, siyi, wfb_ng; + wifi on request)
  * match = same class and (1-D IoU >= 0.3 or centres within 5 MHz); centre is an EMA, alpha 0.5
  * a hit counts at most once per W visits of the window; 2 hits confirm
  * instant confirm on the first hit if score >= 0.5 (dji/autel/wfb_ng need 0.65 at W=8); siyi never
  * a track unmatched for 2W visits ages out
Omitted: the provisional-track retract rules (duty < 0.6, churn > 1/3, gap >= 3 over 4 W-slots).
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

from .model import Box

TARGET_CLASSES = ("analog", "dji", "autel", "siyi", "wfb_ng")
INSTANT_SCORE = 0.5
INSTANT_SCORE_W8 = {"dji": 0.65, "autel": 0.65, "wfb_ng": 0.65}
NO_INSTANT = {"siyi"}
MATCH_IOU = 0.3
MATCH_CENTER_MHZ = 5.0
CONFIRM_HITS = 2
HISTORY_LEN = 64
MERGE_IOU = 0.3


def merge_boxes(boxes: list[Box], iou: float = MERGE_IOU) -> list[Box]:
    """RegionDetectionAggregator.mergeBoxes: same-class boxes with IoU > 0.3 become one (union, max score)."""
    out: list[Box] = []
    for b in sorted(boxes, key=lambda b: -b.score):
        for i, m in enumerate(out):
            if m.cls == b.cls and m.iou(b) > iou:
                out[i] = Box(m.cls, min(m.lo_mhz, b.lo_mhz), max(m.hi_mhz, b.hi_mhz), m.score)
                break
        else:
            out.append(b)
    return out


@dataclass
class Track:
    id: int
    window: int
    cls: str
    lo_mhz: float
    hi_mhz: float
    center_mhz: float
    score: float
    best_score: float
    first_visit: int
    last_seen_visit: int
    last_hit_visit: int
    hits: int = 1
    confirmed: bool = False
    instant: bool = False
    first_t: float = 0.0
    last_t: float = 0.0
    history: list = field(default_factory=list)

    def as_box(self) -> Box:
        return Box(self.cls, self.lo_mhz, self.hi_mhz, self.score)

    def to_dict(self) -> dict:
        return {"id": self.id, "window": self.window, "cls": self.cls, "lo": round(self.lo_mhz, 1),
                "hi": round(self.hi_mhz, 1), "center": round(self.center_mhz, 1), "score": round(self.score, 3),
                "best": round(self.best_score, 3), "hits": self.hits, "confirmed": self.confirmed,
                "instant": self.instant, "first_t": round(self.first_t, 3), "last_t": round(self.last_t, 3)}


class Tracker:
    def __init__(self, w: int, detect_wifi: bool = False):
        self.w = w
        self.classes = set(TARGET_CLASSES) | ({"wifi"} if detect_wifi else set())
        self.tracks: list[Track] = []
        self._ids = itertools.count(1)

    def _instant_threshold(self, cls: str) -> float | None:
        if cls in NO_INSTANT:
            return None
        if self.w == 8 and cls in INSTANT_SCORE_W8:
            return INSTANT_SCORE_W8[cls]
        return INSTANT_SCORE

    @staticmethod
    def _matches(t: Track, b: Box) -> bool:
        return t.cls == b.cls and (t.as_box().iou(b) >= MATCH_IOU
                                   or abs(t.center_mhz - b.center_mhz) <= MATCH_CENTER_MHZ)

    def update(self, window: int, visit: int, boxes: list[Box], now: float | None = None) -> list[tuple[str, Track]]:
        """Feed one window visit. Returns events: ('new'|'confirmed'|'lost', track)."""
        t_now = time.time() if now is None else now
        events = []
        live = [t for t in self.tracks if t.window == window]
        used = set()
        for b in sorted((b for b in boxes if b.cls in self.classes), key=lambda b: -b.score):
            t = next((t for t in live if id(t) not in used and self._matches(t, b)), None)
            if t is None and any(self._matches(u, b) for u in live):
                continue  # a second box on a track already hit this visit: not a new emitter
            if t is None:
                t = Track(next(self._ids), window, b.cls, b.lo_mhz, b.hi_mhz, b.center_mhz, b.score,
                          b.score, visit, visit, visit, first_t=t_now)
                self.tracks.append(t)
                live.append(t)
                events.append(("new", t))
                thr = self._instant_threshold(b.cls)
                if thr is not None and b.score >= thr:
                    t.confirmed = t.instant = True
                    events.append(("confirmed", t))
            else:
                t.center_mhz = 0.5 * t.center_mhz + 0.5 * b.center_mhz
                t.lo_mhz, t.hi_mhz, t.score = b.lo_mhz, b.hi_mhz, b.score
                t.best_score = max(t.best_score, b.score)
                t.last_seen_visit = visit
                if visit - t.last_hit_visit >= self.w:
                    t.hits += 1
                    t.last_hit_visit = visit
                if not t.confirmed and t.hits >= CONFIRM_HITS:
                    t.confirmed = True
                    events.append(("confirmed", t))
            used.add(id(t))
            t.last_t = t_now
            t.history.append((visit, round(b.score, 3)))
            del t.history[:-HISTORY_LEN]
        for t in live:
            if id(t) not in used and visit - t.last_seen_visit > 2 * self.w:
                self.tracks.remove(t)
                events.append(("lost", t))
        return events

    def active(self, window: int | None = None) -> list[Track]:
        return [t for t in self.tracks if window is None or t.window == window]
