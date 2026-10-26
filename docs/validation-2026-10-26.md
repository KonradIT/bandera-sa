# Validation: C5, UHD and Frontera interoperability

Tested on 2026-10-26 using the connected XIAO ESP32-C5 on COM5, LibreSDR B210
`XWR028Z`, and the user's analog FPV camera nominally on A4, 5805 MHz. All RF
tests were receive-only. No DJI was connected. No Android device was
available during the initial bench phase; the subsequent
[Pixel 10 test](phone-validation-2026-10-26.md) passed live display, camera detection,
USB reconnect and stop/start. See the [protocol research](protocol-research.md) for primary
sources and the [TODO](../TODO.md) for remaining validation.

## Results on real RF

Both receivers acquired 24 consecutive sweeps on the unchanged **600-point,
4200–5997 MHz, 3 MHz-step** model grid. The unchanged extracted model was run
over every available overlapping window. A camera match means a detection
center between 5790 and 5820 MHz.

| Receiver | Sweep duration | W=8 analog windows | W=16 analog windows | W=16 analog scores |
|---|---|---|---|---|
| C5, corrected 80 MS/s firmware | 0.406–0.438 s host wall time | 17/17 | 9/9 | 0.787–0.834 |
| LibreSDR, default optimized B210 profile | 1.272–1.911 s acquisition | 13/17 | 9/9 | 0.582–0.766 |

For C5, W=10 and W=12 also detected analog in every available window (15/15 and
13/13). For B210 these were 14/15 and 13/13. One early B210 W=8 window called
the camera `other_signals`; the remaining misses produced no camera-class box.
These are small, overlapping bench samples, not independent accuracy estimates
or evidence of DJI performance.

The C5 camera peak now lands on 5805/5808 MHz. The previously reported 5769 MHz
ghost is no longer a distinct median peak. A residual board spur near 5760 MHz
remains. C5 and B210 levels have different uncalibrated offsets, gain and antenna
responses; their plotted SNR difference is not a receiver sensitivity measurement.

![Median and variability of the two real captures](../build/validation/analog-comparison.png)

The ignored local [validation directory](../build/validation/) contains the plot,
both `.npy` captures, their metadata and `summary.json`. These generated artifacts
are available in this workspace and are not committed repository inputs.

| Capture | SHA-256 |
|---|---|
| `c5_analog_24.npy` | `b75b86f655b13cb6093bf96297a664c1a5f6ac866911bd7c2addbc25a22dafc7` |
| `b210_analog_24.npy` | `c37ed474bab5d4d16de5b0cf29a16cdae4b151704ffe6db9e88bba357d22882d` |

## C5 fixes and checks

The initial device crashed on a scan request. Investigation found multiple
independent faults:

- `psd_compute` produces FFT-shifted arrays, but the fill code treated them as
  natural FFT order. At 80 MS/s this selected masked bins and produced floor.
- The PSD's conjugation already corrects hardware IQ orientation. Applying a
  second spectral mirror moved signals. Raw IQ captures at several LOs verified
  that the correct index is `N/2 + round((f-LO)/(rate/N))`.
- Partial buffer allocation could leave the request falsely initialized.
  Arinst radio/DSP calls also ran independently of the scanner's radio owner.
- Per-LO DC calibration thrashed a 16-entry cache, costing approximately 45 ms
  per hop. One calibration per band retains the per-frequency RX gain tables
  and removes that delay; it is not the old single-gain-table pin.

The fixed path uses 80 MS/s, a 1024-point FFT, four segments, the inner ±16 MHz
passband, and ±7 MHz DC avoidance. Arinst acquisition runs on the scanner task;
the command task waits for completion, and complete packets enter the shared
serial output queue. The model and its preprocessing were not changed.

Hardware checks additionally covered:

- Frontera discovery: 11 floor points at unsupported 100–200 MHz, completing
  without RF work.
- Single-point scans, `scn22` reply tags, and a 2048-point request covering both
  C5 bands.
- Forty alternating 5 GHz/2.4 GHz requests in a 47-request stress sequence with
  no malformed frames or crashes. Typical device times were 377–378 ms and
  52–53 ms respectively.
- Eight ambient 2.4 GHz sweeps with 34 active grid points at 2401–2500 MHz.
  This checks capture and framing, not DJI detection.

