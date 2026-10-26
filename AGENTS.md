# AGENTS.md

Operational notes for anyone (human or agent) working in this repo. Read `README.md` first for
the *what* and *why* (NPO Apogei, the Frontera app, the ML models). This file is the *how*: tooling,
commands, hard-won gotchas, and the model's input contract. Keep it current when the build breaks in
a new way.

Platform: Windows 11. Two shells are used and they are **not** interchangeable (see Gotchas):
- **Git Bash** — Python, apktool, git, ad-hoc serial scripts.
- **PowerShell** — all PlatformIO / ESP-IDF work (`idf_tools.py` refuses to run under Git Bash).

---

## Repo map

| Path | What | Build/run with |
|---|---|---|
| `models/` | The extracted TFLite detector (`RY_0_1_6_s_soup8b_*`) + `*_golden.bin` self-test vector. **Do not move or rename** — `sdr/bandera/model.py` resolves `MODELS_DIR = <repo-root>/models`. | — |
| `sdr/` | Python package `bandera`: UHD receive backends, Arinst serial adapter, model, recording/replay, live web UI. The default preserves the LibreSDR **B210** configuration. | `sdr/.venv/Scripts/python -m bandera ...` |
| `esp32-sdr-arinst/` | Firmware making a **Seeed XIAO ESP32-C5** speak the Arinst `scn20` protocol over USB serial, using the chip's Wi-Fi-radio SDR mode. The $7 backend. | `pio run -e esp32c5` (PowerShell) |
| `patcher/` | Reproducible APK patcher (stock Frontera → **Bandera**, + accept the C5 as an analyzer). `patch.py` + `overlay/`. | `python patcher/patch.py ...` |
| `.github/workflows/patch-apk.yml` | On-demand (`workflow_dispatch`) CI that downloads the stock APK, runs the patcher, publishes a release. | GitHub Actions |
| `modded_app/` | **Local-only** scratch: fresh apktool decode, tool downloads, build outputs. Git-ignored at root. Not needed to build anything; `patcher/` is the source of truth. |
| `tools/`, `build/` | Downloaded jars / transient build dirs. Git-ignored. |

Remote: `origin` = `github.com/KonradIT/bandera-sa`. The CI release tag is `bandera-<short-sha>`.

---

## The model input contract (read before touching any backend)

Every backend exists to feed the detector the exact domain it was trained on. Get this wrong and it
mislabels or floors everything.

- **Grid:** 600 bins × **3 MHz** = an **1800 MHz window**, ~200 kHz RBW per point. The app's request is
  `scn20 4200000000 5997000000 3000000 ...` (600 points, 4200–6000 MHz).
- **W consecutive sweeps** per window (W ∈ {8, 10, 12, 16}); the encoder summarizes them (mean / var /
  max / burstiness). `RegionModel.pick_w()` chooses W from how many sweeps you have.
- **No absolute-frequency input.** Classes come from *spectral shape* + *sweep-to-sweep texture*, so a
  class can fire at any frequency and the window start is just an axis label.
- **Floor-relative.** Absolute dBm calibration barely matters; SNR and shape do.
- 9 classes: `analog, dji, autel, siyi, wifi, lte, other_signals, gnss, wfb_ng`. Drone classes raise an
  alert; `wifi/lte/gnss` are "known, not a drone".

The Python port is verified against `models/RY_0_1_6_s_soup8b_golden.bin` to ~1e-4
(`python -m bandera selftest`). If you change encode/features/decode, that must still pass.

### Arinst `scn20` wire format (what both backends emit)
```
request : scn20 {startHz} {stopHz} {stepHz} 200 {samples} 10700000 {att} {idx}\r\n
reply   : \r\nscn20 {startHz} {idx}\r\n  +  N big-endian u16 (MSB first)  +  FF FF  +  {elapsedMs}\r\ncomplete\r\n
level   : dBm = (800 - (word & 0x7FF)) / 10
```
Checked against the app parser and Arinst's official protocol revision 4 (2022-12-21).
The `200` request field is synthesizer settling timeout, **not RBW**; the SSA's fixed 200 kHz
IF bandwidth is specified separately. `att=10000` means 0 dB; generally `att=10000+100*attenuation_dB`.
The APK's discovery request uses **11 points at 10 MHz**, 100–200 MHz, `samples=1`, and a 1000 ms
timeout. Normal SCN20 requests have a **2000 ms** timeout. Preserve both paths.

This is a Frontera-compatible subset, **not full Arinst emulation**: hardware settings/detector
statistics differ and the upper five sample-counter bits remain zero. pyarinst has decoder bugs;
do not use its output as an unquestioned reference. See `docs/protocol-research.md`.

---

## SDR detector (`sdr/`)

- Setup: `python -m venv sdr/.venv`, then `sdr/.venv/Scripts/pip install -r sdr/requirements.txt`.
  Needs the UHD Python bindings (UHD 4.9 is installed at `C:\Program Files\UHD`).
