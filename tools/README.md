# Local engines

Freerouting is downloaded separately from its official GitHub release.
The engine remains a separate GPL-licensed program; this project invokes its CLI.
The exact engine hash and toolchain versions are recorded in verification artifacts.

Validated local engine: Freerouting 2.0.1, Java 21.
Official asset: https://github.com/freerouting/freerouting/releases/download/v2.0.1/freerouting-2.0.1.jar
Official asset ID: 206469028. Expected size: 66402315 bytes.
Observed SHA-256: d7fd0f63f52e6d74b0fad6715f87ca9f0ffd7109d66b2a584638000270592ecf
All 34481 ZIP entries passed CRC validation after download.
The published JAR's manifest says `unspecified`; the native adapter recognizes this
exact release by its observed hash, not by an arbitrary filename.
The observed hash is a local reproducibility pin, not an upstream signed checksum.

Source for this release: https://github.com/freerouting/freerouting/tree/v2.0.1
License text is kept in FREEROUTING-LICENSE.txt.

## Freerouting 1.9.0 Fallback

Official download used:
https://github.com/freerouting/freerouting/releases/download/v1.9.0/freerouting-1.9.0.jar

Release: https://github.com/freerouting/freerouting/releases/tag/v1.9.0
Source: https://github.com/freerouting/freerouting/tree/v1.9.0
License: GNU GPL version 3, verified in the tagged upstream license:
https://github.com/freerouting/freerouting/blob/v1.9.0/LICENSE

Local file: `freerouting-1.9.0.jar`, downloaded separately; 2.0.1 is retained.
Observed size: 5044336 bytes. All 3465 ZIP entries passed CRC validation.
Observed SHA-256:
`9084a4888937a7f31f857ecc12aa7a37407f51160e4d2892dff9c9bb47ae3102`

The manifest version is `unspecified`; the adapter identifies this exact JAR
by the pinned hash, not its filename. This is a locally observed reproducibility
pin, not an upstream signed checksum. Manifest Build-Revision:
`ca64d4a3f47687df3ecb1b9a4560c3039578962c`.
Main class: `app.freerouting.gui.MainApplication`; entrypoint bytecode requires
Java 17 or newer. The local execution was tested with Java 21, including its
full AWT JRE (not only `openjdk-21-jre-headless`), Xvfb and xauth in Ubuntu WSL.

The isolated `1.9.0-xvfb` profile uses a virtual display, single routing thread,
sequential item selection, `-dct 0`, and `-oit 1`. It requires explicit
`controlled_neckdown: true`: 1.9 uses its native default neckdown, with no verified
CLI disable switch in this adapter. Native minimum-width checks and fresh KiCad
DRC remain mandatory. No visible-window fallback is used. The 2.x headless/API
flags are not passed to this version.

A real four-layer small-board round trip passed with POWER 0.6 mm and LINK
0.25 mm tracks, exact restored placements, zero DRC errors and zero unconnected
items (four library warnings). This is not a general complex-board routing or
manufacturing signoff; every board still requires its own native verification.

The subsequent 160-device same-DSN experiment completed naturally in 603.85 s
with `-mp 10 -mt 1 -is seq -dct 0 -oit 1` and a 900 s outer limit. It saved a
199414-byte SES and passed native import/placement/minimum-width checks.
Fresh schematic-parity-enabled KiCad DRC found 11 unconnected errors and 47
library-footprint-mismatch warnings, with zero schematic parity differences.
This result is blocked for release, not a successful manufacturing pipeline.
Independent evidence: `work/native-validation/legacy-same-dsn-160-20260907`
under the parent workspace; source DSN SHA-256:
`3f043d650e9b4197c4075c977463716dec305ba706128759c335c75ed1924620`.

## Isolated Freerouting 2.4.1 Evaluation

Official release: https://github.com/freerouting/freerouting/releases/tag/v2.4.1
Tagged source: https://github.com/freerouting/freerouting/tree/v2.4.1
JAR: https://github.com/freerouting/freerouting/releases/download/v2.4.1/freerouting-2.4.1.jar
Size: 64076787 bytes. SHA-256 matches the published release asset digest:
`251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9`.
Build revision: `ae3d377740b6ffa744bed1bab26625fe0278fa90`.
Entrypoint: `app.freerouting.Freerouting`, Java 25 minimum.

`jre25-linux` is the independent Eclipse Temurin 25.0.4.1+1 Linux x64 JRE,
not a replacement for the system Java 21. Its original `NOTICE` and `legal/`
files are preserved. Download:
https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.4.1%2B1/OpenJDK25U-jre_x64_linux_hotspot_25.0.4.1_1.tar.gz
Archive size: 61718288 bytes. Official package SHA-256 verified:
`1731a34baadec5479258ea0202e4d5d865d2efeee60cb0c7d7eb056fe96ca219`.
Installed launcher SHA-256:
`97f98bc2e9d0c728b5462d702b0fa7387c2db7b458498bf04318aec4d7ba05cd`.

The separate `toolchain.unified.json` selects this runtime and router. A pinned
download is not evidence of routing quality. Actual tests, failures and supported
profile scope are recorded in `docs/SYSTEM_VALIDATION.zh-CN.md`.
