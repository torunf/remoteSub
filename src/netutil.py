"""Download helpers shared by the platform setup code."""
import json
import ssl
import urllib.request

from i18n import _


def _ssl_context():
    # The python.org Python bundled into the macOS app has no CA certificates of its own, so
    # HTTPS failed with CERTIFICATE_VERIFY_FAILED. certifi's bundle works on every platform.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CONTEXT = _ssl_context()


def urlopen(url, timeout):
    return urllib.request.urlopen(url, timeout=timeout, context=SSL_CONTEXT)


def download(url, path, report):
    with urlopen(url, 60) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                report(_("Downloading… {done} / {total} MB", done=done >> 20, total=total >> 20), done / total)


def github_latest_asset(repo, match):
    """(download URL, file name) of the first asset of the latest release whose lowercased name matches."""
    with urlopen(f"https://api.github.com/repos/{repo}/releases/latest", 30) as r:
        release = json.load(r)
    for asset in release.get("assets", []):
        if match(asset["name"].lower()):
            return asset["browser_download_url"], asset["name"]
    return None, None
