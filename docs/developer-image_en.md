# Developer disk image (personalized DDI)

iOS 17 and later use a **personalized Developer Disk Image**. Mounting follows `ApChipID` and `ApBoardID` in `Restore/BuildManifest.plist`, not a single `DeveloperDiskImage.dmg` chosen by OS version alone.

Implementation: [`backend_function/ddi_manager.py`](../backend_function/ddi_manager.py).

## Local paths

| Path | Role |
|:-----|:-----|
| `runtime/devimages/` | Runtime download cache (default `DEVIMAGES_DIR`) |
| `IOSPrechecker/devimages/` | Optional manually placed images (also searched) |
| `IOSPrechecker/executable/` | `ios` / `ios.exe` unpacked from `utils` (not in git) |

Upstream source: [doronz88/DeveloperDiskImage](https://github.com/doronz88/DeveloperDiskImage) `main`, under `PersonalizedImages/Xcode_iOS_DDI_Personalized/`.

## Mount flow (summary)

1. Image already mounted on device → success.
2. Read chip identity via `pymobiledevice3` when installed, or parse from go-ios `image auto` / `findIdentity` errors.
3. Hard-mount only matching `**/Restore` under `runtime/devimages` (and local `IOSPrechecker/devimages` if present).
4. No match → download upstream `BuildManifest.plist`; if it contains this device, fetch `Image.dmg` and trustcache into `runtime/devimages/Xcode_iOS_DDI_Personalized/Restore`.
5. Still failing → bundled go-ios `image auto` (older builds may still use `ddi-15F31d`).
6. Last resort → `pymobiledevice3 mounter auto-mount` (requires `pymobiledevice3`).

Matching local images are reused. Payloads are not downloaded when the upstream manifest lacks this chip identity.

## Diagnostics

- HTTP: `POST /api/image/auto` with body `{"udid":"<device UDID>"}`.
- Logs: `runtime/logs/ios_app.log` (search for `ApChipId`, `BoardId`, `DDI`).
- Tests: `python -m unittest tests.test_ddi_manager`

## Optional dependency

```bash
pip install "pymobiledevice3>=4.0.0"
```

For `query-personalization-identifiers` and `mounter auto-mount` fallback.
