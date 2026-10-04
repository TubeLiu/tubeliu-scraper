# Bundled ADB: source, licenses and rebuilding

This skill redistributes an unmodified open source rebuild of ADB from
[meator/android-tools-static 36.0.1](https://github.com/meator/android-tools-static/releases/tag/36.0.1),
source commit `cdadf2ecf68bd3b8c8971b9214cd2bc4e2d77127`.
It is not Google's proprietary SDK archive or a Google-signed release.
Only ADB and its two required Windows USB DLLs are bundled; fastboot and the
other tools in the upstream binary archives are excluded.

The source build's Apache-2.0 license does not replace its dependencies' licenses.
The binaries include **libusb 1.0.29 under LGPL-2.1-or-later**, linked statically,
and code under Apache-2.0, BSD, MIT, Zlib, OpenSSL/ISC and other permissive terms.
Windows' GCC runtime libraries are distributed under GPLv3 with the GCC Runtime
Library Exception. The retained MinGW notices are build-environment references;
the upstream SBOM does not identify an exact MinGW runtime package version.
LZ4 library code uses its BSD license; GPL notices for standalone utilities in the
corresponding source are also retained, without claiming those utilities are in ADB.

Original license, copyright and notice texts are retained without edits under
`licenses/upstream-source/` and `licenses/toolchain/`.
[licenses/license-index.json](licenses/license-index.json) maps each text to its
original source archive member or fixed upstream URL and its SHA256.
The upstream SBOMs in `licenses/sbom/` are retained as build evidence; they do
not replace the license texts or independently prove the absence of other code.

## Complete corresponding source

The library source and the complete source work that uses it, with the original
build files, overlays and patches, are provided with equivalent download access
at the same project release as the skill binary package. This follows LGPL-2.1
sections 6(a) and 6(d); it does not rely on a future written-offer promise:

[ADB 36.0.1 complete source and dependency pack](https://github.com/TubeLiu/tubeliu-scraper/releases/download/v0.2.0/adb-36.0.1-corresponding-source.zip)

The pack contains the upstream pre-patched source archive, all 21 additional
fixed wrap source/overlay inputs, the Windows DLL source, original licenses,
build evidence and instructions for rebuilding ADB with modified libusb.
The original 407 MB archive alone omits most dependency sources and is therefore
not our complete corresponding-source delivery.
The exact source pack size and SHA256 are recorded in
[bundle-manifest.json](bundle-manifest.json).

You may modify this third-party software for your own use and reverse engineer
it to debug those modifications, under its original license terms. Those rights
are not restricted by this skill. See [REBUILD.md](REBUILD.md) and the LGPL text
at [licenses/upstream-source/038-COPYING](licenses/upstream-source/038-COPYING).
When redistributing these binaries, retain these notices and provide equivalent
access to the complete corresponding source; forwarding only the skill ZIP
without a source offer or access does not satisfy this distribution arrangement.

## Verification and platform limits

All downloaded archives were checked against their upstream release asset digest
or the source build's pinned wrap SHA256. Bundled files are byte-for-byte copies
and checked by `bundle-manifest.json` before use. Checksums provide reproducible
integrity; they are not a substitute for a vendor code-signing identity.

Windows x86_64 `adb version` was run successfully: `1.0.41`,
`36.0.1-android-tools-static`. No device command was run for this check.
The macOS universal binary contains x86_64 (minimum macOS 10.15) and arm64
(minimum macOS 11.0) slices and only system dynamic dependencies. Its architecture
was checked statically on Windows; a macOS execution or device test was not performed.
Do not disable Gatekeeper or remove quarantine attributes to bypass a macOS block.

Upstream primary references:

- [Build and redistribution notes at the pinned commit](https://github.com/meator/android-tools-static/blob/cdadf2ecf68bd3b8c8971b9214cd2bc4e2d77127/license_considerations.md).
- [Actual release build workflow](https://github.com/meator/android-tools-static/blob/cdadf2ecf68bd3b8c8971b9214cd2bc4e2d77127/.github/workflows/release.yml).
- [Static dependency build configuration](https://github.com/meator/android-tools-static/blob/cdadf2ecf68bd3b8c8971b9214cd2bc4e2d77127/nativefiles/release_configuration.ini).
- [Windows USB DLL build](https://github.com/meator/AdbWinApi/releases/tag/36.0.1p3).
