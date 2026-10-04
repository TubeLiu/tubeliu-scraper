# ADB 36.0.1 source and relinking pack

Download the source pack from the same [v0.2.1 release](https://github.com/TubeLiu/tubeliu-scraper/releases/tag/v0.2.1) as the skill package: `adb-36.0.1-minimal-source.zip`.

This pack accompanies TubeLiu's Scraper's Windows x86_64 and macOS universal2
ADB binaries. It contains the actual ADB application source, its linked library
sources and original code-generation inputs, and the fixed dependency sources
needed to modify libusb and link a new executable. It excludes android-tools'
unrelated fastboot and filesystem tools, performance traces, test APKs,
precompiled profiling tools and large BoringSSL test vectors.

The supplied library source archives remain unchanged and include their own
upstream build material, notices and small tests/examples. This pack does not
claim to remove every test file from those required dependencies.

Source origin: `meator/android-tools-static` 36.0.1, commit
`cdadf2ecf68bd3b8c8971b9214cd2bc4e2d77127`. Unmodified retained files have
their original-member SHA256 in `source-package-manifest.json`. Original in-tree
header aliases have been materialized as identical ordinary files for portable
ZIP extraction. `BUILD-CHANGES.patch` records changes only to the build controls:
the ADB target alone is configured, irrelevant tool targets/completions are
omitted, and the actual shipped libusb wrap is used. Source files and headers
implementing ADB and its libraries are not rewritten.

## Verify and prepare offline inputs

Unzip this pack into a new working directory. With Python 3.10 or newer:

```text
python verify-sources.py
python verify-sources.py --populate-cache
cd adb-source
```

On macOS, use `python3` if that is the installed command. The helper verifies
every retained file and dependency archive, and copies only the checked archives
to `adb-source/subprojects/packagecache`. It neither downloads files nor executes
any application or library source. Existing modified cache entries, symlinks,
junctions and targets outside the chosen source tree are rejected.

Compiler and normal build tools are still required: Meson, Ninja, CMake, Python,
patch/git and a C/C++ compiler. Windows uses MSYS2 UCRT64 with GCC and NASM;
macOS uses Xcode's command line tools. Those normal toolchains and OS SDKs are
not part of this source delivery. Source hashes and the static Meson/CMake file
closure were checked; a new complete C++ rebuild was not executed on this host.

`--wrap-mode=nodownload` uses the included local cache. Forced subprojects prevent
installed system libraries from silently substituting different sources. The
provided route uses libusb 1.0.29 from the wrap, not the different optional
Google snapshot previously present in `vendor/libusb`.
BoringSSL uses `BUILD_TESTING=false` and the original default `FUZZ=false`;
enabling its removed test or fuzz targets is outside this minimal build scope.
The original BoringSSL error-definition tables and object-definition/number
inputs are included alongside their generator programs and generated files.

## Windows x86_64

Run in an MSYS2 UCRT64 shell from `adb-source`:

```sh
meson setup build \
  --native-file nativefiles/release_configuration.ini \
  --native-file nativefiles/release_configuration_fullstatic.ini \
  --native-file nativefiles/release_configuration_standardlayout.ini \
  --wrap-mode=nodownload \
  --force-fallback-for=fmt,lz4,zlib,zstd,libusb,abseil-cpp,AdbWinApi,protobuf,boringssl,google-brotli,gtest \
  -Dbuildonlyadb=true -Duse_bundled_libusb=false -Dgenerate_sbom_data=false
meson compile -C build adb
```

The result is `build/adb.exe`. Its existing Windows USB DLLs and import libraries
are in the original `AdbWinApi-36.0.1p3.zip` input; full DLL source is separately
included as `dependencies/AdbWinApi-36.0.1p3-src.zip`.

## macOS

For an arm64 build host:

```sh
meson setup build-arm64 \
  --native-file nativefiles/release_configuration.ini \
  --native-file nativefiles/release_configuration_standardlayout.ini \
  --native-file "nativefiles/macos arm64.ini" \
  --wrap-mode=nodownload \
  --force-fallback-for=fmt,lz4,zlib,zstd,libusb,abseil-cpp,protobuf,boringssl,google-brotli,gtest \
  -Dbuildonlyadb=true -Duse_bundled_libusb=false -Dgenerate_sbom_data=false
meson compile -C build-arm64 adb
```

For x86_64 using the original cross-build configuration:

```sh
meson setup build-x86_64 \
  --cross-file nativefiles/release_configuration.ini \
  --cross-file nativefiles/release_configuration_standardlayout.ini \
  --cross-file "crossfiles/macos x86_64.ini" \
  --native-file "nativefiles/macos cpp_std fix.ini" \
  --wrap-mode=nodownload \
  --force-fallback-for=fmt,lz4,zlib,zstd,libusb,abseil-cpp,protobuf,boringssl,google-brotli,gtest \
  -Dbuildonlyadb=true -Duse_bundled_libusb=false -Dgenerate_sbom_data=false
meson compile -C build-x86_64 adb
```

After building both on a capable macOS host:

```sh
lipo -create build-x86_64/adb build-arm64/adb -output adb-universal
```

## Modify libusb and relink

First run the Meson setup for your platform to materialize cached subprojects.
Edit `adb-source/subprojects/libusb-1.0.29/`, then run the appropriate
`meson compile -C build adb` command. The build recompiles the changed static
library and relinks the executable from the supplied application source.
Keep the library's notices and record changes when redistributing modifications.
Rebuilding under different compiler/SDK versions need not produce identical
binary bytes. Tests disabled by the original release's `BUILD_TESTING=false`
are not part of this minimal application build.

Original license terms permit modifying this third-party software for your own
use and reverse engineering to debug those modifications. Use the skill's
explicit `--adb` option for a self-built executable; the shipped checksum
manifest does not authorize arbitrary substitutions of bundled bytes. Follow
your OS's normal signing/approval process; no Gatekeeper or quarantine bypass
is provided.
