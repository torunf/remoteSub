#!/bin/bash
# Builds dist/RemoteSub.app (PyInstaller) and dist/RemoteSub-<version>.pkg, an installer that puts
# RemoteSub and the latest chiaki-ng into /Applications and then opens RemoteSub's setup window.
# Must run on an Apple Silicon Mac (chiaki-ng ships macOS builds for arm64 only).
set -euo pipefail
VERSION="${1:-0.1.0}"
cd "$(dirname "$0")/.."

python3 -m pip install -q -r requirements.txt

python3 -m PyInstaller --noconfirm --clean --windowed --name RemoteSub --paths src \
    --osx-bundle-identifier com.torunf.remotesub \
    --collect-data certifi \n    --exclude-module platform_win --exclude-module oneocr --exclude-module rapidocr \
    --distpath dist --workpath build src/remotesub.py

plist=dist/RemoteSub.app/Contents/Info.plist
plutil -replace CFBundleShortVersionString -string "$VERSION" "$plist"
plutil -replace CFBundleVersion -string "$VERSION" "$plist"
plutil -replace LSMinimumSystemVersion -string "13.0" "$plist"
# RemoteSub quits chiaki-ng through AppleScript before changing its settings.
plutil -replace NSAppleEventsUsageDescription -string \
    "RemoteSub, ayarlarını uygulamak için chiaki-ng'yi kapatır." "$plist"
# Ad-hoc signature: keeps the Screen Recording permission tied to this bundle.
codesign --force --deep --sign - dist/RemoteSub.app

# The latest chiaki-ng for macOS, taken out of its disk image.
# Authenticated when GITHUB_TOKEN is set (CI): shared runner IPs exhaust the anonymous rate limit.
auth=()
[ -n "${GITHUB_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
url=$(curl -fsSL ${auth[@]+"${auth[@]}"} https://api.github.com/repos/streetpea/chiaki-ng/releases/latest | python3 -c '
import json, sys
assets = json.load(sys.stdin)["assets"]
print(next(a["browser_download_url"] for a in assets if a["name"].lower().endswith(".dmg") and "macos" in a["name"].lower()))')
echo "chiaki-ng: $url"
mkdir -p build
curl -fsSL -o build/chiaki-ng.dmg "$url"
mount=$(mktemp -d)
hdiutil attach -nobrowse -readonly -mountpoint "$mount" build/chiaki-ng.dmg
payload=build/pkgroot
rm -rf "$payload"
mkdir -p "$payload"
ditto dist/RemoteSub.app "$payload/RemoteSub.app"
for app in "$mount"/*.app; do
    ditto "$app" "$payload/$(basename "$app")"
done
hdiutil detach "$mount" -quiet

# Install both apps exactly at /Applications (pkgbuild would otherwise follow moved copies).
pkgbuild --analyze --root "$payload" build/components.plist
python3 - build/components.plist <<'EOF'
import plistlib, sys
path = sys.argv[1]
with open(path, "rb") as f:
    components = plistlib.load(f)
for component in components:
    component["BundleIsRelocatable"] = False
with open(path, "wb") as f:
    plistlib.dump(components, f)
EOF

chmod +x installer/macos/scripts/postinstall
pkgbuild --root "$payload" --component-plist build/components.plist \
    --identifier com.torunf.remotesub --version "$VERSION" --install-location /Applications \
    --scripts installer/macos/scripts "dist/RemoteSub-$VERSION.pkg"
ls -la "dist/RemoteSub-$VERSION.pkg"
