#!/usr/bin/env python3
"""
NEC Blackboard bulk archiver.

Strategy (based on prior reconnaissance):
- NEC's public REST API is gated for students except /users/me + /users/me/courses.
- Courses render as Blackboard Original UI inside the Ultra chrome.
- The Original UI endpoints under /webapps/blackboard/... work fine with session cookies.
- File downloads live at /bbcswebdav/... and preserve original filenames in Content-Disposition.

Run:
  python3 nec_archive.py            # full run
  NEC_DRY_RUN=1 python3 nec_archive.py   # enumerate only, no downloads
  NEC_COURSES=_99999_1,_88888_1 python3 nec_archive.py  # restrict to specific pk1 ids
  NEC_HEADLESS=1 python3 nec_archive.py  # headless (only after first successful login)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs, quote

from bs4 import BeautifulSoup
from playwright.sync_api import (
    sync_playwright,
    BrowserContext,
    Page,
    APIRequestContext,
    Response,
    TimeoutError as PWTimeout,
)

BASE = os.environ.get("BB_BASE", "https://blackboard.nec.edu").rstrip("/")
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
STATE = ROOT / "nec_state.json"
DRY = os.environ.get("NEC_DRY_RUN") == "1"
HEADLESS = os.environ.get("NEC_HEADLESS") == "1"
ONLY = {c.strip() for c in os.environ.get("NEC_COURSES", "").split(",") if c.strip()}
VERBOSE = os.environ.get("NEC_VERBOSE") == "1"

SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ]+")


def safe(name: str, maxlen: int = 120) -> str:
    name = SAFE_NAME.sub("_", name).strip().strip(".")
    return (name or "untitled")[:maxlen]


def log(msg: str) -> None:
    print(msg, flush=True)


def vlog(msg: str) -> None:
    if VERBOSE:
        print(f"  · {msg}", flush=True)


@dataclass
class Course:
    course_id: str          # external id e.g. "ENGL-101-A_FA24"
    pk1: str                # internal pk1, e.g. "_99999_1"
    name: str
    role: str = "Student"
    available: str = "Yes"
    out_dir: Path = field(init=False)

    def __post_init__(self):
        self.out_dir = OUT / safe(f"{self.course_id}__{self.name}")


# ---------------------------------------------------------------------------
# Auth / context bootstrap
# ---------------------------------------------------------------------------

def ensure_login(context: BrowserContext) -> None:
    """Open a page, navigate to base, poll until authenticated."""
    page = context.new_page()
    log("Opening Blackboard to verify session...")
    api = context.request
    r = api.get(f"{BASE}/learn/api/public/v1/users/me")
    if r.ok and '"userName"' in r.text():
        log("  ✓ Already authenticated.")
        page.close()
        return

    log("")
    log("=" * 70)
    log("LOGIN REQUIRED")
    log("=" * 70)
    log("A Chromium window is open. Sign in to NEC Blackboard (Microsoft SSO + MFA).")
    log("Script will auto-continue once it detects a valid session. Up to 10 min.")
    log("=" * 70)
    log("")
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    deadline = time.time() + 600  # 10 minutes
    last_log = 0.0
    while time.time() < deadline:
        time.sleep(3)
        try:
            r = api.get(f"{BASE}/learn/api/public/v1/users/me")
            if r.ok and '"userName"' in r.text():
                log("  ✓ Authenticated.")
                page.close()
                return
        except Exception:
            pass
        now = time.time()
        if now - last_log > 20:
            log("  …waiting for login…")
            last_log = now
    raise SystemExit("Login timed out after 10 minutes. Aborting.")


# ---------------------------------------------------------------------------
# Course enumeration
# ---------------------------------------------------------------------------

def list_courses(api: APIRequestContext) -> list[Course]:
    log("Enumerating courses...")
    r = api.get(f"{BASE}/learn/api/public/v1/users/me/courses?limit=200&expand=course")
    if not r.ok:
        raise SystemExit(f"course list failed: {r.status} {r.text()[:300]}")
    data = r.json()
    out: list[Course] = []
    for m in data.get("results", []):
        c = m.get("course", {}) or {}
        avail = (c.get("availability", {}) or {}).get("available", "Unknown")
        pk1 = c.get("id") or m.get("courseId") or ""
        ext = c.get("courseId") or pk1
        name = c.get("displayName") or c.get("name") or ext
        role = m.get("courseRoleId") or "Student"
        if not pk1:
            continue
        if ONLY and pk1 not in ONLY and ext not in ONLY:
            continue
        out.append(Course(course_id=ext, pk1=pk1, name=name, role=role, available=avail))
    out.sort(key=lambda c: c.name.lower())
    log(f"  ✓ {len(out)} courses")
    for c in out:
        log(f"    - {c.pk1}  [{c.available}]  {c.name}")
    return out


# ---------------------------------------------------------------------------
# Per-course scraping
# ---------------------------------------------------------------------------

LINK_RX = re.compile(r"""href=['"]([^'"]+)['"]""", re.I)


def fetch_text(api: APIRequestContext, url: str) -> tuple[int, str, dict]:
    try:
        r = api.get(url, headers={"Accept": "text/html,*/*"})
        return r.status, r.text(), dict(r.headers)
    except Exception as e:  # noqa: BLE001
        vlog(f"fetch_text error {url}: {e}")
        return 0, "", {}


def save(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8", errors="replace")
    else:
        path.write_bytes(content)


def get_course_menu(api: APIRequestContext, course: Course) -> list[dict]:
    """Fetch the Original-view left menu and return a list of {title, url} entries."""
    url = f"{BASE}/webapps/blackboard/execute/courseMain?course_id={quote(course.pk1)}"
    status, html, _ = fetch_text(api, url)
    if status != 200 or not html:
        vlog(f"courseMain {course.pk1} -> {status}")
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    # Standard selector
    for ul_id in ("courseMenuPalette_contents", "courseMenuPalette_paletteContents"):
        ul = soup.find("ul", id=ul_id)
        if not ul:
            continue
        for a in ul.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            href = a["href"]
            if not title:
                continue
            items.append({"title": title, "href": urljoin(BASE, href)})
        if items:
            break
    # Fallback: any sidebar anchor pointing at listContent.jsp / announcement / forum / mygrades
    if not items:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(k in href for k in (
                "listContent.jsp", "execute/announcement", "discussionboard",
                "mygrades", "displayLearningUnit", "blankPage", "tools/staff_information",
            )):
                title = a.get_text(" ", strip=True) or href
                items.append({"title": title, "href": urljoin(BASE, href)})
    # Dedupe preserving order
    seen = set()
    out = []
    for it in items:
        key = (it["title"], it["href"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch_announcements(api: APIRequestContext, course: Course, dest: Path) -> int:
    url = (
        f"{BASE}/webapps/blackboard/execute/announcement?method=search"
        f"&context=course&course_id={quote(course.pk1)}"
        f"&viewChoice=all&searchChoice=all"
    )
    status, html, _ = fetch_text(api, url)
    if status != 200:
        vlog(f"announcements {course.pk1} -> {status}")
        return 0
    save(dest / "announcements.html", html)
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("li.clearfix") or soup.select(".announcementInfo") or []
    text_lines = []
    for li in items:
        title = li.find(["h3", "h4"])
        title_text = title.get_text(" ", strip=True) if title else ""
        body = li.get_text("\n", strip=True)
        text_lines.append(f"## {title_text}\n\n{body}\n")
    if text_lines:
        save(dest / "announcements.md", "\n---\n\n".join(text_lines))
    return len(items)


MYGRADES_FRAME_RX = re.compile(r"mygrades\.loadContentFrame\('([^']+)'")


def fetch_grades(api: APIRequestContext, course: Course, dest: Path) -> bool:
    candidates = [
        f"{BASE}/webapps/bb-mygrades-bb_bb60/myGrades?course_id={quote(course.pk1)}&stream_name=mygrades",
        f"{BASE}/webapps/bb-mygrades-BBLEARN/myGrades?course_id={quote(course.pk1)}",
        f"{BASE}/webapps/blackboard/content/listContent.jsp?course_id={quote(course.pk1)}"
        f"&content_id=_my_grades_",
    ]
    grades_html = ""
    for url in candidates:
        status, html, _ = fetch_text(api, url)
        if status == 200 and html and "grade" in html.lower():
            grades_html = html
            break
    if not grades_html:
        return False
    save(dest / "grades.html", grades_html)
    soup = BeautifulSoup(grades_html, "html.parser")
    rows = soup.select(".sortable_item_row, tr") or []
    lines = []
    for row in rows:
        txt = row.get_text(" | ", strip=True)
        if txt:
            lines.append(txt)
    if lines:
        save(dest / "grades.txt", "\n".join(lines))
    # Drill into each gradebook entry — submissions, discussion posts, etc.
    fetch_submissions(api, course, dest, grades_html)
    return True


def fetch_submissions(api: APIRequestContext, course: Course, dest: Path, grades_html: str) -> None:
    """Follow mygrades.loadContentFrame URLs to capture assignment submissions,
    graded discussion posts, and instructor feedback."""
    sub_dir = dest / "submissions"
    seen_paths = set()
    soup = BeautifulSoup(grades_html, "html.parser")
    # Map outcome rows: try to find the assignment name next to each link.
    rows = soup.select(".sortable_item_row")
    n_assign = 0
    n_disc = 0
    for row in rows:
        a = row.select_one("a[onclick*='loadContentFrame']")
        if not a:
            continue
        onclick = a.get("onclick", "")
        m = MYGRADES_FRAME_RX.search(onclick)
        if not m:
            continue
        frame_path = m.group(1)
        if frame_path in seen_paths:
            continue
        seen_paths.add(frame_path)
        # Title from row
        title_el = row.select_one(".cell.gradable") or a
        title = safe(title_el.get_text(" ", strip=True))[:80] or "item"
        kind = "assignment" if "/uploadAssignment" in frame_path else (
            "discussion_post" if "/discussiongrades" in frame_path else "other"
        )
        if kind == "assignment":
            n_assign += 1
            prefix = f"A{n_assign:02d}"
        elif kind == "discussion_post":
            n_disc += 1
            prefix = f"D{n_disc:02d}"
        else:
            prefix = "X"
        item_dir = sub_dir / f"{prefix}_{title}"
        full_url = urljoin(BASE, frame_path)
        status, html, _ = fetch_text(api, full_url)
        if status != 200 or not html:
            vlog(f"submission {full_url} -> {status}")
            continue
        save(item_dir / "_view.html", html)
        # Plain-text extract (includes student post body + feedback)
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        save(item_dir / "_view.txt", text)
        # For assignments: parse the submission history and download every file
        if kind == "assignment":
            isoup = BeautifulSoup(html, "html.parser")
            # Submitted files appear as anchors pointing at /bbcswebdav/ or
            # /webapps/assignment/download?... — collect both.
            files = 0
            for link in isoup.find_all("a", href=True):
                href = link["href"]
                if is_attachment_link(href) or "/assignment/download" in href:
                    p = download_file(api, urljoin(BASE, href), item_dir,
                                      hint=link.get_text(" ", strip=True))
                    if p:
                        files += 1
            vlog(f"  submission {title}: {files} file(s)")
        elif kind == "discussion_post":
            # Discussion grade pages also embed attachments and the actual post body
            isoup = BeautifulSoup(html, "html.parser")
            for link in isoup.find_all("a", href=True):
                href = link["href"]
                if is_attachment_link(href):
                    download_file(api, urljoin(BASE, href), item_dir,
                                  hint=link.get_text(" ", strip=True))
    if n_assign or n_disc:
        log(f"  submissions: {n_assign} assignments, {n_disc} discussion grades")


# Discussion board ----------------------------------------------------------

def fetch_discussions(api: APIRequestContext, course: Course, dest: Path) -> int:
    """Find discussion forums in the menu, capture thread lists + Collect views."""
    # Try several endpoints — different Blackboard versions use different URLs.
    candidates = [
        f"{BASE}/webapps/discussionboard/do/conference?action=list_forums"
        f"&course_id={quote(course.pk1)}&type=Course",
        f"{BASE}/webapps/discussionboard/do/conference?action=list_forums"
        f"&course_id={quote(course.pk1)}",
        f"{BASE}/webapps/blackboard/content/launchLink.jsp"
        f"?course_id={quote(course.pk1)}&tool_id=_144_1&tool_type=TOOL&mode=view&mode=reset",
    ]
    html = ""
    final_url = ""
    for list_url in candidates:
        status, body, headers = fetch_text(api, list_url)
        if status == 200 and body and ("forum" in body.lower() or "discussion" in body.lower()):
            html = body
            final_url = list_url
            break
    if not html:
        vlog(f"forums {course.pk1} -> no working endpoint")
        return 0
    save(dest / "discussions" / "_forums.html", html)
    vlog(f"forums {course.pk1} via {final_url}")
    soup = BeautifulSoup(html, "html.parser")
    forum_count = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "forum_id=" not in href:
            continue
        full = urljoin(BASE, href)
        q = parse_qs(urlparse(full).query)
        forum_id = (q.get("forum_id") or [""])[0]
        conf_id = (q.get("conf_id") or [""])[0]
        if not forum_id:
            continue
        title = safe(a.get_text(" ", strip=True) or forum_id)
        forum_dir = dest / "discussions" / f"{forum_count:02d}_{title}"
        # Thread list
        status2, html2, _ = fetch_text(api, full)
        if status2 == 200:
            save(forum_dir / "_threads.html", html2)
        # Collect-all view: dumps every post in the forum on one page
        if conf_id:
            collect = (
                f"{BASE}/webapps/discussionboard/do/message?action=collect"
                f"&forum_id={quote(forum_id)}&conf_id={quote(conf_id)}"
                f"&course_id={quote(course.pk1)}"
            )
            status3, html3, _ = fetch_text(api, collect)
            if status3 == 200:
                save(forum_dir / "all_posts.html", html3)
                txt = BeautifulSoup(html3, "html.parser").get_text("\n", strip=True)
                save(forum_dir / "all_posts.txt", txt)
        forum_count += 1
    return forum_count


# Content tree / file downloads --------------------------------------------

ATTACHMENT_HOSTS = ("/bbcswebdav/", "/courses/1/")  # second one rare but seen


def is_attachment_link(href: str) -> bool:
    return any(p in href for p in ATTACHMENT_HOSTS)


def get_filename_from_response(headers: dict, fallback_url: str) -> str:
    cd = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="([^"]+)"', cd)
    if m:
        return m.group(1)
    m = re.search(r"filename=([^;]+)", cd)
    if m:
        return m.group(1).strip().strip('"')
    # Fallback: last path component
    name = os.path.basename(urlparse(fallback_url).path) or "file.bin"
    return name


def download_file(api: APIRequestContext, url: str, dest_dir: Path, hint: str = "") -> Path | None:
    try:
        r = api.get(url)
    except Exception as e:  # noqa: BLE001
        vlog(f"download error {url}: {e}")
        return None
    if not r.ok:
        vlog(f"download {url} -> {r.status}")
        return None
    headers = dict(r.headers)
    name = get_filename_from_response(headers, url)
    if hint and "." in hint and not name.lower().endswith(Path(hint).suffix.lower()):
        # Prefer the link text hint if it carries an extension
        name = hint
    name = safe(name)
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 0:
        vlog(f"skip (exists) {dest.name}")
        return dest
    body = r.body()
    # Sometimes Blackboard returns an HTML interstitial when the link redirects
    ctype = (headers.get("content-type") or "").lower()
    if "text/html" in ctype and not name.lower().endswith((".html", ".htm")):
        vlog(f"interstitial HTML for {url} -> save with .html for review")
        dest = dest.with_suffix(dest.suffix + ".interstitial.html")
    save(dest, body)
    return dest


def walk_content(
    api: APIRequestContext,
    course: Course,
    content_url: str,
    dest_dir: Path,
    crumbs: list[str],
    seen: set[str],
    depth: int = 0,
) -> tuple[int, int]:
    """Recursively walk a listContent.jsp page. Returns (folders, files)."""
    if depth > 8:
        return 0, 0
    if content_url in seen:
        return 0, 0
    seen.add(content_url)
    status, html, _ = fetch_text(api, content_url)
    if status != 200:
        vlog(f"content {content_url} -> {status}")
        return 0, 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    save(dest_dir / "_index.html", html)
    soup = BeautifulSoup(html, "html.parser")
    files = 0
    folders = 0

    # Each content item is typically a <li class="liItem ...">
    items = soup.select("li.liItem, li.read")
    if not items:
        items = soup.select("ul#content_listContainer > li")

    for li in items:
        head = li.find(["h3", "h4"])
        title = (head.get_text(" ", strip=True) if head else "").strip() or "item"
        # Save per-item textual description
        body_div = li.find("div", class_="details") or li.find("div", class_="vtbegenerated")
        item_text_parts = [title]
        if body_div:
            item_text_parts.append(body_div.get_text("\n", strip=True))
        else:
            item_text_parts.append(li.get_text("\n", strip=True))
        item_text = "\n\n".join(p for p in item_text_parts if p)

        # Determine type by icon alt text if present
        icon_img = li.find("img", class_="item_icon")
        icon_alt = (icon_img.get("alt") or "") if icon_img else ""
        is_folder = icon_alt in ("Content Folder", "Learning Module", "Lesson Plan") or li.select_one("a[href*='listContent.jsp']")

        # Collect attachments first
        attached_any = False
        for a in li.find_all("a", href=True):
            href = a["href"]
            full = urljoin(BASE, href)
            if is_attachment_link(href):
                hint = a.get_text(" ", strip=True)
                if DRY:
                    log(f"      DRY  file  {hint or full}")
                    files += 1
                else:
                    p = download_file(api, full, dest_dir, hint=hint)
                    if p:
                        files += 1
                attached_any = True

        # Save item description as its own file
        item_slug = safe(title) or "item"
        desc_path = dest_dir / f"{item_slug}.txt"
        if item_text.strip():
            save(desc_path, item_text)

        # Recurse into subfolders
        for a in li.find_all("a", href=True):
            href = a["href"]
            if "listContent.jsp" in href and f"course_id={course.pk1}" in href:
                full = urljoin(BASE, href)
                sub_title = safe(a.get_text(" ", strip=True) or "folder")
                sub_dir = dest_dir / sub_title
                f1, f2 = walk_content(
                    api, course, full, sub_dir,
                    crumbs + [sub_title], seen, depth + 1,
                )
                folders += 1 + f1
                files += f2
            elif "displayLearningUnit" in href:
                full = urljoin(BASE, href)
                sub_title = safe(a.get_text(" ", strip=True) or "module")
                sub_dir = dest_dir / sub_title
                f1, f2 = walk_content(
                    api, course, full, sub_dir,
                    crumbs + [sub_title], seen, depth + 1,
                )
                folders += 1 + f1
                files += f2

        if attached_any:
            vlog(f"{'  ' * depth}+ {title}")

    return folders, files


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def archive_course(api: APIRequestContext, course: Course) -> dict:
    course.out_dir.mkdir(parents=True, exist_ok=True)
    log(f"\n=== {course.name} ({course.pk1}) ===")
    summary = {"course_id": course.pk1, "name": course.name, "available": course.available}

    menu = get_course_menu(api, course)
    save(course.out_dir / "_menu.json", json.dumps(menu, indent=2))
    log(f"  menu: {len(menu)} entries")
    summary["menu_entries"] = len(menu)

    # Announcements
    n = fetch_announcements(api, course, course.out_dir)
    log(f"  announcements: {n}")
    summary["announcements"] = n

    # Grades
    got = fetch_grades(api, course, course.out_dir)
    log(f"  grades: {'saved' if got else 'not found'}")
    summary["grades"] = bool(got)

    # Discussions
    nf = fetch_discussions(api, course, course.out_dir)
    log(f"  discussion forums: {nf}")
    summary["discussion_forums"] = nf

    # Content areas from menu
    total_folders = 0
    total_files = 0
    seen: set[str] = set()
    for entry in menu:
        href = entry["href"]
        if "listContent.jsp" not in href:
            continue
        if f"course_id={course.pk1}" not in href and quote(course.pk1) not in href:
            continue
        section_title = safe(entry["title"]) or "content"
        section_dir = course.out_dir / section_title
        log(f"  → {entry['title']}")
        f1, f2 = walk_content(api, course, href, section_dir, [section_title], seen)
        total_folders += f1
        total_files += f2
    log(f"  content: {total_folders} folders, {total_files} files")
    summary["folders"] = total_folders
    summary["files"] = total_files
    save(course.out_dir / "_summary.json", json.dumps(summary, indent=2))
    return summary


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / ".pw_profile"),
            headless=HEADLESS,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        try:
            ensure_login(ctx)
            api = ctx.request
            courses = list_courses(api)
            if not courses:
                log("No courses to archive. Exiting.")
                return 0
            summaries = []
            for c in courses:
                try:
                    s = archive_course(api, c)
                    summaries.append(s)
                except Exception as e:  # noqa: BLE001
                    log(f"  !! error on {c.pk1}: {e}")
                    if VERBOSE:
                        traceback.print_exc()
                    summaries.append({"course_id": c.pk1, "error": str(e)})
            save(OUT / "_index.json", json.dumps(summaries, indent=2))
            log("\nDone.")
            log(f"Output: {OUT}")
            return 0
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
