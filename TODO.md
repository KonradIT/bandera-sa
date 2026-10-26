# TODO / known gaps

Updated 2026-10-26 after live receive tests on the LibreSDR and XIAO ESP32-C5.
See `docs/validation-2026-10-26.md` for measurements and `docs/protocol-research.md`
for the manufacturer protocol findings. Preserve the extracted models.

## ESP32-C5

Fixed and regression-tested:

- [x] Correct the real DSP contract: FFT-shifted output, DC at N/2, and positive
  `(frequency-LO)` offset after `psd_compute(conjugate=true)`. The previous
  extra mirror correction and natural-order fixture were wrong.
- [x] Restore full sweeps after the flat-floor/crash regression. Radio/DSP work
  runs on the scanner task; partial buffer allocation cannot leave a falsely
  initialized state. Packets go through the shared serial output queue.
- [x] Use 80 MS/s while retaining the inner +/-16 MHz passband and +/-7 MHz DC
  avoidance. Full 4200-5997 MHz frames complete in 0.406-0.438 s at the host.
- [x] Replace per-LO DC-calibration cache thrashing with one calibration per band.
  Retain per-frequency RX gain tables; do not pin one gain table across the band.
- [x] Add official leading CRLF, exact command/field parsing, count/overflow
  rejection, safe packet capacity checks, and scn22 tag handling.
- [x] Validate real analog RF: 24 sweeps, analog in 17/17 overlapping W8 windows
  and 9/9 W16 windows with unchanged models. The old 5769 MHz ghost is absent
  as a distinct peak; this does not establish absence of all board spurs.
- [x] Check the 2.4 GHz receive path with ambient RF: eight complete scans,
  52-53 ms device time. No DJI source was present.
- [x] Ignore dependency lock variants and build/capture scratch artifacts.

Remaining:

- [x] Phone-side launch, displayed sweeps, camera detection, USB reconnect and
  stop/start checked on Pixel 10 / Android 17 with the in-place update. Existing
  permissions were retained; first-install permission prompting was not reset/tested.
- [ ] DJI Mavic observations on 2.4 GHz, including repeatable idle/busy captures.
  Ambient Wi-Fi is not validation of DJI classification.
- [ ] Absolute power and frequency flatness calibration. The -30 dB offset is
  still a placeholder; the current board has residual spurs (e.g. near 5760 MHz).
- [ ] Investigate unverified phone detections away from the known camera,
  including a confirmed analog candidate near 5657.7 MHz. Compare with an
  independent receiver and camera-off controls before attributing these signals.
- [ ] Characterize Arinst-like detector statistics: the C5 takes shared FFT
  snapshots within a hop, while the Arinst measures points sequentially. Compare
  RBW, averaging, dwell, and burst behavior with a real Arinst reference.
- [ ] Long-duration thermal/reconnect testing and mixed monitor/Arinst mode
  transitions. Do not switch debugging protocols during an outstanding scan.
- [ ] Full Arinst functionality is out of scope: sample-index upper bits remain
  zero, legacy control fields are not all mapped to hardware, and no tracking
  generator is implemented.

## UHD detector and serial adapter

Completed:

- [x] Keep the optimized B210 profile as the default and add a generic UHD path
  using actual rate, frontend ranges, bandwidth, gain limits, channel and antenna.
- [x] Fix nonzero channel selection in StreamArgs, distinguish unavailable bins
  from valid samples, and reject incomplete/corrupt USB bursts.
- [x] Serve real Arinst discovery and model-grid packets over a serial endpoint.
  Frontera's 11-point discovery completed in 399 ms on the LibreSDR.
- [x] Keep overload/invalid rows out of model windows; preserve quality metadata
  in recordings and honor it in live, replay, and plotting paths.
- [x] Golden tests for W8/10/12/16 pass. C and Python encoders match all sixteen
  extracted golden sweeps byte for byte; the wire roundtrip loses no levels.
- [x] Validate the default profile on real analog RF: 24 sweeps at 1.27-1.91 s,
  analog in 13/17 W8 windows and 9/9 W16 windows.

Remaining:

- [ ] Validate other physical UHD models/daughterboards. Generic capability and
  extraction tests pass, but only the LibreSDR was attached for RF tests.
- [ ] Improve generic throughput if Android serial use is needed: 10 MS/s generic
  operation on this B210 took 6.53-6.54 s, exceeding the app's 2 s timeout. Keep
  this limitation explicit; do not fake faster sweeps by repeating old data.
- [ ] Characterize B210 timing outliers under host load. A preliminary run had
  two sweeps above 2 s even though the longer capture stayed below it.
- [ ] Connect and verify a PC-to-phone serial bridge. A B210 USB connection and
  a Windows COM server are not themselves an Android USB peripheral.
- [ ] Compare dwell/averaging settings using real DJI 2.4 GHz recordings. Do not
  tune the model or assume analog performance predicts digital-link fidelity.
- [ ] Wi-Fi around 5.2 GHz can still be labelled siyi. Preserve raw detections and
  band-filtered alerts; do not hardcode spectral class overrides.
- [ ] Mirror suppression still has tuning-limit edge cases; generic frontend
  spur/image rejection needs per-device validation.
- [ ] `sdr/c5_freqs.npy` was a pre-existing untracked probe artifact and was
  deliberately preserved. Move it only when its owner no longer needs it.

## APK patcher and CI

- [x] Guard the verified Frontera 2.22 version/package and required smali sites.
- [x] Patch all four C5 service USB presence/permission checks while retaining
  actual USB IDs for the serial driver.
- [x] Complete device-name and floating-window rebranding; add patcher README.
- [x] Use fresh build directories/framework cache, verify launcher/package/label,
  and compare all model/golden asset hashes before accepting the signed APK.
- [x] Build and verify `build/bandera-research.apk`; six patcher tests pass.
- [x] Validate CI URL hosts exactly before attaching the GitHub token; pass
  workflow input through environment values and release notes through a file.
- [x] Confirm launch and C5 attach/reconnect on Pixel 10 / Android 17 using
  `build/bandera-phone.apk` (versionCode 220003), preserving installed app data.
- [ ] Exercise first-install USB permission prompting separately from the
  successful update/reconnect test, which reused existing permissions.
- [ ] Document/use a stable release signing key if distribution grows. Current
  builds remain debug-signed; changing signer identity can prevent updates.
- [ ] Re-audit parser and patch sites for every upstream APK version.

## Repository constraints

- [x] Models and golden files remain byte-identical to the supplied stock APK.
- [x] Generated APKs, firmware outputs and validation captures stay ignored.
- [ ] No model training or replacement is part of this work. Address remaining
  measurement differences with captures and instrument behavior first.
