"""bandera: run the Frontera 2.22 drone-signal model on LibreSDR/B210 sweeps.

    python -m bandera selftest
    python -m bandera web --band 2300-2550          # live page at http://127.0.0.1:8765/
    python -m bandera sweep-test --start 1600
    python -m bandera run --band 2300-2550 --record captures/rec --tag dji_mini4
    python -m bandera replay captures/rec/<file>.npy
    python -m bandera plot captures/rec/<file>.npy
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .model import CLASSES, GOLDEN_DIST_TOL, GOLDEN_LOGIT_TOL, N_BINS, STEP_MHZ, SUPPORTED_W, RegionModel

CAPTURES = Path(__file__).resolve().parent.parent / "captures"


def _fmt_boxes(boxes, targets_only=False, targets=()):
    shown = [b for b in boxes if not targets_only or b.cls in targets]
    return " | ".join(f"{b.cls} {b.lo_mhz:.0f}-{b.hi_mhz:.0f} {b.score:.2f}" for b in shown) or "-"


def _sweeper(a, flatten=None):
    from .b210 import B210Sweeper
    return B210Sweeper(args=a.args, gain=a.gain, antenna=a.antenna, channel=a.channel,
                       readings=a.readings, averaging=a.averaging,
                       flatten=(not a.no_flatten) if flatten is None else flatten,
                       image_check=not a.no_image_check)


def _parse_band(text: str) -> tuple[float, float]:
    lo, hi = (float(x) for x in text.replace(" ", "").split("-"))
    return lo, hi


# ---------------------------------------------------------------- commands

def cmd_selftest(a) -> int:
    res = RegionModel().self_test()
    ok = all(l <= GOLDEN_LOGIT_TOL and d <= GOLDEN_DIST_TOL for l, d in res.values())
    for w, (l, d) in sorted(res.items()):
        print(f"W={w:2d}  max logit err {l:.2e} (tol {GOLDEN_LOGIT_TOL})  max dist err {d:.2e} (tol {GOLDEN_DIST_TOL})")
    print("golden self-test", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def cmd_sweep_test(a) -> int:
    from .model import instrument_floor
    s = _sweeper(a)
    print("device:", json.dumps(s.info))
    rows = []
    for i in range(a.sweeps):
        r = s.sweep(a.start)
        rows.append(r.db)
        img = (f", image fixes {r.extra['image_points']} pts in {r.extra['image_tunes']} tunes "
               f"(median drop {r.extra['image_drop_db']} dB, max {r.extra['image_max_drop_db']} dB, "
               f"{r.extra['image_s'] * 1e3:.0f} ms)") if r.extra else ""
        print(f"sweep {i + 1}: {r.duration_s * 1e3:.0f} ms, overload {r.overload_frac:.1e}, bad bursts {r.bad_bursts}{img}")
    x = np.asarray(rows)
    med = np.median(x, axis=0)
    floor = instrument_floor(med)
    quiet = med - floor < 1.5
    print(f"floor median {np.median(floor):.1f} dB | quiet bins {quiet.sum()} | "
          f"temporal std in quiet bins {x[:, quiet].std(axis=0, ddof=1).mean():.2f} dB (Arinst reference 1.13)")
    slot = np.arange(N_BINS) % 16
    dev = (med - floor)[quiet]
    print("per-slot median offset in quiet bins (slot = grid index mod 16):",
          np.round([np.median(dev[slot[quiet] == j]) for j in range(16)], 2))
    if a.save:
        np.save(a.save, x.astype(np.float32))
        print("saved", a.save)
    return 0


def cmd_calibrate(a) -> int:
    """Per-slot passband ripple across the band, from quiet (noise-only) bins."""
    from .b210 import FLATNESS_FILE, SLOTS
    from .model import instrument_floor
    from .plan import plan_windows
    from .recording import utc_stamp

    s = _sweeper(a, flatten=False)
    devs, slots, per_window = [], [], {}
    for win in plan_windows(*_parse_band(a.band)):
        x = np.asarray([s.sweep(win.start_mhz).db for _ in range(a.sweeps)])
        med = np.median(x, axis=0)
        quiet = med - instrument_floor(med) < 1.5
        k = np.nonzero(quiet)[0]
        dev = (med - instrument_floor(med))[k]
        devs.append(dev)
        slots.append(k % SLOTS)
        prof = [float(np.median(dev[k % SLOTS == j])) for j in range(SLOTS)]
        per_window[win.label()] = [round(v, 2) for v in prof]
        print(f"[{win.label()}] quiet bins {len(k)}: {np.round(prof, 2)}")
    dev, slot = np.concatenate(devs), np.concatenate(slots)
    offsets = np.array([np.median(dev[slot == j]) for j in range(SLOTS)])
    offsets -= offsets.mean()
    out = Path(a.out) if a.out else FLATNESS_FILE
    out.write_text(json.dumps({"created_utc": utc_stamp(), "gain_db": s.gain, "band": a.band,
                               "slot_offsets_db": [round(float(v), 3) for v in offsets],
                               "per_window": per_window}, indent=1))
    print("slot offsets (subtracted from each sweep):", np.round(offsets, 2))
    print("wrote", out)
    return 0


def cmd_run(a) -> int:
    from .recording import utc_stamp
    from .scanner import OVERLOAD_WARN, LiveSource, ScanConfig, Scanner

    cfg = ScanConfig(_parse_band(a.band), a.w, a.gain, a.auto_gain, a.detect_wifi, a.whole_window)
    scanner = Scanner(LiveSource(_sweeper(a)), cfg)
    if a.record:
        scanner.start_recording(Path(a.record), a.tag)
    targets = set(scanner.tracker.classes)
    labels = {ws.window.index: ws.window.label() for ws in scanner.windows}
    log_path = Path(a.log) if a.log else CAPTURES / f"run_{utc_stamp()}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")
    n_win = len(scanner.windows)
    print(f"windows: {', '.join(f'{ws.window.label()}@{ws.gain:.0f}dB' for ws in scanner.windows)} MHz "
          f"| W={a.w} | log {log_path}")
    print(f"first detections after {a.w} sweeps of each window (~{a.w * n_win * 1.3:.0f} s); Ctrl+C to stop")
    steps = 0
    try:
        while a.rounds == 0 or steps < a.rounds * n_win:
            ev = scanner.step()
            steps += 1
            warn = f" OVERLOAD {ev['overload']:.1e}" if ev["overload"] > OVERLOAD_WARN else ""
            head = f"{datetime.now():%H:%M:%S} [{labels[ev['window']]}] #{ev['visit']} {ev['sweep_s']:.2f}s"
            if ev["fill"] < ev["w"]:
                print(f"{head} filling {ev['fill']}/{ev['w']}{warn}")
                continue
            shown = [b for b in ev["boxes"] if not a.targets_only or b["target"]]
            line = " | ".join(f"{b['cls']} {b['lo']:.0f}-{b['hi']:.0f} {b['score']:.2f}" for b in shown) or "-"
            print(f"{head}  {line}{warn}")
            for e in ev["events"]:
                if e["kind"] == "confirmed":
                    how = "instant" if e["instant"] else f"{e['hits']} hits"
                    print(f"  >>> CONFIRMED {e['cls']} {e['lo']:.0f}-{e['hi']:.0f} MHz score {e['score']:.2f} ({how})")
                elif e["kind"] == "lost":
                    print(f"  --- lost {e['cls']} ~{e['center']:.0f} MHz (best {e['best']:.2f})")
            log.write(json.dumps({k: ev[k] for k in ev if k != "rel"}) + "\n")
            log.flush()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        for f in scanner.close():
            print("recorded", f)
        log.close()
    return 0


def cmd_web(a) -> int:
    from .scanner import LiveSource, ReplaySource, ScanConfig, Scanner
    from .web import Hub, serve

    if a.replay:
        source = ReplaySource(Path(a.replay), speed=a.speed)
        band, mode, auto = (source.start_mhz, source.start_mhz + 1797.0), "replay", False
    else:
        source = LiveSource(_sweeper(a))
        band, mode, auto = _parse_band(a.band), "live", a.auto_gain
    from .recording import utc_stamp
    scanner = Scanner(source, ScanConfig(band, a.w, a.gain, auto, a.detect_wifi, a.whole_window))
    log_path = Path(a.log) if a.log else Path(a.record_dir) / f"web_{utc_stamp()}.jsonl"
    serve(scanner, Hub(), a.host, a.port, mode, Path(a.record_dir), log_path)
    return 0


def _replay(sweeps, start, w, every, detect_wifi):
    from .tracker import Tracker, merge_boxes
    model = RegionModel()
    tracker = Tracker(w, detect_wifi)
    steps = []
    for i in range(w, len(sweeps) + 1, every):
        _, boxes = model.detect(sweeps[i - w:i], start, w)
        boxes = merge_boxes(boxes)
        events = tracker.update(0, i, boxes)
        steps.append((i, boxes, events))
    return steps, tracker


def cmd_replay(a) -> int:
    from .recording import load_recording
    from .tracker import TARGET_CLASSES
    sweeps, meta = load_recording(a.file)
    start = a.start if a.start is not None else meta.get("start_mhz")
    if start is None:
        print("no start frequency: pass --start or keep the .json sidecar next to the .npy")
        return 2
    steps, tracker = _replay(sweeps, start, a.w, a.every, a.detect_wifi)
    targets = set(tracker.classes)
    for i, boxes, events in steps:
        line = _fmt_boxes(boxes, a.targets_only, targets)
        ev = " ".join(f"[{k} {t.cls} {t.center_mhz:.0f}]" for k, t in events if k != "new")
        print(f"sweeps {i - a.w:4d}-{i - 1:4d}: {line} {ev}")
    print("\nsummary (all windows of the recording):")
    agg = collections.defaultdict(list)
    seen = collections.Counter()
    for _, boxes, _ in steps:
        for b in boxes:
            agg[b.cls].append(b)
        seen.update({b.cls for b in boxes})
    for cls in CLASSES:
        if cls in agg:
            bs = agg[cls]
            print(f"  {cls:13s} in {seen[cls]:4d}/{len(steps)} steps | max score "
                  f"{max(b.score for b in bs):.2f} | centres {np.percentile([b.center_mhz for b in bs], 10):.0f}"
                  f"-{np.percentile([b.center_mhz for b in bs], 90):.0f} MHz (p10-p90)")
    confirmed = {t.id: t for _, _, events in steps for k, t in events if k == "confirmed"}  # incl. later lost
    print("tracks confirmed during the recording:",
          ", ".join(f"{t.cls}@{t.center_mhz:.0f}MHz best {t.best_score:.2f}" for t in confirmed.values()) or "none")
    return 0


def cmd_plot(a) -> int:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from .recording import load_recording

    sweeps, meta = load_recording(a.file)
    start = a.start if a.start is not None else meta.get("start_mhz", 0.0)
    freqs = start + np.arange(sweeps.shape[1]) * STEP_MHZ
    steps, _ = _replay(sweeps, start, a.w, 1, a.detect_wifi) if len(sweeps) >= a.w else ([], None)
    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(16, 9), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    vmin, vmax = np.percentile(sweeps, [5, 99.5])
    ax0.imshow(sweeps, aspect="auto", origin="lower", cmap="viridis", vmin=vmin, vmax=vmax,
               extent=[freqs[0], freqs[-1] + STEP_MHZ, 0, len(sweeps)])
    colors = dict(zip(CLASSES, plt.cm.tab10.colors))
    for i, boxes, _ in steps:
        for b in boxes:
            ax0.plot([b.lo_mhz, b.hi_mhz], [i - 0.5, i - 0.5], color=colors[b.cls], lw=2.5, alpha=0.9)
    for cls, c in colors.items():
        ax0.plot([], [], color=c, lw=3, label=cls)
    ax0.legend(loc="upper right", fontsize=8, ncol=3)
    ax0.set_ylabel("sweep #  (boxes drawn at the newest sweep of each W-window)")
    ax0.set_title(f"{Path(a.file).name}  W={a.w}")
    ax1.plot(freqs, np.median(sweeps, axis=0), lw=0.8, label="median")
    ax1.plot(freqs, sweeps.max(axis=0), lw=0.6, alpha=0.6, label="max hold")
    ax1.set_xlabel("MHz")
    ax1.set_ylabel("dB")
    ax1.legend(fontsize=8)
    if a.band:
        ax1.set_xlim(*_parse_band(a.band))  # shared x: zooms the waterfall too
    out = Path(a.out) if a.out else Path(a.file).with_suffix(".png")
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    print("wrote", out)
    return 0


# ---------------------------------------------------------------- argparse

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bandera", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def radio(sp):
        sp.add_argument("--args", default="type=b200", help="UHD device args")
        sp.add_argument("--gain", type=float, default=60.0, help="RX gain dB (60 = no overload in a city)")
        sp.add_argument("--antenna", default="RX2")
        sp.add_argument("--channel", type=int, default=0)
        sp.add_argument("--readings", type=int, default=24, help="log readings averaged per point (24 ~ Arinst jitter)")
        sp.add_argument("--averaging", choices=["log", "lin"], default="log")
        sp.add_argument("--no-flatten", action="store_true", help="ignore flatness.json")
        sp.add_argument("--no-image-check", action="store_true",
                        help="skip re-measuring points that may be IQ mirror images of strong signals")

    def det(sp):
        sp.add_argument("--w", type=int, choices=SUPPORTED_W, default=16, help="sweeps per analysis window")
        sp.add_argument("--detect-wifi", action="store_true", help="treat wifi as a target class, like the app option")
        sp.add_argument("--targets-only", action="store_true", help="print only drone target classes")
        sp.add_argument("--whole-window", action="store_true",
                        help="track/alert on the whole 1800 MHz model window, not only --band")

    sp = sub.add_parser("selftest", help="golden self-test of the model port (no radio)")
    sp.set_defaults(fn=cmd_selftest)

    sp = sub.add_parser("sweep-test", help="a few sweeps: timing, overload, noise statistics")
    radio(sp)
    sp.add_argument("--start", type=float, default=1600.0)
    sp.add_argument("--sweeps", type=int, default=8)
    sp.add_argument("--save")
    sp.set_defaults(fn=cmd_sweep_test)

    sp = sub.add_parser("calibrate", help="measure per-slot passband ripple -> flatness.json")
    radio(sp)
    sp.add_argument("--band", default="70-6000")
    sp.add_argument("--sweeps", type=int, default=4, help="sweeps per window")
    sp.add_argument("--out")
    sp.set_defaults(fn=cmd_calibrate)

    sp = sub.add_parser("run", help="live detection")
    radio(sp)
    det(sp)
    sp.add_argument("--band", default="70-6000", help="MHz range, e.g. 2300-2550 (<=1800 MHz -> one centred window)")
    sp.add_argument("--rounds", type=int, default=0, help="stop after N rounds over all windows (0 = forever)")
    sp.add_argument("--auto-gain", action="store_true",
                    help="per-window fixed gain: start at --gain, drop 6 dB until overload <= 1e-4")
    sp.add_argument("--record", help="directory for raw sweep recordings (.npy + .json per window)")
    sp.add_argument("--tag", default="", help="label put in recording names, e.g. dji_mini4_100m")
    sp.add_argument("--log", help="JSONL detection log (default captures/run_<utc>.jsonl)")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("web", help="live web UI (waterfall, detections, alarm) at http://127.0.0.1:8765/")
    radio(sp)
    det(sp)
    sp.add_argument("--band", default="2300-2550", help="initial band in MHz (changeable in the page)")
    sp.add_argument("--auto-gain", action="store_true", help="per-window fixed gain chosen at startup")
    sp.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it from a phone on the same network")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--record-dir", default=str(CAPTURES / "web"), help="where the Record button writes")
    sp.add_argument("--log", help="JSONL detection log (default <record-dir>/web_<utc>.jsonl)")
    sp.add_argument("--replay", help="serve a recording (.npy) instead of the radio")
    sp.add_argument("--speed", type=float, default=1.0, help="replay speed factor")
    sp.set_defaults(fn=cmd_web)

    sp = sub.add_parser("replay", help="run the detector over a recording")
    det(sp)
    sp.add_argument("file")
    sp.add_argument("--start", type=float, help="window start MHz (default: from sidecar)")
    sp.add_argument("--every", type=int, default=1, help="slide step in sweeps")
    sp.set_defaults(fn=cmd_replay)

    sp = sub.add_parser("plot", help="waterfall PNG with detections")
    det(sp)
    sp.add_argument("file")
    sp.add_argument("--start", type=float)
    sp.add_argument("--band", help="zoom to MHz range, e.g. 5640-5960")
    sp.add_argument("--out")
    sp.set_defaults(fn=cmd_plot)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
