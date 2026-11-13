"""Download helpers shared by the platform setup code."""
import json
import urllib.request


def download(url, path, report):
    with urllib.request.urlopen(url, timeout=60) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                report(f"İndiriliyor… {done >> 20} / {total >> 20} MB", done / total)


def github_latest_asset(repo, match):
    """(download URL, file name) of the first asset of the latest release whose lowercased name matches."""
    with urllib.request.urlopen(f"https://api.github.com/repos/{repo}/releases/latest", timeout=30) as r:
        release = json.load(r)
    for asset in release.get("assets", []):
        if match(asset["name"].lower()):
            return asset["browser_download_url"], asset["name"]
    return None, None
