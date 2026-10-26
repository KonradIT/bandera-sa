#!/usr/bin/env python3
"""Patch a Frontera APK into Bandera: rebrand, Tryzub icon, B-A mark, Telegram -> @SSternenko,
and accept the ESP32-C5 Arinst analyzer (USB 303a:1001) via a smali manufacturer shim.

Designed to run on a freshly decoded APK (so it survives minor Frontera updates): binary assets
come from overlay/, everything else is applied as text edits keyed by name/pattern, not by
replacing whole files.

  python patch.py --apk frontera.apk --out bandera.apk --version <sha> \
                  --tools-dir tools --framework .framework [--version-code N]
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # emoji in diagnostics on a cp1252 console
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ESP_VENDOR_ID = 12346   # 0x303A Espressif
ESP_PRODUCT_ID = 4097   # 0x1001 USB-Serial/JTAG

# String resources replaced by name (value-agnostic), per locale file basename.
STRING_EDITS = {
    "values/strings.xml": {
        "app_name": "Bandera",
        "manufacturer_name": "Bandera RE \U0001F1FA\U0001F1E6",
        "telegram_channel_link": "@SSternenko",
        "telegram_channel_url": "https://telegram.me/ssternenko",
        "telegram_qr_description": "QR-код телеграм-канала Bandera RE",
    },
    "values-en/strings.xml": {
        "app_name": "Bandera",
        "manufacturer_name": "Bandera RE \U0001F1FA\U0001F1E6",
        "telegram_qr_description": "QR code of Bandera RE Telegram channel",
    },
}

GET_MANUFACTURER = re.compile(
    r"invoke-virtual (\{[pv]\d+\}), Landroid/hardware/usb/UsbDevice;->getManufacturerName\(\)Ljava/lang/String;")
MANUFACTURER_SHIM = (
    r"invoke-static \1, Lua/bandera/bridge/Bridge;->manufacturer(Landroid/hardware/usb/UsbDevice;)Ljava/lang/String;")


def run(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True)


def apply_overlay(decoded: Path, overlay: Path) -> None:
    for src in overlay.rglob("*"):
        if src.is_file():
            dst = decoded / src.relative_to(overlay)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            print("overlay:", dst.relative_to(decoded))
    # The F/A wordmark vector is replaced by the B/A raster in drawable-nodpi; drop the old vector.
    old = decoded / "res/drawable/ic_back_letter.xml"
    if old.exists():
        old.unlink()
        print("removed:", old.relative_to(decoded))


def edit_strings(decoded: Path) -> None:
    for rel, subs in STRING_EDITS.items():
        path = decoded / "res" / rel
        if not path.exists():
            print("skip (absent):", rel)
            continue
        text = path.read_text(encoding="utf-8")
        for name, value in subs.items():
            pat = re.compile(r'(<string name="%s"[^>]*>).*?(</string>)' % re.escape(name), re.DOTALL)
            new, n = pat.subn(lambda m, v=value: m.group(1) + v + m.group(2), text)
            if n:
                text = new
                print(f"string {rel}:{name} -> {value}")
            else:
                print(f"string {rel}:{name} not found (skipped)")
        path.write_text(text, encoding="utf-8")


def edit_device_filter(decoded: Path) -> None:
    path = decoded / "res/xml/device_filter.xml"
    if not path.exists():
        print("WARNING: device_filter.xml not found; the C5 won't trigger the USB attach dialog")
        return
    text = path.read_text(encoding="utf-8")
    if str(ESP_VENDOR_ID) in text and str(ESP_PRODUCT_ID) in text:
        print("device_filter: ESP entry already present")
        return
    entry = '    <usb-device product-id="%d" vendor-id="%d" />\n' % (ESP_PRODUCT_ID, ESP_VENDOR_ID)
    text = text.replace("</resources>", entry + "</resources>", 1)
    path.write_text(text, encoding="utf-8")
    print("device_filter: added ESP32-C5 303a:1001")


def patch_smali(decoded: Path) -> None:
    total = 0
    files = []
    for smali_dir in sorted(decoded.glob("smali*")):
        files += smali_dir.rglob("*.smali")
    for f in files:
        if "ua/bandera/bridge" in f.as_posix():
            continue  # never rewrite the shim's own fallback getManufacturerName() -> infinite recursion
        text = f.read_text(encoding="utf-8")
        new, n = GET_MANUFACTURER.subn(MANUFACTURER_SHIM, text)
        if n:
            f.write_text(new, encoding="utf-8")
            total += n
            print(f"smali shim: {f.relative_to(decoded)} ({n})")
    if total == 0:
        sys.exit("ERROR: no getManufacturerName() call sites found to shim")
    # Bridge.smali must land in the primary dex (smali/), where the overlay put it.
    if not (decoded / "smali/ua/bandera/bridge/Bridge.smali").exists():
        sys.exit("ERROR: Bridge.smali missing from overlay")
    print(f"smali shim applied at {total} site(s)")


def set_version(decoded: Path, version_name: str, version_code: str | None) -> None:
    yml = decoded / "apktool.yml"
    text = yml.read_text(encoding="utf-8")
    text, n = re.subn(r"versionName: .*", "versionName: %s" % version_name, text)
    print(f"versionName -> {version_name}" + ("" if n else " (not found!)"))
    if version_code:
        text, _ = re.subn(r"versionCode: .*", "versionCode: %s" % version_code, text)
        print(f"versionCode -> {version_code}")
    yml.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", required=True, help="versionName to stamp (e.g. the main SHA)")
    ap.add_argument("--version-code", default=None)
    ap.add_argument("--tools-dir", default=str(HERE.parent / "tools"))
    ap.add_argument("--overlay", default=str(HERE / "overlay"))
    ap.add_argument("--framework", default=str(HERE.parent / "build/.framework"))
    ap.add_argument("--work", default=str(HERE.parent / "build/work"))
    args = ap.parse_args()

    tools = Path(args.tools_dir)
    apktool = tools / "apktool.jar"
    signer = tools / "uber-apk-signer.jar"
    for j in (apktool, signer):
        if not j.exists():
            sys.exit(f"missing tool: {j}")
    fw = Path(args.framework); fw.mkdir(parents=True, exist_ok=True)
    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    decoded = work / "decoded"

    run(["java", "-jar", str(apktool), "d", "-f", "-p", str(fw), "-o", str(decoded), args.apk])
    apply_overlay(decoded, Path(args.overlay))
    edit_strings(decoded)
    edit_device_filter(decoded)
    patch_smali(decoded)
    set_version(decoded, args.version, args.version_code)

    built = work / "built.apk"
    run(["java", "-jar", str(apktool), "b", "-p", str(fw), "-o", str(built), str(decoded)])
    run(["java", "-jar", str(signer), "--apks", str(built), "--allowResign", "-o", str(work / "signed")])

    signed = glob.glob(str(work / "signed" / "*Signed.apk"))
    if not signed:
        sys.exit("ERROR: signing produced no APK")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(signed[0], args.out)
    print("\nPATCHED ->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
