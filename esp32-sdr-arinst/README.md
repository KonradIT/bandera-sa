# esp32-sdr-arinst — an ESP32-C5 that speaks the Arinst protocol

Firmware that turns a **Seeed XIAO ESP32-C5** into a spectrum analyzer which talks the
**Arinst SSA `scn20` serial protocol**, so the Frontera drone-detection app (via the
modded bridge in `../modded_app`) uses it like a Frontera/Arinst analyzer — no tinySA,
no external SDR.

It reuses the hard-won C5 radio work from
[CapnBry/widscan58](https://github.com/CapnBry/widscan58) (built on
[ESPARGOS/esp-sdr](https://github.com/ESPARGOS/esp-sdr)): the undocumented raw-I/Q debug
path, C5 tuning, radix-4 FFT, IQ-image correction, and a measured dBFS→dBm calibration.
On top of that this project adds an Arinst protocol + sweep layer (`firmware/src/arinst/`).

GPL-3.0-or-later; files derived from widscan58/esp-sdr keep their headers.

## How it works

```
Frontera app --scn20--> USB-Serial/JTAG --> arinst.c --> radio_tune + radio_capture + psd_compute
            <--packet--                                   (widscan58 C5 radio core)
```

1. The app sends `scn20 <start> <stop> <step> 200 <samples> 10700000 <att> <idx>`.
2. `arinst_plan` (host-tested, `firmware/src/arinst/arinst_sweep.c`) tiles the requested
   band with LO hops of ~32 MHz (40 MS/s × 0.8 usable), but only where the C5's PLL locks.
3. Each hop: `radio_tune` → `radio_capture` → `psd_compute` → map FFT bins to the Arinst
   3 MHz grid → dBFS + calibration → dBm.
4. `arinst_build_packet` emits the exact Arinst reply: `scn20 <start> <idx>\r\n`, big-endian
   16-bit words (`dBm = (800 − word&0x7FF)/10`), `FF FF`, `<elapsed>\r\ncomplete\r\n`.
   Verified byte-for-byte against the app's parser and pyarinst.

## Two hard constraints (both from the C5 silicon)

- **Coverage is 2.4 GHz and ~5.15–5.95 GHz only.** The C5 PLL locks near 2.4 and 5 GHz, so
  points outside those bands come back at a floor level. That still covers the FPV / drone
  bands (2.4 GHz and 5.8 GHz), which is the point. The 5 GHz range matches widscan58's proven
  FPV sweep; known board spurs (5600/5712/5760/5920 MHz) are handled by the radio core.
- **USB identity cannot be faked.** The C5 has only a USB-Serial/JTAG controller (no USB-OTG),
  so its VID/PID is fixed at Espressif `303a:1001` and TinyUSB/custom descriptors are impossible.
  The device therefore cannot impersonate a real Arinst (`0483:5740`), so the **stock** Frontera
  app will not open it. The **modded** app (`../modded_app`) is extended to accept `303a:1001`
  as an analyzer (device_filter + the manufacturer shim); the C5 speaks Arinst natively, so no
  protocol translation is needed on the phone (unlike the tinySA).

## Status

- **Sweep/packet logic: host-tested and passing** (`firmware/test/test_arinst_sweep.c`): a
  synthetic 5843 MHz carrier round-trips to −40 dBm through the app's own parser, in-band floor
  to −100 dBm, out-of-band to the clamp.
- **Firmware: builds on the widscan58 base**; wired as the default `PROTO_ARINST` protocol.
- **Not yet done:** flash to a real XIAO C5 and confirm RF (needs the board) + a power
  calibration (`arinst_set_cal`, currently a placeholder; detection is floor-relative so the
  absolute offset barely matters, but set it with widscan58's `tools/powercal.py` for real dBm).

## Build

pioarduino 55.03.312-1 (ESP-IDF 5.5.5, PlatformIO Core 6.2.x). Run from PowerShell — pioarduino's
`idf_tools.py` refuses Git Bash.

```powershell
cd firmware
pio run -e esp32c5                 # build
pio run -e esp32c5 -t upload       # flash the XIAO C5 over its native USB
```

If upload stalls: hold BOOT, tap RESET, release BOOT, then upload (widscan58's flashing notes
apply; the C5 bootloader is at 0x2000).

Host test of the sweep logic (no board):

```sh
gcc -std=c11 -Wall -Isrc/arinst -o t firmware/test/test_arinst_sweep.c firmware/src/arinst/arinst_sweep.c -lm && ./t
```

## Using it with the phone

1. Flash the C5.
2. Install the modded app (`../modded_app/build/signed/patched+bridge-aligned-debugSigned.apk`),
   which accepts `303a:1001`.
3. Plug the C5 into the phone (USB-C). The XIAO C5 is self-powered and low-draw, unlike the tinySA.
4. Tap "OK" on the "Open Frontera?" prompt; scan the 2.4 or 5.8 GHz band.

## External antenna (XIAO ESP32-C5)

The XIAO C5 has an onboard antenna and a u.FL connector selected by a board RF switch. Confirm
the switch GPIO from Seeed's schematic and set it with the firmware's GPIO output (persisted),
the same mechanism widscan58 uses for antenna switches, before relying on the external antenna.

## Layout

- `firmware/src/arinst/` — **new**: Arinst protocol + sweep (`arinst.c` glue, `arinst_sweep.c` pure logic).
- `firmware/src/radio/`, `dsp/`, `proto/` — from widscan58 (C5 radio, FFT/PSD, USB-Serial/JTAG link).
- `firmware/src/proto/commands.c` — extended: `PROTO_ARINST` is the default; `scn20`/`test`/… route to `arinst_service`.
- `firmware/test/` — host test of the sweep logic.
- `reference/` — upstream esp-sdr and widscan58 checkouts (not tracked).