- Hardware: a **LibreSDR B210 clone** (opens as `type=b200`). On a **USB 3** cable. See
  `[libresdr-sdrpp-usrp-module]` history — the clone's EEPROM name is `LibreSDR_B220mini`, serial
  `XWR028Z`; it runs a patched B210 FPGA image.
- Commands:
  - `python -m bandera selftest` — golden check, no hardware.
  - `python -m bandera web --band 5650-5950 --w 8 --auto-gain` — live page at http://127.0.0.1:8765/.
  - `python -m bandera run --band 5650-5950 --w 8 --auto-gain --rounds N` — console detections (bounded by `--rounds`).
  - `python -m bandera sweep-test`, `run --record ...`, `replay`, `plot` — capture/replay tooling.
  - `python -m bandera serial --port COMx` — serve Arinst discovery/SCN20 on a serial transport.
    This requires an actual serial link or paired virtual ports; the B210 USB interface is not CDC serial.
  - `--args` / `--device` selects any UHD device. `--profile auto` preserves B210 optimizations;
    `--profile generic` uses discovered frontend capabilities. See `sdr/README.md` for rate, channel,
    subdevice, gain, antenna, range limits, and timing limitations.
- Status: the B210 path **works** — e.g. `analog 5800-5814 MHz score 0.52-0.69`, stable, ~1.35 s/window.

### B210 gotchas (all handled in `bandera/b210.py`, don't "simplify" away)
- Bursts at 56 MS/s overflow the USB link. The sweeper runs master clock 56 MHz, **28 MS/s** output, two
  **±12 MHz DDC offsets** per LO.
- UHD re-runs the AD9361 RF DC calibration (~120 ms) on any LO move **>100 MHz** from the last cal point.
  Disabling IQ/DC tracking does **not** stop it, so LO tunes are grouped into calibration blocks.
- IQ image sits at **2·LO − f** (median ~35 dB down, sometimes only ~11 dB). Suppressed by re-measuring
  suspect points with the LO moved 24 MHz (see `sdr/README.md`).
- Stream channels must be set in `StreamArgs.channels`, not only tuning/gain calls.
- The generic path is portable but slower (6.5 s at 10 MS/s on this B210); it is not guaranteed to
  meet Frontera's serial timeout. Host tests cover additional capability combinations, but only
  the connected LibreSDR has received live RF validation.

---

## ESP32-C5 firmware (`esp32-sdr-arinst/`)

Forked from CapnBry/widscan58 (on ESPARGOS/esp-sdr). New code is in `firmware/src/arinst/`:
- `arinst_sweep.c` — **pure logic** (parse, hop plan, fill, packet build). Host-testable, no ESP-IDF.
- `arinst.c` — radio glue (`radio_tune` / `radio_capture` / `psd_compute` per hop).
- Radio core under `firmware/src/radio/c5/`, DSP under `firmware/src/dsp/`.

### Build / flash (PowerShell only)
```powershell
& "$HOME\.platformio\penv\Scripts\pio.exe" run -e esp32c5 -d <path>\esp32-sdr-arinst\firmware
& "$HOME\.platformio\penv\Scripts\pio.exe" run -e esp32c5 -t upload --upload-port COM5 -d <...>\firmware
```
- Toolchain: **pioarduino 55.03.312-1** (ESP-IDF 5.5.5, PIO Core 6.2.x). See `[platformio-setup]`.
- The C5 enumerates on **COM5**, VID/PID **303a:1001** (Espressif USB-Serial/JTAG). `before_reset = usb-reset`
  resets into download mode in hardware — no BOOT button needed.
- A **full** rebuild (anything that touches `sdkconfig*`) can trip the known esptool penv breakage
  (`tool-esptoolpy does not appear to be a Python project`). It is usually transient — re-run the build;
  if it persists, `pip install --force-reinstall --no-deps esptool==5.4.0` into the pioarduino penv.
