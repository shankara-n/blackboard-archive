#!/usr/bin/env bash
# One-shot setup + run for the Blackboard archiver (macOS / Linux).
# Safe to re-run; skips steps that are already done.
set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

say() { printf "${GREEN}==>${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!!${NC} %s\n" "$*"; }
fail() { printf "${RED}xx${NC} %s\n" "$*"; exit 1; }

# 1. Find a Python ≥ 3.10
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    major=${ver%.*}; minor=${ver#*.}
    if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
      PY="$cand"
      break
    fi
  fi
done
if [ -z "$PY" ]; then
  if [ "$(uname)" = "Darwin" ]; then
    fail "Need Python 3.10+. Install with: brew install python  (or get it from https://python.org/downloads/)"
  else
    fail "Need Python 3.10+. Install via your package manager or from https://python.org/downloads/"
  fi
fi
say "Using $PY ($("$PY" --version))"

# 2. venv
if [ ! -d .venv ]; then
  say "Creating virtualenv at .venv/"
  "$PY" -m venv .venv
fi
. .venv/bin/activate
say "venv active: $(which python)"

# 3. Deps
if ! python -c "import playwright" >/dev/null 2>&1; then
  say "Installing Python packages (playwright, beautifulsoup4, requests)"
  pip install --upgrade pip --quiet
  pip install playwright beautifulsoup4 requests --quiet
else
  say "Python packages already installed"
fi

# 4. Chromium binary for Playwright
# Detect by checking if `playwright install --dry-run` reports anything pending
if ! python -m playwright install --dry-run chromium 2>&1 | grep -q "is already installed"; then
  say "Downloading Chromium for Playwright (~150 MB, one-time)"
  python -m playwright install chromium
else
  say "Chromium already installed"
fi

# 5. Clean any stale profile lock from a previous crashed run
if [ -d .pw_profile ]; then
  rm -f .pw_profile/SingletonLock .pw_profile/SingletonCookie .pw_profile/SingletonSocket 2>/dev/null || true
fi

# 6. Friendly run banner
cat <<'EOF'

================================================================
READY. About to launch the scraper.
================================================================
  • A "Chrome for Testing" window will open (it looks like Chrome
    but with a different icon — that's NORMAL, the script needs
    its own browser).
  • Sign in to your Blackboard via your school's SSO IN THAT
    WINDOW. (Not your regular Chrome.) Complete MFA as usual.
  • Once signed in, do nothing — the script polls every 3 seconds
    and continues automatically.
  • Leave the window open until your terminal prints "Done."
    (5–30 min depending on how much content you have.)
  • Output goes to ./out/
================================================================

Starting in 3 seconds...
EOF
sleep 3

exec python nec_archive.py
