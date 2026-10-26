# Frontera and Arinst interoperability audit

Audit date: 2026-10-26. Source APK: **Frontera 2.22**, package `com.koritin.frontera`,
versionCode 22, SHA-256
`49163518b5ed665821470a39662f8fd1d93c7dae1572b410a322bc6e1ea1b9ab`.
The conclusions below separate wire compatibility, app behavior, and RF measurement fidelity.
No model changes are required for this work.

## The actual Arinst contract

The manufacturer's [download page](https://www.arinst.net/download-apk.php) links
[Protocol.zip](https://www.arinst.net/files/Protocol.zip), containing **Arinst protocol,
revision 4, 21 December 2022**, and `example.m`. This is the primary protocol reference.

```text
scn20 START STOP STEP TIMEOUT SAMPLES IF ATTENUATION INDEX\r\n

\r\nscn20 START INDEX\r\n
<2 bytes per point><FF FF><elapsed milliseconds>\r\ncomplete\r\n
```

Only the displayed line breaks represented by `\r\n` are transmitted; there is no newline
between the sample payload, terminator and elapsed digits. The leading blank line is required
by the documented frame. `STOP` is inclusive.

| Field | Meaning |
|---|---|
| `START`, `STOP`, `STEP` | Frequencies in Hz |
| `TIMEOUT` | Synthesizer settling parameter; recommended `200`; units unspecified |
| `SAMPLES` | ADC samples per frequency; recommended `20`, affects acquisition time |
| `IF` | Hardware intermediate frequency; `10700000` Hz |
| `ATTENUATION` | `10000 + 100*a`, with `a` between -30 and 0 dB |
| `INDEX` | Request identifier echoed in the header |

Each measurement is a big-endian word. Its lower 11 bits encode `(800 - 10*dBm)`;
the upper five bits carry a sample index. For nonzero attenuation the client adds back its
magnitude. The initial sample-index phase is unspecified. `scn22` uses the same exchange
with a tracking-generator command name. [Manufacturer protocol](https://www.arinst.net/files/Protocol.zip).

The `200` request field is **not an RBW selector**. Separately, the
[SSA-TG R2 manual, specifications](https://arinst.net/files/Manual-Spectrum_Analyzer_Arinst_SSA-TG_R2-ENG.pdf)
specifies a fixed **200 kHz IF bandwidth**. The newer
[SSA-TG R2s manual](https://www.arinst.net/files/Spectrum-Analyzer-Arinst-SSA-TG-R2s-ENG.pdf)
also specifies 200 kHz and a different sweep speed. A command-compatible implementation
does not necessarily reproduce either receiver's filter, settling behavior, detector averaging
or calibration. The protocol alone does not establish the exact hardware used for model training.

## What the supplied app actually does

These findings come from decoding the supplied APK, with smali used to verify decompiler output.
The class names are specific to version 2.22 and should be rechecked after an APK update.

| APK location | Verified behavior |
|---|---|
| `j9/e` (`FronteraClient.sendScan`) | Sends `scn20 start stop step 200 samples 10700000 10000 idx\r\n` |
| `c8/c` | SCAN20 and SCAN30 timeout is 2000 ms; TEST, SCF, GON and GOFF are 200 ms |
| `i9/j.d` | Discovery sends `scn20 100000000 200000000 10000000 200 1 10700000 10000 0\r\n` |
| `i9/j.d` | Up to eight discovery attempts; 1000 ms per attempt and 400 ms between attempts |
| `c8/l.c` | Finds the scan header, CRLF, last `complete`, and last `FF FF`; extracts elapsed digits |
| `c8/l.b` | Accepts `scn22` by converting its tag internally to `scn20`; SCN30 unimplemented |
| `c8/o.a` | Combines consecutive high/low bytes, masks `0x7ff`, computes `(800-value)/10` |
| `c8/l.b` test branch | Expects `test <address> complete <generator-address>` |

The normal 4200–5997 MHz request has **600 points at 3 MHz spacing**. This is a set of
narrowband power measurements on a sparse grid, not a request to integrate each entire 3 MHz
cell. The model's window remains 600 points, with 8/10/12/16 consecutive sweeps.

The app tolerates the old missing leading CRLF because it searches for the command name.
It ignores the upper sample-index bits. It rejects a returned start frequency that differs
from the requested start by more than one requested step. A partial trace is not a substitute
for the requested complete frame, even if an app parser happens to accept it.

Discovery is a different grid from the model grid: a backend restricted to 3 MHz measurements
still needs to answer that 11-point probe promptly. The C5 can legitimately return its floor
for these unsupported RF frequencies. A general adapter should make any unsupported-range
policy explicit.

The old Bandera patch changed the manufacturer string but left four service VID/PID checks
unchanged: three in `ConnectionService` and one in its USB receiver. That allowed a discrepancy
between manual analyzer discovery and automatic attach/permission/presence handling for C5.
The patcher now maps the exact `303a:1001` pair in those service checks while keeping real
descriptors visible to the USB serial driver. The subsequent
[Pixel 10 test](phone-validation-2026-10-26.md) verified live sweeps, known-camera
detection and USB reconnect after an in-place app update.

## Why pyarinst is useful but not a conformance oracle

The supplied [pyarinst source](https://github.com/IlyaDanilenko/pyarinst/blob/main/pyarinst/pyarinst.py)
confirms the request order and attenuation encoding. Its four-CRLF reader expects the documented
leading blank line; the repository's previous three-CRLF C5 response could block it.

Its decoder currently reads `i+2` for the low byte instead of `i+1`, then subtracts the encoded
attenuation integer rather than the original dB attenuation. It also splits binary data on
spaces and CRLF and waits without a serial timeout. Valid sample bytes can contain either
separator. Consequently, copying its parser or treating its numeric output as calibration
would reproduce bugs. Parse by the known point count and validate the following marker/tail.

The C5 and Python implementations are **Frontera-oriented receive adapters**, not complete
Arinst instruments. They have different RF coverage/calibration and do not implement a
tracking generator. Parsed legacy parameters do not automatically reproduce ADC dwell or
physical attenuation on an SDR. Consult each backend's README for the implemented subset.
Packet compatibility, time-to-completion, and RF feature fidelity need separate tests.

The requested [Telegram announcement](https://t.me/kbrtb_uav/126) was not accessible through
the research tool. No technical claims here depend on its unavailable content.
