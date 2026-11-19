#!/bin/bash
# Builds dist/RemoteSub.app (PyInstaller) and dist/RemoteSub-<version>.pkg, an installer that puts
# RemoteSub and the latest chiaki-ng into /Applications and then opens RemoteSub's setup window.
# Must run on an Apple Silicon Mac (chiaki-ng ships macOS builds for arm64 only).
#
# Signing: when MACOS_APP_CERT_P12, MACOS_INSTALLER_CERT_P12, MACOS_CERT_PASSWORD and MACOS_TEAM_ID
# are set (CI secrets), the app and package are signed with Developer ID and, with ASC_API_KEY_P8,
# ASC_API_KEY_ID and ASC_API_ISSUER_ID, notarized and stapled. Without them the build is ad-hoc signed.
set -euo pipefail
VERSION="${1:-0.1.0}"
cd "$(dirname "$0")/.."

SIGN=0
if [ -n "${MACOS_APP_CERT_P12:-}" ] && [ -n "${MACOS_INSTALLER_CERT_P12:-}" ] && [ -n "${MACOS_TEAM_ID:-}" ]; then
    SIGN=1
    APP_IDENTITY="Developer ID Application: Furkan Torun ($MACOS_TEAM_ID)"
    INSTALLER_IDENTITY="Developer ID Installer: Furkan Torun ($MACOS_TEAM_ID)"
fi

setup_keychain() {
    # A throwaway keychain holding the two Developer ID identities for this build only.
    KEYCHAIN="${RUNNER_TEMP:-$(mktemp -d)}/remotesub-signing.keychain-db"
    local keychain_password secrets
    keychain_password=$(uuidgen)
    secrets=$(mktemp -d)
    security create-keychain -p "$keychain_password" "$KEYCHAIN"
    security set-keychain-settings -lut 21600 "$KEYCHAIN"
    security unlock-keychain -p "$keychain_password" "$KEYCHAIN"
    printf '%s' "$MACOS_APP_CERT_P12" | base64 --decode > "$secrets/app.p12"
    printf '%s' "$MACOS_INSTALLER_CERT_P12" | base64 --decode > "$secrets/installer.p12"
    for p12 in "$secrets/app.p12" "$secrets/installer.p12"; do
        security import "$p12" -k "$KEYCHAIN" -P "$MACOS_CERT_PASSWORD" \
            -T /usr/bin/codesign -T /usr/bin/pkgbuild -T /usr/bin/productsign
    done
    # Apple's Developer ID intermediate, so the identities chain to a trusted root.
    curl -fsSL -o "$secrets/DeveloperIDG2CA.cer" https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer
    security import "$secrets/DeveloperIDG2CA.cer" -k "$KEYCHAIN" || true
    rm -rf "$secrets"
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$KEYCHAIN" >/dev/null
    security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')
    security find-identity -v -p codesigning "$KEYCHAIN"
}

sign_app() {  # sign_app <app> <entitlements>
    codesign --force --deep --options runtime --timestamp --keychain "$KEYCHAIN" \
        --entitlements "$2" --sign "$APP_IDENTITY" "$1"
    codesign --verify --deep --strict --verbose=2 "$1"
}

[ "$SIGN" = 1 ] && setup_keychain

python3 -m pip install -q -r requirements.txt
# The version the app reports and compares against GitHub releases for updates.
printf 'VERSION = "%s"\n' "$VERSION" > src/_version.py

python3 -m PyInstaller --noconfirm --clean --windowed --name RemoteSub --paths src \
    --osx-bundle-identifier com.torunf.remotesub \
    --collect-data certifi --collect-data sv_ttk --add-data src/fonts:fonts \
    --exclude-module platform_win --exclude-module oneocr --exclude-module rapidocr \
    --distpath dist --workpath build src/remotesub.py

plist=dist/RemoteSub.app/Contents/Info.plist
plutil -replace CFBundleShortVersionString -string "$VERSION" "$plist"
plutil -replace CFBundleVersion -string "$VERSION" "$plist"
plutil -replace LSMinimumSystemVersion -string "13.0" "$plist"
# RemoteSub quits chiaki-ng through AppleScript before changing its settings.
plutil -replace NSAppleEventsUsageDescription -string \
    "RemoteSub, ayarlarını uygulamak için chiaki-ng'yi kapatır." "$plist"
if [ "$SIGN" = 1 ]; then
    sign_app dist/RemoteSub.app installer/macos/entitlements.plist
else
    # Ad-hoc signature: keeps the Screen Recording permission tied to this bundle.
    codesign --force --deep --sign - dist/RemoteSub.app
fi

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

if [ "$SIGN" = 1 ]; then
    for app in "$payload"/*.app; do
        [ "$(basename "$app")" = RemoteSub.app ] && continue
        echo "--- upstream signature of $(basename "$app"):"
        codesign -dv --verbose=2 "$app" 2>&1 | grep -E "Authority|TeamIdentifier|flags|Timestamp" || true
        # Notarization requires every executable in the package to carry a Developer ID
        # signature with the hardened runtime. Keep chiaki-ng's own when it has one.
        if codesign -dv --verbose=2 "$app" 2>&1 | grep -q "Authority=Developer ID Application" \
           && codesign -dv "$app" 2>&1 | grep -q "flags=.*runtime"; then
            echo "keeping upstream Developer ID signature"
        else
            echo "re-signing with $APP_IDENTITY"
            sign_app "$app" installer/macos/chiaki-entitlements.plist
        fi
    done
fi

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

pkg="dist/RemoteSub-$VERSION.pkg"
sign_args=()
[ "$SIGN" = 1 ] && sign_args=(--sign "$INSTALLER_IDENTITY" --keychain "$KEYCHAIN" --timestamp)
chmod +x installer/macos/scripts/postinstall
pkgbuild --root "$payload" --component-plist build/components.plist \
    --identifier com.torunf.remotesub --version "$VERSION" --install-location /Applications \
    --scripts installer/macos/scripts ${sign_args[@]+"${sign_args[@]}"} "$pkg"

if [ "$SIGN" = 1 ] && [ -n "${ASC_API_KEY_P8:-}" ]; then
    key_file=$(mktemp -d)/AuthKey.p8
    printf '%s' "$ASC_API_KEY_P8" | base64 --decode > "$key_file"
    echo "Submitting to Apple for notarization…"
    if ! xcrun notarytool submit "$pkg" --key "$key_file" --key-id "$ASC_API_KEY_ID" \
            --issuer "$ASC_API_ISSUER_ID" --wait --timeout 45m --output-format json > build/notary.json; then
        cat build/notary.json || true
    fi
    cat build/notary.json
    submission=$(python3 -c 'import json; print(json.load(open("build/notary.json")).get("id", ""))')
    status=$(python3 -c 'import json; print(json.load(open("build/notary.json")).get("status", ""))')
    if [ "$status" != "Accepted" ]; then
        [ -n "$submission" ] && xcrun notarytool log "$submission" --key "$key_file" \
            --key-id "$ASC_API_KEY_ID" --issuer "$ASC_API_ISSUER_ID" || true
        rm -f "$key_file"
        echo "Notarization failed: $status" >&2
        exit 1
    fi
    rm -f "$key_file"
    xcrun stapler staple "$pkg"
    spctl --assess --type install --verbose=2 "$pkg" || true
fi
ls -la "$pkg"
