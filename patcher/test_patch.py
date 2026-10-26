"""Regression checks for APK version drift and narrowly scoped USB shims."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("bandera_patch", Path(__file__).with_name("patch.py"))
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_version_drift_fails_before_patching(self):
        self.write("apktool.yml", "versionInfo:\n  versionName: 2.23\n")
        with self.assertRaisesRegex(ValueError, "only Frontera 2.22"):
            patch.check_source(self.root)

    def test_filter_checks_vid_pid_as_one_pair(self):
        path = self.write("res/xml/device_filter.xml", '<resources><usb-device vendor-id="12346" product-id="1" />'
                          '<usb-device vendor-id="1" product-id="4097" /></resources>')
        patch.edit_device_filter(self.root)
        patch.edit_device_filter(self.root)
        entries = patch.ET.fromstring(path.read_text()).findall("usb-device")
        self.assertEqual(sum(e.get("vendor-id") == "12346" and e.get("product-id") == "4097" for e in entries), 1)

    def test_shim_fallback_is_never_rewritten(self):
        invoke = "invoke-virtual {p0}, Landroid/hardware/usb/UsbDevice;->getManufacturerName()Ljava/lang/String;"
        bridge = self.write("smali/ua/bandera/bridge/Bridge.smali", invoke)
        client = self.write("smali/i9/j.smali", invoke)
        patch.patch_smali(self.root)
        self.assertEqual(bridge.read_text(), invoke)
        self.assertIn("invoke-static", client.read_text())

    def test_connection_mapping_does_not_change_driver_ids(self):
        calls = "invoke-virtual {v1}, Landroid/hardware/usb/UsbDevice;->getVendorId()I\n" \
                "invoke-virtual {v1}, Landroid/hardware/usb/UsbDevice;->getProductId()I\n"
        service = self.write("smali/" + patch.SERVICE + ".smali", calls * 3)
        receiver = self.write("smali/" + patch.SERVICE + "$usbReceiver$1.smali", calls)
        driver = self.write("smali/com/hoho/android/usbserial/driver/Test.smali", calls)
        patch.patch_connection_ids(self.root)
        self.assertEqual(service.read_text().count("->analyzerVendor"), 3)
        self.assertEqual(receiver.read_text().count("->analyzerProduct"), 1)
        self.assertEqual(driver.read_text(), calls)

    def test_missing_service_checks_are_rejected(self):
        self.write("smali/" + patch.SERVICE + ".smali", "")
        with self.assertRaisesRegex(ValueError, "expected 3 getVendorId"):
            patch.patch_connection_ids(self.root)

    def test_invalid_versions_are_rejected(self):
        for name, code in (("x\nversionCode: 2", "1"), ("ok", "0"), ("ok", "'2'")):
            with self.subTest(name=name, code=code), self.assertRaises(ValueError):
                patch.set_version(self.root, name, code)


if __name__ == "__main__":
    unittest.main()