- If that environment has no pip, use PlatformIO's bundled `uv pip install --python <penv-python>
  --reinstall --no-deps esptool==5.4.0`. The component must explicitly require `esp_ringbuf`; relying
  on a transitive include can break a clean ESP-IDF build.

### Host test (fast, no hardware — run this after editing sweep logic)
```bash
cd esp32-sdr-arinst/firmware/test
gcc -I../src/arinst -O2 -o /tmp/t test_arinst_sweep.c ../src/arinst/arinst_sweep.c -lm && /tmp/t
# expect: ALL PASSED
```

### C5 hard constraints & gotchas
- **PLL locks only near ~2.4 GHz and ~5.15–5.95 GHz.** `scn20` points outside those return floor
  (fine — that's where drones live). `ARINST_C5_BANDS` encodes this.
- **`radio_tune` rejects non-whole-MHz LOs** (`lo_khz % 1000 != 0`). The planner rounds every LO to a
  whole MHz; keep it that way or hops silently tune to a stale LO.
- **USB-Serial/JTAG only** — the VID/PID is fixed at `303a:1001` and *cannot* be changed (no USB-OTG, no
  TinyUSB). So the **stock** app can't use the C5 (it looks for an Arinst `0483:5740`); only the
  **patched** app, whose device filter + manufacturer shim accept `303a:1001`.
- **PSD order and orientation:** `psd_compute(..., conjugate=true)` already corrects the hardware
  IQ orientation and returns **FFT-shifted** power/valid arrays (DC at `N/2`). Read
  `N/2 + round((f-LO)/(rate/N))`. The previous natural-order assumption and extra mirror were wrong;
  raw IQ captures at several LOs and the known 5805 MHz camera established this on 2026-10-26.
  Keep host fixtures consistent with the real PSD contract, not with the fill implementation.
- **±7 MHz DC dead zone** (DC offset / LO leakage, much wider than the 3-bin DC guard). The planner is a
  single-sideband design that serves every point from `|offset| ∈ [dead, edge-1]` and never near the LO.
- **Sampling:** 80 MS/s with the inner ±16 MHz retained avoids using the Nyquist-edge aliases of
  the old 40 MS/s path. The old full-floor failure was a bin-order bug, not evidence against 80 MS/s.
- **Scan speed:** full per-LO DC calibration (~45 ms) thrashed its 16-entry cache. Arinst now calibrates
  once per band while retaining per-frequency RX gain tables. Do not replace this with pinned gain.
- **Ownership/memory:** Arinst requests run on the scanner task that owns RF/DSP and use the shared
  serial output queue. Check all buffer allocations; a partially allocated request must not be
  considered initialized on the next call.
- `managed_components/` and `dependencies.lock.*` are fetched from `src/idf_component.yml` on build;
  they are build artifacts, not sources.

---

## APK patcher (`patcher/`)

Turns the stock Frontera APK into Bandera and makes it accept the C5. **Smali-only** — no javac/D8/dex.

```bash
python patcher/patch.py --apk <stock.apk> --out bandera.apk \
  --version <sha> --version-code <n> --tools-dir tools
# overlay / framework / work default to patcher/ and the repo root
```
- Tools (auto-downloaded by CI into `tools/`, or place manually): **apktool 3.0.3**,
  **uber-apk-signer 1.3.0**. Needs **JDK 17**.
- Steps: `apktool d` → verify Frontera 2.22/package/required call sites → apply `overlay/` (icons, strings, the `Bridge.smali` shim) → edit strings per
  locale → edit `device_filter` (adds `303a:1001`) → rewrite `getManufacturerName` call sites → stamp
  version → `apktool b` → sign → verify launcher/package/label and unchanged model/golden assets.
- ConnectionService's attach/permission VID/PID checks also need the bridge; changing only the
  manufacturer string misses background discovery. Keep actual USB driver IDs unchanged.
- Builds are **debug-signed** (sideload only).
- CI (`workflow_dispatch`): set the repo variable `FRONTERA_APK_URL` to the stock APK (a GitHub release
  asset works — the token is sent only to exact HTTPS `github.com` / `api.github.com` hosts on port 443). `versionName` = short `main` SHA,
  `versionCode` = run number. Publishes/overwrites release `bandera-<short-sha>`.

### APK gotchas
- apktool **3.0.3** with a **fresh** `-p <framework dir>` is required for SDK 35/36 APKs; the stock
  2.5.0 cache throws on `dataExtractionRules`.
- `apktool.yml` wants **bare** `versionCode:` (no quotes) — a quoted int throws `NumberFormatException`.
- The `Bridge.smali` manufacturer shim keeps its own fallback as `invoke-virtual`; the patcher must
  **not** rewrite call sites inside `ua/bandera/bridge/` (else infinite recursion). `patch_smali` skips
  that path.
- `zip` is absent on this box's Git Bash — if you ever need to inject into the APK, use Python `zipfile`.

---

## Hardware & ports cheat-sheet

| Device | How it shows up | Notes |
|---|---|---|
| LibreSDR B210 clone | UHD `type=b200`, USB 3 | UHD 4.9 at `C:\Program Files\UHD` |
| XIAO ESP32-C5 | `COM5`, USB `303a:1001` | self-powered; fine on phone OTG |
| Phone (Pixel 10, Android 17) | `adb` | wireless adb fails across subnets (10.94.x vs 192.168.0.x); mDNS `_adb-tls-connect` worked |

---

## Conventions
- The caveman/terse reply style used in chat is for the terminal only — **code, comments, commit
  messages, and docs like this are normal prose.**
- `modded_app/` is disposable; never treat its contents as canonical. `patcher/overlay/` is canonical.
- Ad-hoc hardware probe scripts (C5 serial readers, B210 captures) live in the session scratchpad, not
  the repo. Re-derive from the `scn20` format above if you need one.
