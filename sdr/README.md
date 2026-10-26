# bandera-sdr / sdr

This project runs the drone-signal detector from the Android app **Frontera 2.22** (`com.koritin.frontera`, NPO Apogei) on a PC. It accepts **UHD-compatible SDRs**, defaulting to the measured LibreSDR B210 mini configuration, and can expose its sweeps through an Arinst serial adapter.

## What the model is

The models are extracted from the APK and stored in the repo-root `models/`, unchanged. All of them are LiteRT/TFLite fp32 files with the tag `RY_0_1_6_s_soup8b`; embedded tensor names identify a PyTorch / LiteRT Torch export path. The original training framework, dataset size and training recipe are not recorded. See the [model audit](../docs/model-training-audit-2026-10-26.md).

| File | Role |
| --- | --- |
| `*_rowsums_w{8,10,12,16}_fp32.tflite` | Encoder. Takes W sweeps × 600 bins × 3 channels and outputs per-bin mean, variance, lag-1 and max statistics. 10k params, with identical weights across all W variants. |
| `*_detect_fp32.tflite` | 1-D FPN detector (256k params). For each of 263 positions it outputs objectness, 9 class scores and 2 edge distances. |
| `*_golden.bin` | The app's self-test: 16 real sweeps plus the expected outputs. |

The 9 classes are `analog, dji, autel, siyi, wifi, lte, other_signals, gnss, wfb_ng`. The app alerts on `analog, dji, autel, siyi, wfb_ng`, plus `wifi` if that option is turned on.

Properties worth knowing:

- **No absolute frequency input.** Classes come from spectral shape and from how a signal changes from sweep to sweep. A `gnss` or `analog` box can therefore appear at any frequency, and the app applies no per-band rules either.
- **Input levels are relative to the noise floor**, so absolute calibration is irrelevant. SNR does matter.
- **The scale is fixed** at 3 MHz per bin and 600 bins, i.e. 1800 MHz windows. Feeding finer bins makes every signal look wider than anything the model was trained on.
- **Different classes rely on different statistics.**
  - LTE, WiFi, DJI and analog mostly depend on the time-averaged level.
  - `wfb_ng` collapses without sweep-to-sweep variance.
  - `siyi` depends on max-hold.
  - DJI needs both level and variance.

`bandera/model.py` is a line-by-line port of the app's Kotlin pre- and post-processing (`RegionEncoding4`, `RegionBAccumulator`, `RegionBDecoder`). It reproduces the golden outputs to within about 1e-4; the app's own tolerance is 5e-3.

## How the B210 imitates the Arinst

The Arinst SSA measures one point at a time. The current emulation targets a roughly 200 kHz RBW, an estimate rather than a verified instrument setting. In `scn20`, **200 is the synthesizer timeout, not the RBW**, and 10700000 is the IF. The app asks for 3 MHz steps with `samples=20`; measured noise jitter of about 1.1 dB is consistent with averaging about 20 log-detector readings per point. This does not establish the instrument's averaging law or dwell time.

Because every point is taken at a different instant, a bursty TDD video link appears as a **dotted** block. The model relies on exactly that texture, so a wideband FFT snapshot is not an adequate substitute.

`bandera/b210.py` reproduces it as follows:

