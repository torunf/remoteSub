"""Finds a newer RemoteSub release on GitHub and downloads its installer.

Installing is platform specific (platform_*.install_update); macOS verifies the package's
Developer ID signature and notarization before running it.
"""
import json
import os
import tempfile

import netutil

REPO = "torunf/remoteSub"
CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000


def _version_tuple(version):
    return tuple(int(part) for part in version.lstrip("v").split("-")[0].split(".") if part.isdigit())


def find_update(current, asset_suffix):
    """The latest release if it is newer than `current` and has an installer for this platform:
    {"version", "url", "name", "page"}; otherwise None. Development builds never update."""
    if "dev" in current:
        return None
    with netutil.urlopen(f"https://api.github.com/repos/{REPO}/releases/latest", 15) as r:
        release = json.load(r)
    latest = release.get("tag_name", "").lstrip("v")
    if not latest or _version_tuple(latest) <= _version_tuple(current):
        return None
    asset = next((a for a in release.get("assets", []) if a["name"].endswith(asset_suffix)), None)
    if asset is None:
        return None
    return {"version": latest, "url": asset["browser_download_url"], "name": asset["name"],
            "page": release.get("html_url", "")}


def download_update(update, report):
    path = os.path.join(tempfile.mkdtemp(prefix="remotesub-update-"), update["name"])
    netutil.download(update["url"], path, report)
    return path
