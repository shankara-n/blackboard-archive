#!/usr/bin/env python3
"""
Streaming downloader for Blackboard files that exceed Playwright's body() limit (~512MB).

Playwright's APIRequestContext.body() loads the whole response into a JS string and
fails on responses larger than V8's max string size. This helper reuses your saved
Playwright session cookies but does the actual download via `requests` with
stream=True, so the file is written to disk in 1 MB chunks.

Use after the main scraper logs lines like:
    download error https://blackboard.school.edu/webapps/assignment/download?...
    APIRequestContext.get: Timeout 30000ms exceeded.

Run:
    .venv/bin/python bigfile_download.py urls.txt
        — one URL per line (lines starting with # are ignored)

    .venv/bin/python bigfile_download.py - <<EOF
    https://blackboard.school.edu/webapps/assignment/download?...
    https://blackboard.school.edu/bbcswebdav/pid-...
    EOF

By default, files go to ./bigfiles/<filename>. Pass --out <dir> to override.
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BB_BASE", "https://blackboard.nec.edu").rstrip("/")
PROFILE = Path(__file__).resolve().parent / ".pw_profile"

SAFE = re.compile(r"[^A-Za-z0-9._\- ]+")


def safe(name: str) -> str:
    return SAFE.sub("_", name).strip().strip(".") or "file.bin"


def filename_from_url(url: str) -> str:
    # Prefer ?fileName= query param if present
    m = re.search(r"[?&]fileName=([^&]+)", url)
    if m:
        return safe(unquote(m.group(1)))
    # Otherwise last path segment
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] or "file.bin"
    return safe(unquote(name))


def filename_from_response(resp: requests.Response, url: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="([^"]+)"', cd)
    if m:
        return safe(unquote(m.group(1)))
    return filename_from_url(url)


def get_session_cookies() -> dict:
    """Open the persistent Playwright profile, verify auth, return cookies dict."""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
        )
        api = ctx.request
        r = api.get(f"{BASE}/learn/api/public/v1/users/me", timeout=15000)
        if not r.ok or '"userName"' not in r.text():
            print("Not authenticated — sign in to the Chromium window that just opened.")
            page = ctx.new_page()
            page.goto(f"{BASE}/", wait_until="domcontentloaded")
            deadline = time.time() + 600
            while time.time() < deadline:
                time.sleep(3)
                r = api.get(f"{BASE}/learn/api/public/v1/users/me")
                if r.ok and '"userName"' in r.text():
                    print("Authenticated.")
                    break
            else:
                raise SystemExit("Login timeout after 10 min.")
        # Restrict to the configured host's cookies only
        host = urlparse(BASE).hostname or ""
        cookies = {c["name"]: c["value"] for c in ctx.cookies() if host.endswith(c["domain"].lstrip("."))}
        ctx.close()
    return cookies


def load_urls(source: str) -> list[str]:
    if source == "-":
        text = sys.stdin.read()
    else:
        text = Path(source).read_text()
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("urls_file", help="Path to a file of URLs (one per line), or - for stdin")
    ap.add_argument("--out", default="bigfiles", help="Destination directory (default: ./bigfiles)")
    args = ap.parse_args()

    urls = load_urls(args.urls_file)
    if not urls:
        print("No URLs to download.")
        return 0

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cookies = get_session_cookies()
    print(f"Got {len(cookies)} cookies for {urlparse(BASE).hostname}.")
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )

    n_ok = n_fail = 0
    for url in urls:
        print(f"\n[ {url[:120]}{'…' if len(url) > 120 else ''} ]")
        try:
            with session.get(url, stream=True, timeout=(30, 1800)) as resp:
                if not resp.ok:
                    print(f"  HTTP {resp.status_code}")
                    n_fail += 1
                    continue
                fname = filename_from_response(resp, url)
                dest = out_dir / fname
                if dest.exists() and dest.stat().st_size > 100_000:
                    print(f"  already exists: {dest} ({dest.stat().st_size:,} bytes)")
                    continue
                total = int(resp.headers.get("Content-Length", "0"))
                start = time.time()
                written = 0
                last_print = time.time()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        written += len(chunk)
                        now = time.time()
                        if now - last_print > 5:
                            pct = (written / total * 100) if total else 0
                            mbps = written / max(now - start, 1e-3) / 1024 / 1024
                            print(f"    {written/1024/1024:.1f} MB ({pct:.0f}%) @ {mbps:.1f} MB/s")
                            last_print = now
                dur = time.time() - start
                print(f"  ✓ {dest.name}  {written:,} bytes in {dur:.1f}s "
                      f"({written/dur/1024/1024:.1f} MB/s)")
                n_ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {e!r}")
            n_fail += 1

    print(f"\nDone: {n_ok} downloaded, {n_fail} failed.")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
