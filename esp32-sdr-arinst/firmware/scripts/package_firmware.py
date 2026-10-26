#!/usr/bin/env python3
"""Validate and package PlatformIO's merged image for flashing at address zero.

pioarduino treats merge-bin failures as warnings. Require a complete factory image
and check it against this build's components before publishing a release asset.
GPL-3.0-or-later.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct


TARGETS = {"esp32c5": (23, 0x2000), "esp32s3": (9, 0x0000)}
PARTS = {"bootloader": "bootloader.bin", "partition-table": "partitions.bin", "app": "firmware.bin"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def package(build_dir, target, version, out_dir):
    """Return the published image path; reject incomplete or mismatched builds."""
    if target not in TARGETS:
        raise ValueError(f"Unsupported target: {target}")
    if not re.fullmatch(r"[0-9a-f]{7,40}", version):
        raise ValueError("Version must be a 7-40 character lowercase commit SHA")
    build_dir, out_dir = Path(build_dir), Path(out_dir)
    args = json.loads((build_dir / "flasher_args.json").read_text(encoding="utf-8"))
    if args["extra_esptool_args"]["chip"] != target:
        raise ValueError("Build target does not match the requested chip")
    settings = args["flash_settings"]
    # The repository's board profiles and partition table deliberately use 4 MB.
    if settings["flash_size"] != "4MB":
        raise ValueError("Expected the repository's 4MB flash profile")
    image = (build_dir / "firmware.factory.bin").read_bytes()
    if not image or len(image) > 4 * 1024 * 1024:
        raise ValueError("Factory image is empty or exceeds the 4MB flash profile")

    chip_id, boot_offset = TARGETS[target]
    expected_offsets = {"bootloader": boot_offset, "partition-table": 0x8000, "app": 0x10000}
    declared_files = {}
    segments = []
    for name, filename in PARTS.items():
        entry = args[name]
        offset = int(entry["offset"], 0)
        if offset != expected_offsets[name]:
            raise ValueError(f"Unexpected {name} offset: {offset:#x}")
        if str(entry.get("encrypted", "false")).lower() != "false":
            raise ValueError("Encrypted images are not supported by this release workflow")
        declared_files[offset] = entry["file"]
        data = (build_dir / filename).read_bytes()
        if not data:
            raise ValueError(f"Empty component: {filename}")
        if name != "partition-table":
            if len(data) < 24 or data[0] != 0xE9 or struct.unpack_from("<H", data, 12)[0] != chip_id:
                raise ValueError(f"Wrong chip or invalid ESP image header: {filename}")
        # Bootloader settings are already set by elf2image in this pinned build.
        # Refuse a changed header too: it would not describe the same build.
        if image[offset:offset + len(data)] != data:
            raise ValueError(f"Factory image does not contain {filename} at {offset:#x}")
        segments.append({"file": filename, "offset": offset, "size": len(data), "sha256": sha256(data)})

    flash_files = {int(offset, 0): filename for offset, filename in args["flash_files"].items()}
    if len(flash_files) != len(args["flash_files"]) or flash_files != declared_files:
        raise ValueError("Unexpected or inconsistent flash_files in flasher_args.json")
    end = 0
    for segment in sorted(segments, key=lambda item: item["offset"]):
        offset = segment["offset"]
        if offset < end:
            raise ValueError("Flash components overlap")
        if image[end:offset] != b"\xff" * (offset - end):
            raise ValueError(f"Unexpected data in factory padding before {offset:#x}")
        end = offset + segment["size"]
    if image[end:] != b"\xff" * (len(image) - end):
        raise ValueError("Unexpected data after the application image")

    basename = f"bandera-{target}-{version}.factory.bin"
    digest = sha256(image)
    metadata = {
        "file": basename,
        "chip": target,
        "version": version,
        "flash_offset": 0,
        "flash_settings": settings,
        "size": len(image),
        "sha256": digest,
        "segments": sorted(segments, key=lambda item: item["offset"]),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / basename
    destination.write_bytes(image)
    (out_dir / (basename + ".sha256")).write_text(
        f"{digest}  {basename}\n", encoding="utf-8", newline="\n")
    (out_dir / (basename + ".json")).write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Verified {target}: {len(image):,} bytes, flash at 0x0, SHA256 {digest}")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        package(args.build_dir, args.target, args.version, args.out_dir)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Firmware packaging failed: {exc}\n")


if __name__ == "__main__":
    main()
