#!/bin/bash
# Builds the Android TV app: dist/RemoteSub-TV-<version>.apk (debug-signed unless a release keystore
# is configured in android/local.properties: remotesubKeystore, remotesubKeystorePW,
# remotesubKeyAlias, remotesubKeyPW).
#
# Needs: Android SDK (ANDROID_HOME or ~/Library/Android/sdk) with NDK 28.2.13676358 and CMake 3.22.1,
# JDK 17, protoc (brew install protobuf) and python3.
set -euo pipefail
VERSION="${1:-dev}"
cd "$(dirname "$0")/../android"

git -C .. submodule update --init android/chiaki-ng
git -C chiaki-ng submodule update --init --depth 1 \
    third-party/nanopb third-party/jerasure third-party/gf-complete third-party/curl

command -v protoc >/dev/null || { echo "protoc is missing: brew install protobuf" >&2; exit 1; }
# chiaki-ng's nanopb generator needs a Python with protobuf.
if [ ! -x .build-venv/bin/python3 ]; then
    python3 -m venv .build-venv
    .build-venv/bin/pip install -q protobuf  # must not be older than protoc's generated code
fi

sdk="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
[ -f local.properties ] || echo "sdk.dir=$sdk" > local.properties

if grep -q '^remotesubKeystore=' local.properties; then
    ./gradlew assembleRelease
    apk=app/build/outputs/apk/release/app-release.apk
else
    ./gradlew assembleDebug
    apk=app/build/outputs/apk/debug/app-debug.apk
fi
mkdir -p ../dist
cp "$apk" "../dist/RemoteSub-TV-$VERSION.apk"
ls -la "../dist/RemoteSub-TV-$VERSION.apk"