- **Grid:** 600 points at 3 MHz, covering 1800 MHz per window.
- **Radio setup:** master clock 56 MHz, 28 MS/s output, 56 MHz analog bandwidth.
- **Per LO tune:** two DDC offsets of ±12 MHz cover 16 grid points (48 MHz). The LO sits 1.5 MHz from the nearest point, so the DC spike never lands on a point.
- **Per point:**
  - Own time slot inside the burst, so points are sequential as on a swept analyzer.
  - Hann FFT of 224 samples, giving bins of about 180 kHz at 3 dB (close to the Arinst's 200 kHz).
  - 24 readings averaged in dB.
  - Rounded to 0.1 dB, like the Arinst output.
- **Calibration-aware tune order:** UHD re-runs the AD9361 RF DC calibration, which costs about 120 ms, whenever the LO moves more than 100 MHz from the last calibration point. LO tunes are therefore grouped in blocks of 144 MHz or less, and each block is entered at its centre. DDC-only retunes take about 0.2 ms and never trigger the calibration.
- **Result:** about 1.3 s per 1800 MHz window, measured over USB 3.
- **Flatness correction:** `flatness.json`, written by `calibrate`, removes the roughly 1 dB passband ripple that repeats every 16 points.
- **Mirror (IQ image) suppression:** the AD9361 leaks a copy of every signal at 2·LO − f, which falls on the point's partner in the other DDC of the same tune.
  - A point is re-measured when it sits at least 6 dB below a partner that is at least 20 dB above the floor. The partner's strength is taken as the max over the partner and its neighbours, because the partner was measured at a different instant.
  - The re-measurement uses an LO moved 24 MHz, which shifts the point's mirror 48 MHz away. The fresh reading replaces the old one. If the new position also has a strong mirror, the lower of the two readings is kept.
  - Re-measuring happens right after the point's own tune, using the previous sweep's floor, so the LO stays inside the calibration window. The first sweep of a window uses a single pass at the end instead.
  - Measured with an analog VTX at 5844 MHz, 43 dB above the floor: the mirror at 5865 MHz fell from +5 dB (worst +15 dB) to +0.8 dB (worst +1.4 dB), which is noise level.
  - Cost per sweep: about +30 ms on quiet bands and about +170 ms on the busy 1.6–3.4 GHz cellular window (+8%).
  - `--no-image-check` turns it off for A/B comparisons.

Measured on this unit (serial XWR028Z, gain 60, RX2):

- No overload in a city environment.
- Quiet-bin jitter of 1.1–1.2 dB, against 1.13 dB in the Arinst golden capture.
- A live 1600–3400 MHz window gives `lte` on B3 (1806–1884 MHz, score 0.87–0.92), `lte` on B1 (2108–2181), `wifi` on 2.4 GHz and `lte` on B7. The golden capture recorded with the Arinst shows the same structure.

## Setup

The UHD 4.9 Python bindings come from the system Python. The venv adds only LiteRT.

```powershell
python -m venv --system-site-packages .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip uninstall -y numpy   # keep the system numpy 1.26 that pyuhd was built against
.venv\Scripts\python -m unittest discover -s tests -v
```

## Other UHD devices

`--profile auto` keeps the optimized B210 implementation when UHD identifies a B200/B210 and no custom rate or bandwidth was requested. The default `--args type=b200` still selects the LibreSDR. Device arguments are passed to UHD; use `--args ""` for UHD's default discovery or select a different device explicitly. `--device` is an alias for `--args`.

```powershell
# Keep the existing LibreSDR configuration and paired-DDC sweep plan.
.venv\Scripts\python -m bandera run --band 5650-5950 --w 8 --auto-gain

# Example for another UHD device; select a band and antenna supported by its frontend.
.venv\Scripts\python -m bandera run --args "addr=192.168.10.2" --profile generic --rate 10e6 --band 2300-2500 --gain 20 --antenna RX2 --w 8

# Exercise the portable implementation on the connected B210.
.venv\Scripts\python -m bandera sweep-test --profile generic --rate 10e6 --start 4200 --sweeps 2
```

The generic backend queries the selected channel's frequency ranges, gain range, antennas, actual sample rate and bandwidth. It uses separate time slots for each point and computes power at the exact requested frequency, including when UHD coerces a sample rate. The Hann window scales with the actual rate to retain approximately 180 kHz 3 dB bandwidth. Its tuning groups stay within the inner 80% of the sampled/analog passband and avoid measuring at DC. It polls `lo_locked` where available and discards settling samples after each tune. This follows UHD's [tuning and settling guidance](https://files.ettus.com/manual/page_general.html).

Useful options:

| Option | Meaning |
| --- | --- |
| `--channel 1` | Select RX channel 1 for both tuning and the sample stream. |
| `--subdev "A:0"` | Apply a UHD RX subdevice specification before selecting the channel. |
| `--antenna NAME` | Select an advertised antenna. Default: RX2 if available, otherwise the device's default. |
| `--gain DB` | Explicit gain, checked against the device. Default: 60 dB limited to the advertised range. |
| `--rate 10e6` | Generic sample rate, in samples/s; the actual UHD rate determines extraction geometry. |
| `--bandwidth 10e6` | Request analog bandwidth in Hz. If a fixed-filter frontend cannot report bandwidth, this is the user's known bandwidth limit; device metadata marks it as unreported. |
| `--master-clock-rate HZ` | Explicit UHD master clock. A non-56 MHz clock selects the generic profile. |
| `--settle-us 1000` | Discard this many microseconds of samples after each tune; generic default 1000, B210 default 100. |
| `--level-offset DB` | Add a measured calibration offset. Default readings are relative receiver levels, not calibrated dBm. |

Requested bands outside the frontend's ranges are rejected. A frontend with less than 1800 MHz coverage still produces the fixed 600-point model input: points outside its physical range are explicitly floored at -120 dB, with measured/floored counts in sweep metadata. Such partial coverage changes the model's input statistics, so classification fidelity must be measured on that device. The B210 profile retains its 70–6000 MHz bounds and requires its fixed clock/rate/bandwidth.

The generic profile does not apply B210-specific slot calibration, AD9361 tune ordering or IQ-image remeasurement. `calibrate` is therefore restricted to the B210 profile. USB/network throughput, tuning latency and frontend response differ by device; this is API portability, not a claim that every radio has been tested or meets the Android app's two-second deadline. On the connected LibreSDR, two generic 10 MS/s 600-point sweeps took 6.54 and 6.53 seconds. The B210 profile remains the appropriate default for this unit.

Incomplete/corrupt UHD bursts are rejected before power extraction. The live scanner clears its consecutive-sweep buffer on overload above 0.1%, invalid samples or reported bad bursts, and waits for W clean sweeps before inference resumes. Recordings retain rejected rows and their quality metadata for inspection. Web replay, CLI replay and plots preserve these quality checks; no model window containing a rejected row is classified. Older sidecars lacking quality fields are treated as clean, apart from nonfinite samples.

## Arinst serial adapter

```powershell
.venv\Scripts\python -m bandera serial --port COM12 --gain 60
```

The server supports `test`, Frontera's 100–200 MHz discovery probe, and `scn20` requests on the model's 3 MHz grid. It emits the leading CRLF, echoed command/start/index, big-endian level words, `FF FF`, elapsed milliseconds and `complete` trailer. Binary samples are length-delimited; newline bytes inside samples are data. Legacy control fields are parsed but do not change the configured SDR dwell, gain or RBW. This is a receive sweep adapter, not a complete emulation of every Arinst command or analog control.

`--port` is a serial endpoint, for example one side of a virtual COM pair; an ordinary B210 USB connection does not become a USB CDC analyzer. Connecting a phone requires a suitable serial bridge/USB device and the Bandera app's supported USB identification. No physical PC-to-phone serial bridge was connected during validation. `--max-requests N` bounds a test session. A stock discovery request completed in 399 ms on the LibreSDR after opening the device; generic full sweeps can exceed the app's timeout.

## Live web page

```powershell
.venv\Scripts\python -m bandera web --band 2300-2550
```

Then open http://127.0.0.1:8765/. The page shows:

- An alert banner that is green when clear, amber for a possible drone, and red for a confirmed drone.
- A spectrum in dB above the noise floor, with the model's latest boxes drawn over it.
- A waterfall with detection marks.
- A drone-track table, an event log, and a status chip per window with gain, fill, sweep time, floor and overload.

The controls are:

- **Band presets:** 2.4 GHz, 5.8 GHz, 5.1–5.9, 1.2–1.3, 900 MHz and full band, plus a custom range.
- **Scan settings:** W, gain, auto-gain, WiFi alerts and whole window. Changing any of these restarts the sweep buffers.
- **Pause/Start** and **Clear**.
- **Record** (with a tag), which writes to `captures/web/`.
- **Sound.** Click once to enable it, since browsers block audio until you interact with the page. It then beeps 400 ms every second while a drone is confirmed, like the app, and vibrates on phones.

Things to know when using it:

- **Detections only count inside the selected band.** The model always looks at a full 1800 MHz window. By default only boxes centred inside the selected band are tracked and alerted on. Tick **whole window** for the app's behaviour, which tracks the entire window.
  - Boxes outside the band are drawn dashed when **show full window** is on.
  - The spectrum header says when detections are outside the current view.
  - Example: while watching 5.8 GHz, the model briefly called the 5.17–5.25 GHz WiFi channels `siyi` (best score 0.84).
- **Opening it from a phone:** start with `--host 0.0.0.0` and browse to the LAN address the server prints at startup.
  - Windows Firewall may ask to allow Python.
  - There is no authentication, so anyone on that network can change the scan settings.
  - Browser notifications only work on `localhost`. Beep and vibration work over the LAN.
- **Replaying a recording through the same page:** `web --replay FILE.npy [--speed 4]`. The band is fixed to the recording's window.

## Commands

Run all commands from this directory with `.venv\Scripts\python -m bandera ...`.

| Command | Purpose |
| --- | --- |
| `web [--band 2300-2550] [--host 0.0.0.0]` | Live web page, described above. |
| `selftest` | Golden self-test of the model port (no radio needed). |
| `sweep-test --start 1600` | Timing, overload, noise jitter and per-slot ripple. |
| `calibrate [--band 70-6000]` | Measures the passband ripple and writes `flatness.json`. Run once per gain setting. |
| `run --band 2300-2550 [--w 16] [--record DIR --tag NAME]` | Live detection with an app-style tracker. Writes a JSONL log to `captures/`. |
| `replay FILE.npy [--w 8]` | Runs the detector and tracker over a recording and prints a per-class summary. |
| `plot FILE.npy [--w 8]` | Writes a waterfall PNG with the detected boxes. |

**Bands.** A `--band` of 1800 MHz or less becomes one window centred on it, clamped to 70–6000 MHz. A wider band is split into app-style windows with a 1660 MHz stride. The full band 70–6000 gives windows starting at 70, 1730, 3390 and 4200 MHz. Each window owns the half of its overlap that is nearer to it, so a box is never reported twice.

**Timing.** A window needs W sweeps before its first detection, which is about 21 s at W=16 or 10 s at W=8 for a single window. After that every sweep produces an update.

**Gain.** Keep overload below 1e-3; the `run` command warns above that.

- `--auto-gain` picks one fixed gain per window at startup. It starts at `--gain` and drops 6 dB at a time until two test sweeps stay clean. Here the 70–1867 MHz window settled at 48 dB, likely because of strong signals in that range, while the other windows stayed at 60.
- Gain never changes within a window, because rows recorded at different gains would look like signal variance to the model.
- Bursty nearby emitters can still overload occasionally, since two test sweeps can miss them. If the warning keeps appearing, lower `--gain`.
- A drone or controller within a few metres will saturate gain 60. Use about 40 for close-range tests.

**Recordings.** Each window produces `<tag>_<start>-<stop>mhz_<utc>.npy`, a float32 array of shape (n_sweeps, 600), plus a `.json` sidecar. The sidecar contains the sweep times, durations, overload, device settings and an empty `boxes` list ready for labels. The layout follows the app's `dataset_regions` export, so recordings can be labelled the same way.

## Tracker

`bandera/tracker.py` ports the main confirmation rules of the app:

- Only target classes are tracked.
- A box matches a track if it has the same class and an IoU of at least 0.3, or a centre within ±5 MHz.
- A track gets at most one hit per W visits of its window, and 2 hits confirm it.
- A track is confirmed instantly if its score is at least 0.5. At W=8, dji, autel and wfb_ng need 0.65 instead. siyi is never confirmed instantly.
- A track is dropped after 2W visits without a match.

Before tracking, same-class boxes with an IoU above 0.3 are merged into one (the app's `mergeBoxes`). A second box overlapping a track that was already hit during the same visit is ignored rather than opening a new track.

Not ported:

- The app's retraction rules for provisional tracks (duty, churn, gap).
- The seam suppression between windows. Ownership filtering is used instead.

## Suggested drone sessions

1. **Baseline.** With nothing flying, run `run --band <band> --record captures/<date> --tag ambient` for 3–5 minutes per band you plan to use.
2. **Per drone and per link band**, recording about 2 minutes each with a descriptive tag:
   - drone on the ground at about 5 m, around 50 m, and in flight further out;
   - a tag such as `--tag dji_mini4_2g4_50m`.
3. **Useful single windows:**
   - `--band 2300-2550` gives 1525–3322 MHz.
   - `--band 5650-5950` gives 4200–5997 MHz, covering 5.1 and 5.8 GHz.
   - `--band 800-1000` gives 0–1800 MHz, clamped to start at 70.
4. **Compare W.** `replay` each recording with `--w 8` and `--w 16`, `plot` it, and note which class each link actually gets. Autel could not be triggered with synthetic signals, so real recordings are the only way to find out what the model expects for it.

## Known gaps

- **Arinst measurement behaviour is only partly established.** The protocol defines `samples` as an ADC sample count, but the detector's exact log/linear averaging, filter shape, per-point dwell and reference sweep cadence remain unmeasured. A same-signal comparison with an actual Arinst is still needed. The B210 profile typically takes about 1.3–1.6 s per window on this host, with occasional longer scheduling/retuning delays.
- **Cross-device SNR is not calibrated.** Gain, noise figure, antenna and measurement bandwidth can change the spectrum relative to the training data. There is no controlled side-by-side measurement establishing a universal B210-versus-Arinst noise-figure advantage, and no deliberate SNR-degradation option.
- **Mirror suppression does not cover the band edges.** Points whose re-measure LO would fall outside 70–6000 MHz are left as measured: A-side points of the first tune of a window starting near 70 MHz, and B-side points of the last tune near 6 GHz. Spurs other than the IQ image, such as LO harmonics or ADC aliases, are not handled.
- **The B210 profile stops at 6 GHz.** The generic profile uses the connected frontend's advertised frequency ranges. The app's Horizont-12 downconverter arrangement is not implemented here.
