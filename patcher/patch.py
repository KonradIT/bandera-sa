#!/usr/bin/env python3
"""Patch a Frontera APK into Bandera: rebrand, Tryzub icon, B-A mark, Telegram -> @SSternenko,
and accept the ESP32-C5 Arinst analyzer (USB 303a:1001) via a smali manufacturer shim.

Designed to run on a freshly decoded, verified Frontera 2.22 APK: binary assets
come from overlay/, everything else is applied as text edits keyed by name/pattern, not by
replacing whole files.

  python patch.py --apk frontera.apk --out bandera.apk --version <sha> \
                  --tools-dir tools --framework .framework [--version-code N]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # emoji in diagnostics on a cp1252 console
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ESP_VENDOR_ID = 12346   # 0x303A Espressif
ESP_PRODUCT_ID = 4097   # 0x1001 USB-Serial/JTAG
SUPPORTED_VERSION = "2.22"
PACKAGE = "com.koritin.frontera"
SERVICE = "com/example/frontera/services/ConnectionService"

# String resources replaced by name (value-agnostic), per locale file basename.
STRING_EDITS = {
    "values/strings.xml": {
        "app_name": "Bandera",
        "app_icon_description": "Bandera",
        "device_frontera": "Bandera",
        "manufacturer_name": "Bandera RE \U0001F1FA\U0001F1E6",
        "telegram_channel_link": "@SSternenko",
        "telegram_channel_url": "https://telegram.me/ssternenko",
        "telegram_qr_description": "QR-код телеграм-канала Bandera RE",
    },
    "values-en/strings.xml": {
        "app_icon_description": "Bandera_App",
        "device_frontera": "Bandera",
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


def check_source(decoded: Path) -> None:
    """Fail before editing when a new APK changes our verified patch contract."""
    text = (decoded / "apktool.yml").read_text(encoding="utf-8")
    version = re.search(r"^\s*versionName:\s*['\"]?([^'\"\s]+)", text, re.M)
    if version is None or version[1] != SUPPORTED_VERSION:
        raise ValueError(f"only Frontera {SUPPORTED_VERSION} is verified; inspect this APK before updating the patcher")
    if ET.parse(decoded / "AndroidManifest.xml").getroot().get("package") != PACKAGE:
        raise ValueError("unexpected source APK package")
    for rel in ("i9/j.smali", SERVICE + ".smali"):
        if not GET_MANUFACTURER.search((decoded / "smali" / rel).read_text(encoding="utf-8")):
            raise ValueError(f"required analyzer selection call missing: {rel}")


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
            raise ValueError(f"required locale missing: {rel}")
        text = path.read_text(encoding="utf-8")
        for name, value in subs.items():
            pat = re.compile(r'(<string name="%s"[^>]*>).*?(</string>)' % re.escape(name), re.DOTALL)
            new, n = pat.subn(lambda m, v=value: m.group(1) + v + m.group(2), text)
            if n == 1:
                text = new
                print(f"string {rel}:{name} -> {value}")
            else:
                raise ValueError(f"expected one string {rel}:{name}, found {n}")
        # Preserve translated text while finishing the visible product-name rebrand.
        text = re.sub(r'(<string name="overlay_switch_hint"[^>]*>)(.*?)(</string>)',
                      lambda m: m[1] + m[2].replace("Frontera", "Bandera") + m[3], text, flags=re.S)
        path.write_text(text, encoding="utf-8")


def edit_device_filter(decoded: Path) -> None:
    path = decoded / "res/xml/device_filter.xml"
    if not path.exists():
        raise ValueError("required device_filter.xml missing")
    text = path.read_text(encoding="utf-8")
    devices = ET.fromstring(text).findall("usb-device")
    if any(d.get("vendor-id") == str(ESP_VENDOR_ID) and d.get("product-id") == str(ESP_PRODUCT_ID) for d in devices):
        print("device_filter: ESP entry already present")
        return
    entry = '    <usb-device product-id="%d" vendor-id="%d" />\n' % (ESP_PRODUCT_ID, ESP_VENDOR_ID)
    if text.count("</resources>") != 1:
        raise ValueError("unexpected device filter root")
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


def patch_connection_ids(decoded: Path) -> None:
    """Include the C5 in service attach/permission checks, leaving USB drivers alone."""
    for suffix, expected in ((".smali", 3), ("$usbReceiver$1.smali", 1)):
        path = decoded / "smali" / (SERVICE + suffix)
        text = path.read_text(encoding="utf-8")
        for original, replacement in (("getVendorId", "analyzerVendor"), ("getProductId", "analyzerProduct")):
            pattern = rf"invoke-virtual (\{{[pv]\d+\}}), Landroid/hardware/usb/UsbDevice;->{original}\(\)I"
            text, count = re.subn(pattern, rf"invoke-static \1, Lua/bandera/bridge/Bridge;->{replacement}(Landroid/hardware/usb/UsbDevice;)I", text)
            if count != expected:
                raise ValueError(f"{path.name}: expected {expected} {original} checks, found {count}")
        path.write_text(text, encoding="utf-8")
        print(f"service C5 USB checks: {path.name}")


def set_version(decoded: Path, version_name: str, version_code: str | None) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", version_name):
        raise ValueError("version must contain only letters, digits, '.', '_', '+', or '-'")
    if version_code is not None and (not version_code.isdecimal() or not 0 < int(version_code) <= 2100000000):
        raise ValueError("version-code must be a positive Android version integer")
    yml = decoded / "apktool.yml"
    text = yml.read_text(encoding="utf-8")
    text, n = re.subn(r"versionName: .*", "versionName: %s" % version_name, text)
    if n != 1:
        raise ValueError("expected exactly one versionName in apktool.yml")
    print(f"versionName -> {version_name}")
    if version_code:
        text, n = re.subn(r"versionCode: .*", "versionCode: %s" % version_code, text)
        if n != 1:
            raise ValueError("expected exactly one versionCode in apktool.yml")
        print(f"versionCode -> {version_code}")
    yml.write_text(text, encoding="utf-8")


def verify_apk(stock: Path, signed: Path, apktool: Path, work: Path, version: str) -> None:
    """Check packaging, unchanged neural-net assets, and a launchable manifest."""
    def models(path: Path) -> dict[str, str]:
        with zipfile.ZipFile(path) as apk:
            return {name: hashlib.sha256(apk.read(name)).hexdigest() for name in apk.namelist()
                    if name.startswith("assets/") and (name.endswith(".tflite") or name.endswith("_golden.bin"))}
    original = models(stock)
    if not original or models(signed) != original:
        raise ValueError("model/golden assets changed or disappeared from APK")
    platform = "windows" if os.name == "nt" else "macosx" if sys.platform == "darwin" else "linux"
    binary = "aapt2.exe" if os.name == "nt" else "aapt2"
    executable = work / binary
    with zipfile.ZipFile(apktool) as archive:
        executable.write_bytes(archive.read(f"prebuilt/{platform}/{binary}"))
    executable.chmod(executable.stat().st_mode | 0o111)
    badging = subprocess.check_output([str(executable), "dump", "badging", str(signed)], text=True, encoding="utf-8")
    if f"package: name='{PACKAGE}'" not in badging or f"versionName='{version}'" not in badging:
        raise ValueError("built APK package/version mismatch")
    if "launchable-activity:" not in badging or "application-label:'Bandera'" not in badging:
        raise ValueError("built APK lacks Bandera label or launcher activity")
    print(f"verified: launchable Bandera manifest; {len(original)} unchanged model/golden assets")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", required=True, help="versionName to stamp (e.g. the main SHA)")
    ap.add_argument("--version-code", default=None)
    ap.add_argument("--tools-dir", default=str(HERE.parent / "tools"))
    ap.add_argument("--overlay", default=str(HERE / "overlay"))
    ap.add_argument("--framework", default=None, help="apktool framework cache (default: fresh cache per build)")
    ap.add_argument("--work", default=str(HERE.parent / "build/work"))
    args = ap.parse_args()

    tools = Path(args.tools_dir)
    apktool = tools / "apktool.jar"
    signer = tools / "uber-apk-signer.jar"
    for j in (apktool, signer):
        if not j.exists():
            sys.exit(f"missing tool: {j}")
    work_root = Path(args.work).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="bandera-", dir=work_root))
    fw = Path(args.framework).resolve() if args.framework else work / "framework"
    fw.mkdir(parents=True, exist_ok=True)
    apktool, signer = apktool.resolve(), signer.resolve()
    stock = Path(args.apk).resolve()
    print("build directory:", work)
    decoded = work / "decoded"

    run(["java", "-jar", str(apktool), "d", "-f", "-p", str(fw), "-o", str(decoded), str(stock)])
    check_source(decoded)
    apply_overlay(decoded, Path(args.overlay))
    edit_strings(decoded)
    edit_device_filter(decoded)
    patch_smali(decoded)
    patch_connection_ids(decoded)
    set_version(decoded, args.version, args.version_code)

    built = work / "built.apk"
    run(["java", "-jar", str(apktool), "b", "-p", str(fw), "-o", str(built), str(decoded)])
    run(["java", "-jar", str(signer), "--apks", str(built), "--allowResign", "-o", str(work / "signed")])

    signed = list((work / "signed").glob("*Signed.apk"))
    if len(signed) != 1:
        sys.exit("ERROR: signing did not produce exactly one APK")
    verify_apk(stock, signed[0], apktool, work, args.version)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(signed[0], args.out)
    print("\nPATCHED ->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
