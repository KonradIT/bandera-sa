# Pixel 10 integration test — 2026-10-26

The updated Bandera app displayed live C5 sweeps and detected the known analog
camera near **5806 MHz** on the Pixel 10 running Android 17. Physical USB
disconnect/reconnect and scan stop/start recovered successfully. No model changes
were needed.

## Installed build

- APK: [`build/bandera-phone.apk`](../build/bandera-phone.apk).
- Package: `com.koritin.frontera`; version `phone-20261026`, versionCode **220003**.
- APK SHA-256: `adbdbe78281328e22c5bec2087064531db286b18b3b68796b79e305fa5c42b98`.
- The installed previous build used versionCode 220002. A higher version code and
  matching signing certificate allowed `adb install -r` without uninstalling or
  clearing app data. Existing preferences and permissions were retained.
- Signatures, alignment, launcher metadata and all six unchanged model/golden
  assets passed the same packaging checks as the earlier bench build.

The previous APK was backed up under `build/phone-validation/previous-bandera.apk`.
ADB continued over the phone's already-enabled wireless debugging connection while
the C5 occupied its USB port. Android reported the real device identity `303a:1001`.

## Observed behavior

1. The updated app opened successfully, recognized the C5 and showed a changing
   spectrum and waterfall. The camera label was approximately 5805.7–5806.5 MHz.
2. An app-scoped log segment contained 76 processing/event batches over 36.06 s,
   with median spacing 0.477 s, approximately **2.1 updates/s**. These log events
   do not expose every raw sweep or every continuing model hit.
3. Unplugging the C5 produced the expected USB I/O failure; the app closed its
   connection. After reconnection it resumed scanning without an app crash.
4. A new analog track near 5806.0 MHz was emitted and subsequently confirmed
   with score 0.51. This establishes a fresh detection after reconnect rather
   than relying on an old label retained in the UI.
5. Stop changed the control to **Start scan**. Restart produced a new analog
   track at 5805.9 MHz, confirmed with score 0.69. Scanning remained operational
   afterward, including while navigating app settings.

The screenshot and app-scoped logs are local ignored artifacts under
[`build/phone-validation/`](../build/phone-validation/). The initial verified live
view is [`installed-screen.png`](../build/phone-validation/installed-screen.png);
the reconnect/restart events are in `reconnect-live.log`.

## Limits and follow-up

This demonstrates launch, USB integration, displayed acquisition and known-camera
detection. It does **not** establish a general false-positive rate: the wider scan
also produced unverified classifications, including a confirmed analog candidate
near **5657.7 MHz** and other digital-link labels. Compare those frequencies with
an independent receiver and camera-off captures before identifying their sources.

Absolute dBm remains uncalibrated. First-install permission prompting was not
reset or tested; this update reused existing permissions. DJI 2.4 GHz fidelity
and longer-duration stability remain separate tests. No scan preferences were
programmatically edited during this validation.
