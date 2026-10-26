# ESP32-C5 Arinst-compatible receiver

Firmware for the Seeed XIAO ESP32-C5 that answers Frontera's Arinst `scn20`
requests over USB serial. Build the Bandera Android APK with [`../patcher/`](../patcher/)
to accept the C5's fixed USB identity, `303a:1001`. Stock Frontera does not accept it.

The radio and DSP derive from [widscan58](https://github.com/CapnBry/widscan58)
and [ESPARGOS esp-sdr](https://github.com/ESPARGOS/esp-sdr). Arinst command parsing,
grid planning, and reply encoding live in `firmware/src/arinst/`.
GPL-3.0-or-later; upstream files retain their attribution.

## Receive and serial path

1. Frontera requests a grid, normally 600 points spaced 3 MHz apart. Its discovery
   request is only 11 points at 100–200 MHz; these correctly return the C5 floor.
2. The command task queues each sweep to the scanner task and waits for completion.
   The scanner exclusively owns the radio and PSD buffers. Replies share the USB
   output queue, so other command replies cannot split a binary sweep packet.
3. A hop captures 4096 I/Q samples at 80 MS/s and averages four Hann-windowed
   1024-point spectra. Bin spacing is 78.125 kHz and Hann equivalent noise bandwidth
   is approximately 117.2 kHz. This is an SDR measurement approximation, not a
   reproduction of an Arinst's analog IF filter or detector.
4. The planner uses whole-MHz LOs and only offsets 7–15 MHz from the LO, inside the
   retained ±16 MHz passband. A full 4200–5997 MHz request needs about 90 hops.
   The 80 MS/s capture avoids the earlier nearby-carrier alias problem at 40 MS/s.
5. DC/IQ calibration runs once per supported band in each sweep. The RX gain table
   still follows **every LO**; gain is not pinned to one frequency. This avoids
   thrashing the 16-entry per-LO calibration cache on a wide scan.
6. `psd_compute` has already conjugated the radio's inverted raw I/Q and returns
   **FFT-shifted** bins. Read frequency `f` at `N/2 + round((f-LO)/(rate/N))`.
   Do not conjugate or mirror it again. Hardware raw-IQ captures at three LOs
   independently confirmed the 5805 MHz camera's orientation.

The wire exchange is:

```text
request: scn20 START STOP STEP 200 SAMPLES 10700000 ATT INDEX\r\n
reply:   \r\nscn20 START INDEX\r\n
         [N big-endian u16 words] FF FF ELAPSED_MS\r\ncomplete\r\n
level:   (800 - (word & 0x07ff)) / 10 dBm
```

The leading CRLF follows the [official Arinst protocol](https://www.arinst.net/files/Protocol.zip).
`scn22` returns a matching `scn22` header but has no tracking-generator hardware.
`test` returns `test 0 complete 0\r\n`. One-point sweeps and up to 2048 points are
accepted; malformed or oversized requests are rejected without truncating the grid.

This implements the app's receive subset, not the entire Arinst instrument:

- Only 2400–2500 and 5150–5955 MHz are supported. Other points return −110 dBm.
- The upper five sample-index bits are zero; Frontera ignores them. Consumers that
  validate Arinst's rolling sample index need additional support.
- `200` is the requested synthesizer settling delay, **not RBW**. The C5 currently
  uses a fixed 100 µs minimum settle and the capture settings above. Requested ADC
  sample count, IF, and attenuation are parsed but do not configure equivalent
  Arinst hardware. Frontera uses attenuation code `10000` (0 dB).
- There is no tracking generator or calibrated attenuator. Generator commands do
  not enable a transmitter.
- The dBFS-to-dBm offset remains an uncalibrated −30 dB. Absolute levels should not
  be treated as calibrated RF power. Internal board spurs, including 5760 MHz,
  can remain visible.

## Hardware validation (2026-10-26)

On the connected XIAO ESP32-C5, with an analog FPV camera on A4 (nominal 5805 MHz):

- Full 600-point 4200–5997 MHz sweeps completed in **377–378 ms** on the device,
  within Frontera's 2000 ms scan timeout. The camera appeared at 5805/5808 MHz.
- Replaying 24 consecutive sweeps through the unchanged extracted model detected
  analog near the camera in every sliding window: 17/17 at W=8, 15/15 at W=10,
  13/13 at W=12, and 9/9 at W=16. This validates this bench target, not other drones.
- A 600-point 2200–3997 MHz request completed in **52–53 ms**, measuring 34 points
  at 2401–2500 MHz. Ambient bursts were present; DJI validation remains pending.
- A 47-request sequence passed discovery, one-point, `scn22`, 2048-point, and 40
  alternating 2.4/5 GHz requests without a crash or malformed packet.
- A follow-up Pixel 10 / Android 17 test displayed live spectrum/waterfall and
  confirmed analog near 5806 MHz. USB reconnect and stop/start recovered correctly.
  Additional labels elsewhere in the band were not verified against known sources;
  this is an integration check, not a false-positive-rate measurement.

The previous empty-sweep regression had two independent frequency-index errors:
natural-order indexing of FFT-shifted PSD data, and a second spectral reversal.
Partial buffer allocation was also unsafe, and the command task accessed DSP
scratch concurrently with the scanner. Buffers now allocate for the requested
size, clean up partial failures, and remain owned by the scanner.

## Build and flash

Use **PowerShell** with pioarduino 55.03.312-1 / ESP-IDF 5.5.5:

```powershell
& "$HOME\.platformio\penv\Scripts\pio.exe" run -e esp32c5 -d .\firmware
& "$HOME\.platformio\penv\Scripts\pio.exe" run -e esp32c5 -t upload --upload-port COM5 -d .\firmware
```

USB-Serial/JTAG hardware reset enters the downloader; no BOOT button is normally
needed. The board tested reports 8 MB flash, while the project deliberately uses
the existing 4 MB board definition and partition layout.

The local pioarduino installation can repeatedly warn that `tool-esptoolpy` lacks
`pyproject.toml`/`setup.py`; builds and uploads still completed. Installing
`esptool==5.4.0` into its `.espidf-5.5.5` environment provides the working module,
but does not repair the incomplete tool-package directory or eliminate its warning.
Use PowerShell for this environment; `idf_tools.py` rejects Git Bash.

Host regression test, from this directory in Git Bash:

```bash
gcc -std=c11 -Wall -Wextra -Werror -Ifirmware/src/arinst -O2 \
  -o /tmp/bandera-arinst-test firmware/test/test_arinst_sweep.c \
  firmware/src/arinst/arinst_sweep.c -lm
/tmp/bandera-arinst-test
```

Tests cover strict request parsing, complete supported-band coverage, independent
40/80 MS/s shifted-PSD fixtures, single-point scans, official reply framing, exact
buffer bounds, and round trips through a client decoder. Expected result: `ALL PASSED`.

## Phone and antenna

Install an APK produced by `../patcher/`, connect the C5 over USB-C, and grant the
USB permission. Choose the 2.4 or 5.8 GHz band. The C5 cannot change its USB VID/PID,
so stock Frontera still needs the Bandera compatibility patch.

The XIAO's onboard antenna and u.FL path are selected by a board RF switch. Check
the Seeed schematic before changing its GPIO selection; monitor-mode GPIO settings
can persist in NVS.

## Reference limits

[C5VRX](https://github.com/Twotoz/C5VRX) is useful for physical gain-stage behavior,
DC calibration, and the warning that private PHY calls must match the exact PHY
library. Its current receiver uses a different ESP-IDF/PHY revision and a continuous
video-demodulation path. Those register settings are not interchangeable with this
project's pinned snapshot receiver. No C5VRX private-register tuning was copied.

`reference/` holds ignored upstream checkouts; build artifacts and captures are
ignored too. The firmware sources and `../patcher/overlay/` are canonical, not
the disposable `../modded_app/` directory.
