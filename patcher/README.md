# Bandera APK patcher

Builds a debug-signed Bandera APK from **Frontera 2.22**, without changing its neural-network
models. Other source versions are rejected until their USB selection code and resources have
been inspected. The Android package remains `com.koritin.frontera`.

Requirements: Python 3.10+, Java 17 or newer, `apktool.jar` **3.0.3**, and
`uber-apk-signer.jar` **1.3.0** in `tools/`. Run Python/Java from Git Bash on this Windows host.

```bash
python patcher/patch.py --apk /c/Users/konra/Downloads/Frontera_2.22.apk \
  --out build/bandera.apk --version research-20261026 --version-code 23 --tools-dir tools
python -m unittest discover -s patcher -p 'test_*.py'
```

Each invocation creates a new build directory inside `--work` (default `build/work`) and uses
a fresh apktool framework cache. Existing build directories are retained. `--framework` can
override the cache, but an old cache may fail on SDK 35/36 resource attributes.

The patch changes icons, visible product names, and Telegram resources from `overlay/`. It
adds USB `303a:1001` to the attach filter, substitutes the C5 manufacturer at analyzer selection
sites, and includes that exact VID/PID pair in the connection service's presence and permission
checks. Vendor/product substitutions apply only to those service checks; the serial driver
still receives the real USB descriptors. Other USB devices retain their original identity.
The shim's own fallback calls are never rewritten.

Required source version, package, locale resources, analyzer selection calls, and service USB
checks are verified before producing an artifact. Signing must produce exactly one APK.
The final smoke check runs apktool's bundled `aapt2 dump badging`, checks the Bandera label,
package/version and launcher activity, and compares every TFLite/golden asset with the stock
APK by SHA-256. This is a packaging check; it does not claim an on-device launch or USB test.

`overlay/smali/ua/bandera/bridge/Bridge.smali` is the USB compatibility shim. Files under
`overlay/res/` supply the icon, wordmark and other branding assets. `modded_app/` is unrelated
scratch space; do not edit it as the source of a patch.

Debug signing is suitable for sideloading. Android requires matching signing certificates for
updates: keep using the same signing identity, or uninstall the differently signed app first
(which can remove its local data). A production signing key is not created by this script.

The on-demand GitHub workflow accepts `apk_url` or the repository variable `FRONTERA_APK_URL`.
It stops if neither exists, downloads the tool versions above, and runs these same checks before
publishing the APK. Run numbers supply `versionCode`; the source SHA supplies `versionName`.
Download authentication is sent only to the exact `github.com` and `api.github.com` hosts.
