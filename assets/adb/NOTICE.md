# Bundled ADB: source and licenses

This skill includes byte-for-byte copies of the open source ADB rebuild from
[android-tools-static 36.0.1](https://github.com/meator/android-tools-static/releases/tag/36.0.1),
commit `cdadf2ecf68bd3b8c8971b9214cd2bc4e2d77127`.
Only ADB and its required Windows USB DLLs are shipped. This is not Google's
proprietary SDK archive or a Google-signed release.

The binaries statically link **libusb 1.0.29, LGPL-2.1-or-later**. Their other
components use Apache-2.0, BSD, MIT, Zlib, OpenSSL/ISC and related terms.
Windows GCC runtime libraries use GPLv3 with the GCC Runtime Library Exception.
The retained MinGW notices are build-environment references; the upstream evidence
does not identify the exact MinGW runtime package version.
Original notices are retained without edits under `licenses/`;
[license-index.json](licenses/license-index.json) records their origins and hashes.
The index covers ADB, its linked dependencies and their build inputs. Unrelated
android-tools notices and full-tool SBOMs are excluded.

## Necessary source and relinking material

[ADB 36.0.1 source and dependency pack](https://github.com/TubeLiu/tubeliu-scraper/releases/download/v0.2.1/adb-36.0.1-minimal-source.zip)
is available at the same release as the skill binary package, with equivalent
download access under LGPL-2.1 sections 6(a) and 6(d). It contains the ADB
application source, linked libraries, generation inputs, fixed dependency archives,
DLL source, licenses and instructions for rebuilding with modified libusb.
It excludes unrelated tools, performance traces, test APKs and large unused test
vectors. Required dependency archives retain their own build material and notices.

The source pack is optional for normal skill installation. Its size and SHA256
are recorded in [bundle-manifest.json](bundle-manifest.json).
Build-control changes for the ADB-only scope are recorded in its patch and
per-file manifest. Source implementation files are preserved from the fixed
upstream version. Compiler toolchains and OS SDKs are not included.
Source hashes and static build-input closure were checked; a new complete C++
rebuild was not performed. See [REBUILD.md](REBUILD.md).

You may modify this third-party software for your own use and reverse engineer
it to debug those modifications. The skill does not restrict these license rights.
When redistributing these binaries, retain the notices and provide equivalent
access to this corresponding source. See the
[LGPL text](licenses/upstream-source/038-COPYING).

## Verification and platform limits

Downloaded inputs were checked against upstream release digests or pinned wrap
hashes. All shipped ADB files are checked against the local manifest before use.
These checks provide integrity, not a vendor signing identity.

Windows x86_64 `adb version` succeeded: `1.0.41`,
`36.0.1-android-tools-static`. No device command was run for that check.
The macOS universal binary contains x86_64 (minimum macOS 10.15) and arm64
(minimum macOS 11.0) slices with system dynamic dependencies. It was checked
statically on Windows; execution and device use on a Mac were not tested.
Do not bypass Gatekeeper or quarantine controls.

Primary build references:

- [Upstream redistribution notes](https://github.com/meator/android-tools-static/blob/cdadf2ecf68bd3b8c8971b9214cd2bc4e2d77127/license_considerations.md).
- [Release build workflow](https://github.com/meator/android-tools-static/blob/cdadf2ecf68bd3b8c8971b9214cd2bc4e2d77127/.github/workflows/release.yml).
- [Static dependency configuration](https://github.com/meator/android-tools-static/blob/cdadf2ecf68bd3b8c8971b9214cd2bc4e2d77127/nativefiles/release_configuration.ini).
- [Windows USB DLL build](https://github.com/meator/AdbWinApi/releases/tag/36.0.1p3).