Absolute C5 dBm calibration remains a placeholder. Longer thermal/reconnect
tests and real digital-link captures remain necessary.

The final firmware is flashed on COM5. Local binaries are
[`c5-firmware.bin`](../build/validation/c5-firmware.bin) (application at `0x10000`)
and [`c5-firmware.factory.bin`](../build/validation/c5-firmware.factory.bin)
(merged image at `0x0`). [Build metadata](../build/validation/c5-firmware.json)
records their hashes and settings. The final image repeated all 47 serial checks
and a pipelined scan/protocol-switch check; request completion now prevents later
commands from overtaking an outstanding sweep.

## UHD portability and serial serving

`--profile auto` retains the default B210's clock, paired DDCs, calibration-aware
tune ordering and image checks. The generic profile uses actual UHD sample rate,
frontend coverage, bandwidth, gain limits, channel, subdevice and antenna. A
channel-selection bug in `StreamArgs.channels` was fixed.

The generic path was exercised on the same LibreSDR at 10 MS/s: two sweeps took
6.53–6.54 s, with no bad bursts or overload. It is slower than the optimized
profile and exceeds Frontera's two-second deadline. Other physical UHD devices
were not available; capability combinations and exact-frequency extraction have
offline tests, but universal hardware performance is not claimed.

`bandera serial --port COMx` now serves Arinst discovery and the model grid.
The real LibreSDR answered the app's special 11-point discovery in **399 ms**,
within its one-second budget. Full packets use the official leading CRLF,
big-endian levels, marker and completion trailer. Fragmented lines, serial
timeouts and partial writes are tested. A bad UHD burst drops one request and
allows subsequent requests to proceed.

The server is a receive adapter, not a complete Arinst instrument: upper sample
counter bits are zero; analog-control fields do not reconfigure SDR dwell or
attenuation; arbitrary grids and generator commands are unsupported. A physical
PC-to-phone serial bridge was not attached. A B210 USB cable does not itself
provide an Android USB serial endpoint.

The longer B210 capture stayed below two seconds. A preliminary 12-sweep run had
two host timing outliers above that threshold (maximum 2.31 s), so the optimized
profile is not a hard real-time guarantee either.

## Model, transport and packaging validation

All six files under `models/` match the supplied `Frontera_2.22.apk` byte for byte,
including the golden capture. Golden checks passed at W=8, 10, 12 and 16; worst
observed logit error was **6.87e-5**, and distance error **1.37e-4**, within the
existing tolerances. No model assets or model code were changed.

The firmware's C packet builder and the Python packet builder were compared
directly using all 16 golden sweeps and the maximum signed sequence index. Every
packet matched byte for byte. Encoding and decoding the golden levels returned
the original float32 values exactly.

All **41 Python tests and six patcher tests passed**, and the C host suite reported
`ALL PASSED`. The Python suite covers golden inference, protocol framing, discovery, sequential
power extraction, UHD capability/rate/channel behavior, invalid/overloaded sweep
rejection, replay quality, recording and the web UI. The C host suite covers the
real shifted PSD contract at 40/80 MS/s, hop geometry, parsing and packet bounds.
CI URL validation was separately exercised
against nine normal/adversarial URL cases; tokens are attached only to exact
HTTPS GitHub hosts, and workflow inputs are not interpolated as shell source.

The rebuilt [Bandera APK](../build/bandera-research.apk) has version
`research-20261026`, versionCode 23, and SHA-256
`15b17ffd81d6b3230e4b8b996d51b064414b041c8b1c08b77b95c82c883a260d`.
Its v2/v3 signatures, zip alignment, launchable Bandera manifest and six unchanged
model/golden assets passed checks. Four ConnectionService USB presence/permission
checks now recognize the exact C5 VID/PID pair while the USB driver retains real
device IDs. The same patch was subsequently built with versionCode 220003 for
an in-place phone update; see the [phone validation](phone-validation-2026-10-26.md)
for successful runtime, live-display and reconnect checks.

## Next hardware checks

Phone discovery, visible sweeps, disconnect/reconnect and stop/start now pass.
Investigate the additional unverified labels observed outside the known camera
frequency, then collect actual DJI Mavic 2.4 GHz
recordings at fixed gain, with ambient captures for comparison; preserve the
model grid and consecutive-sweep timing.
