# Rebuild or relink bundled ADB with modified libusb

Download `adb-36.0.1-corresponding-source.zip` from the same
[v0.2.0 release](https://github.com/TubeLiu/tubeliu-scraper/releases/tag/v0.2.0)
as the skill package, and unzip it into a new working directory.
The pack includes the exact upstream 36.0.1 pre-patched source archive, missing
dependency source and Meson overlay archives, AdbWinApi 36.0.1p3 source, the
original notices and a complete SHA256 input manifest. No additional application
or libusb source download is needed.

A compiler and normal build tools are still required. The upstream binary build
uses Meson, Ninja, CMake, Python, patch/git and a C/C++ compiler. Windows uses
MSYS2 UCRT64, GCC and NASM; macOS uses Xcode's command line tools. These normal
toolchains and the operating system SDK are not in this source delivery.
See the included original `.github/workflows/release.yml` for the exact upstream
build configuration. We have verified all source inputs and recipes, but have
not run a new complete C++ rebuild on Windows or macOS in this task.

## Verify and populate the offline source cache

From the unpacked source-pack directory, with Python 3.10 or later:

```text
python verify-sources.py
tar -xzf upstream/android-tools-static-36.0.1-src.tar.gz
python verify-sources.py --populate-cache android-tools-static-36.0.1-src
cd android-tools-static-36.0.1-src
```

On macOS use `python3` if that is the installed Python command. On Windows use
an MSYS2 UCRT64 shell for the subsequent Meson and compiler commands. Cache
population copies only the hash-verified archives to `subprojects/packagecache`;
it neither fetches network data nor executes source code.

The source archive already contains the patched BoringSSL source. Other required
sources and their overlays are in the populated cache, including libusb 1.0.29.
The release uses `use_bundled_libusb=false`; its library is the libusb wrap,
not the different Google snapshot at `vendor/libusb`.

Meson reads and checks local cache files even in `nodownload` mode. The following
commands also force the provided subprojects so an installed system library
does not silently replace the pinned sources. This behavior is documented by
[Meson's wrap manual](https://mesonbuild.com/Wrap-dependency-system-manual.html)
and [subproject options](https://mesonbuild.com/Subprojects.html#command-line-options).

## Windows x86_64

```sh
meson setup build \
  --native-file nativefiles/release_configuration.ini \
  --native-file nativefiles/release_configuration_fullstatic.ini \
  --native-file nativefiles/release_configuration_standardlayout.ini \
  --wrap-mode=nodownload \
  --force-fallback-for=fmt,lz4,zlib,zstd,libusb,abseil-cpp,AdbWinApi,protobuf,boringssl,google-brotli,gtest,pcre2 \
  -Duse_bundled_libusb=false
meson compile -C build adb
```

The result is `build/adb.exe`. The existing AdbWinApi/AdbWinUsbApi DLLs in the
provided AdbWinApi input can be used alongside it. The separate full DLL source
is `dependencies/AdbWinApi-36.0.1p3-src.zip`, with its original rebuild files.

## macOS

For an arm64 host:

```sh
meson setup build-arm64 \
  --native-file nativefiles/release_configuration.ini \
  --native-file nativefiles/release_configuration_standardlayout.ini \
  --native-file "nativefiles/macos arm64.ini" \
  --wrap-mode=nodownload \
  --force-fallback-for=fmt,lz4,zlib,zstd,libusb,abseil-cpp,protobuf,boringssl,google-brotli,gtest,pcre2 \
  -Duse_bundled_libusb=false
meson compile -C build-arm64 adb
```

For x86_64, use the original release's cross-build configuration:

```sh
meson setup build-x86_64 \
  --cross-file nativefiles/release_configuration.ini \
  --cross-file nativefiles/release_configuration_standardlayout.ini \
  --cross-file "crossfiles/macos x86_64.ini" \
  --native-file "nativefiles/macos cpp_std fix.ini" \
  --wrap-mode=nodownload \
  --force-fallback-for=fmt,lz4,zlib,zstd,libusb,abseil-cpp,protobuf,boringssl,google-brotli,gtest,pcre2 \
  -Duse_bundled_libusb=false
meson compile -C build-x86_64 adb
```

On a capable macOS build host, combine the two results if a universal executable
is wanted:

```sh
lipo -create build-x86_64/adb build-arm64/adb -output adb-universal
```

## Change libusb and relink

Run the first Meson setup for your platform to materialize its cached subprojects.
Modify files in `subprojects/libusb-1.0.29/` and run `meson compile -C build adb`
(or the appropriate `build-arm64`/`build-x86_64` directory). Meson/Ninja rebuild
the affected static library and link a new executable from the included complete
ADB application source. Do not replace `vendor/libusb`; it is not the library
used by this release configuration.

Retain the original license texts when redistributing modified versions and
record your changes. Rebuilding with the same source does not imply identical
binary bytes across different compiler, SDK or build-tool versions.

A user-built executable may be used through the skill's explicit `--adb` option;
the built-in checksum manifest deliberately does not authorize arbitrary
replacements of the shipped executable. Follow your operating system's normal
approval and signing process; no Gatekeeper or quarantine bypass is provided.
