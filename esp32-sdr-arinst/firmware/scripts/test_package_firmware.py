"""Release checks for complete images, chip selection, and corrupted builds."""

import json
from pathlib import Path
import struct
import tempfile
import unittest

from package_firmware import package


class FactoryPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.build = Path(self.temp.name) / "build"
        self.output = Path(self.temp.name) / "output"
        self.build.mkdir()

    def fixture(self, target="esp32c5"):
        chip, boot = (23, 0x2000) if target == "esp32c5" else (9, 0)
        args = {"extra_esptool_args": {"chip": target},
                "flash_settings": {"flash_mode": "dio", "flash_size": "4MB", "flash_freq": "80m"},
                "flash_files": {}}
        image = bytearray(b"\xff" * 0x10100)
        for name, file, idf_file, offset in [
            ("bootloader", "bootloader.bin", "bootloader/bootloader.bin", boot),
            ("partition-table", "partitions.bin", "partition_table/partition-table.bin", 0x8000),
            ("app", "firmware.bin", "widscan58.bin", 0x10000),
        ]:
            data = bytearray(256)
            data[0] = 0xE9
            struct.pack_into("<H", data, 12, chip)
            (self.build / file).write_bytes(data)
            image[offset:offset + len(data)] = data
            args[name] = {"offset": hex(offset), "file": idf_file, "encrypted": "false"}
            args["flash_files"][hex(offset)] = idf_file
        (self.build / "flasher_args.json").write_text(json.dumps(args))
        (self.build / "firmware.factory.bin").write_bytes(image)
        return args

    def run_package(self, target="esp32c5"):
        return package(self.build, target, "abcdef0", self.output)

    def test_both_chips_and_checksums(self):
        for target in ("esp32c5", "esp32s3"):
            with self.subTest(target=target):
                self.fixture(target)
                result = self.run_package(target)
                self.assertEqual(result.read_bytes(), (self.build / "firmware.factory.bin").read_bytes())
                meta = json.loads(result.with_suffix(".bin.json").read_text())
                self.assertEqual(meta["flash_offset"], 0)
                self.assertEqual(meta["chip"], target)
                # sha256sum treats a CR from Windows CRLF as part of the filename.
                self.assertEqual(result.with_suffix(".bin.sha256").read_bytes(),
                                 f"{meta['sha256']}  {result.name}\n".encode("utf-8"))

    def test_missing_factory_and_missing_component(self):
        for missing in ("firmware.factory.bin", "bootloader.bin"):
            with self.subTest(missing=missing):
                self.fixture()
                (self.build / missing).unlink()
                with self.assertRaises(FileNotFoundError):
                    self.run_package()
                self.assertFalse(self.output.exists())

    def test_rejects_corrupted_or_app_only_factory(self):
        self.fixture()
        factory = self.build / "firmware.factory.bin"
        data = bytearray(factory.read_bytes())
        data[0x10040] ^= 1
        for invalid in (data, (self.build / "firmware.bin").read_bytes()):
            factory.write_bytes(invalid)
            with self.assertRaisesRegex(ValueError, "does not contain"):
                self.run_package()
        self.assertFalse(self.output.exists())

    def test_rejects_wrong_chip_even_if_json_matches(self):
        self.fixture()
        with self.assertRaisesRegex(ValueError, "Build target"):
            self.run_package("esp32s3")
        for file in ("bootloader.bin", "firmware.bin"):
            data = bytearray((self.build / file).read_bytes())
            struct.pack_into("<H", data, 12, 9)
            (self.build / file).write_bytes(data)
        with self.assertRaisesRegex(ValueError, "Wrong chip"):
            self.run_package()

    def test_rejects_extra_segments_and_nonempty_padding(self):
        args = self.fixture()
        args["flash_files"]["0x9000"] = "unexpected.bin"
        (self.build / "flasher_args.json").write_text(json.dumps(args))
        with self.assertRaisesRegex(ValueError, "flash_files"):
            self.run_package()
        self.fixture()
        factory = self.build / "firmware.factory.bin"
        data = bytearray(factory.read_bytes())
        data[0] = 0
        factory.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "padding"):
            self.run_package()


if __name__ == "__main__":
    unittest.main()
