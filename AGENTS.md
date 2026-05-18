# Instructions for AI assistants

If a human asks you to "run this for me" or "set this up", here is exactly what to do.

## What this is

A Python script that signs into a Blackboard Learn instance once (via the user's
SSO in a popped-up Chromium window) and then bulk-downloads every reachable file:
course content attachments, announcements, grades, assignment submissions, graded
discussion posts. The user is a student trying to preserve their coursework before
their school's Blackboard is shut down.

## What the user needs from you

1. Install Python 3 if it's missing.
2. Run `bash setup.sh` from this repo's directory (macOS/Linux). It creates a
   venv, installs `playwright`, `beautifulsoup4`, `requests`, downloads the
   Chromium binary, and then launches `nec_archive.py`.
3. When the Chromium window pops up, instruct the user to sign in to their
   school's Blackboard SSO **in that specific window** (the "Google Chrome for
   Testing" app, NOT their regular Chrome). The script auto-detects login and
   continues — no need for them to press anything.
4. Tell the user to leave the Chromium window open until the terminal prints
   `Done.` Closing it mid-run crashes the scrape.
5. Output ends up at `./out/<course>/`. ~5–30 min depending on course count and
   attachment volume.

## OS-specific notes

- **macOS / Linux**: `bash setup.sh` is the one command.
- **Windows**: PowerShell. The equivalent is:
  ```powershell
  python -m venv .venv
  .venv\Scripts\pip install playwright beautifulsoup4 requests
  .venv\Scripts\playwright install chromium
  .venv\Scripts\python nec_archive.py
  ```
  If `python` isn't installed, install from <https://python.org/downloads/> first
  (check the "Add Python to PATH" box during install).

## Configuration the user might need

- Different school: prepend `BB_BASE="https://blackboard.theirschool.edu"` to the
  python command. Default is NEC.
- Specific courses only: `NEC_COURSES=_12345_1,_67890_1` (the internal pk1 IDs;
  the script logs them on startup so the user can grab them from a first run).
- Verbose output for debugging: `NEC_VERBOSE=1`.

## Common issues you should pre-empt

- **"It's just sitting there saying 'waiting for login'"** — the user probably
  signed in to their regular Chrome, not the Chrome for Testing window the
  script opened. Direct them to the right window (different icon, sandbox look).
- **"It says the profile is locked"** — a previous run didn't clean up. Run
  `rm -f .pw_profile/SingletonLock .pw_profile/SingletonCookie .pw_profile/SingletonSocket`
  then retry.
- **A `.mov` or `.mp4` errored with "Timeout 30000ms exceeded"** — that's a
  Playwright size limit, not a real failure. Grab the URL(s) from the log,
  paste them one-per-line into `urls.txt`, and run
  `.venv/bin/python bigfile_download.py urls.txt --out ./bigfiles`.
- **A specific file returns HTTP 500** — that's Blackboard's server-side
  corruption on that file, not the script. Nothing to do; skip it.
- **Discussion board page returns a Java NPE / "Cannot invoke String.equals"** —
  that's a Blackboard backend bug on many instances. The user's graded
  discussion posts are still captured via the gradebook path (`submissions/D*/`).
  Don't keep debugging this.

## What NOT to do

- Don't commit `.pw_profile/`, `out/`, `bigfiles/`, or any log files — they
  contain the user's session cookies and personal coursework.
- Don't run headless on first auth (`NEC_HEADLESS=1`). The user must sign in
  through a visible Chromium window. Only use headless on re-runs after the
  session is saved (within ~few hours).
- Don't try to "improve" the discussion-board scraper to fetch from the broken
  endpoint. Multiple Blackboard instances return Java NPEs there; the gradebook
  path is the working workaround.
- Don't run multiple scrapes in parallel — they fight over `.pw_profile/`.
