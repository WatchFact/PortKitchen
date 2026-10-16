# PortKitchen

**Samsung One UI donor-to-target firmware kitchen, run locally or with GitHub Actions.**

PortKitchen replaces **framework-side** partitions (by default `system`, `system_ext`, `product`) from a One UI donor build while preserving the target device's hardware-side images (`vendor`, `vendor_dlkm`, `odm`, `boot`, `dtbo`, etc.). It handles common Samsung AP `tar.md5`, ZIP (including AP tar inside ZIP), `.img.lz4`, Android sparse images, and dynamic `super.img` using AOSP logical partition tools. It can also assemble a new `super.img` with a *reviewed target-device layout*.

> **Developer preview, NOT a bootable-ROM generator.** Cross-device One UI porting also requires device-specific framework/vendor compatibility, VINTF, SELinux, RIL, camera, encryption, fstab, kernel, recovery and AVB work. A successful build does not establish bootability. PortKitchen NEVER flashes a device, patches the bootloader, disables verification, or bypasses unlocking. Do not flash the output without completing these adaptations and validating them for the exact device.

## Use GitHub Actions

1. Open [Actions → Prepare One UI Port](https://github.com/WatchFact/PortKitchen/actions/workflows/port.yml) and select **Run workflow**.
2. Supply three direct HTTPS links: donor firmware, target stock firmware, and a JSON device profile. Provide SHA-256 hashes when available.
3. Optionally select `assemble_super`. Only do so when `profile.super` contains the exact target partition geometry, group assignments and slot layout, obtained from that device's stock firmware.
4. Download the `PortKitchen-N` Actions artifact from the finished run. `images/`, `manifest.json` and `READ_BEFORE_FLASHING.txt` are generated. Artifacts are **not** GitHub Releases and may contain copyrighted firmware; the public Releases page distributes only the software.

The workflow uses an Ubuntu runner, pinned Android 15 partition utilities and LZ4. Large Samsung firmware can exceed free-hosted runner disk or artifact quotas; use a self-hosted runner with sufficient storage when required. Avoid passing short-lived/private signed URLs in a publicly visible workflow: Actions inputs and logs can expose sensitive links. Only process firmware you may lawfully use.

## Local usage

Requirements: Python 3.10+, `lz4`, `simg2img`, `lpunpack`, and for logical partition assembly `lpmake` and `lpdump` (Android AOSP partition tools).

```bash
python -m pip install -e .
python -m portkitchen inspect --firmware AP_TARGET.tar.md5 --output /tmp/target-inspect
python -m portkitchen build \
  --donor DONOR.zip --target TARGET.tar.md5 \
  --profile device-profile.json --output output
# Once target super geometry is confirmed:
python -m portkitchen build \
  --donor DONOR.zip --target TARGET.tar.md5 \
  --profile device-profile.json --output clean-output --assemble-super
```

Inspect requires an empty destination for reliable results; `build` refuses to reuse nonempty outputs. Firmware directory input is supported as well as ZIP/TAR. The build emits checksums to make image provenance verifiable.

## Device profile

Start with [`config/example-profile.json`](config/example-profile.json). Set the exact Samsung donor and target model identifiers. `compatibility_confirmed: true` represents a **manual** device-specific review, not an automated guarantee. Without it the build refuses to run. If ZIP/TAR includes optional `firmware.json` metadata (model, Android API, architecture), PortKitchen validates declared values. Official Samsung firmware may not provide that manifest; missing metadata cannot be inferred from an opaque filesystem image.

For optional super assembly add exact stock-target geometry (example values below are illustrative, **NOT** for flashing):

```json
{
  "donor_model": "SM-DONOR",
  "target_model": "SM-TARGET",
  "compatibility_confirmed": true,
  "donor_partitions": ["system", "product", "system_ext"],
  "super": {
    "device_size": 8589934592,
    "metadata_size": 65536,
    "metadata_slots": 2,
    "groups": { "main_dynamic_partitions": 8000000000 },
    "partitions": {
      "system": "main_dynamic_partitions",
      "vendor": "main_dynamic_partitions",
      "product": "main_dynamic_partitions",
      "system_ext": "main_dynamic_partitions"
    }
  }
}
```

Copy the actual target metadata from `lpdump` and check partition names, slot suffixes, group sizes and physical super device size. Only specified layout partitions enter `super.img`; other staged images remain separate. `lpmake` validates space and the output is checked with `lpdump`. Samsung Odin/Heimdall packaging is deliberately **not generated** without a validated target flashing profile.

## Safeguards and current boundaries

- Rejects ZIP/TAR traversal and symlink entries, duplicate partitions, image layout mismatches and oversized archive entries.
- Preserves the original **target** kernel/boot/vendor-related partitions; donor selection is limited to `system`, `system_ext`, `product`.
- Detects declared API/architecture mismatches and checks target layout group sizes before assembly.
- Supports unsuffixed and `_a` images; other logical A/B arrangements require device-specific handling.
- Does not support `payload.bin` OTAs, encrypted firmware, AP archives using codecs other than LZ4, automatic EROFS/EXT4 file edits, file-context rebuilding or verified device-specific patches yet.
- Samsung proprietary firmware must be obtained separately; never commit firmware/images to the repository or upload them as a public release.

## Development

```bash
python -m pip install -e . pytest build
python -m pytest -q
python -m build
```

The PortKitchen source code is **MIT licensed**. Samsung firmware and external AOSP utilities keep their respective licenses; this project's license does not grant redistribution rights to firmware.
