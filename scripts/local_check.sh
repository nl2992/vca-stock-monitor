#!/bin/bash
# Local VCA stock check, run by launchd every 15 minutes.
#
# Uses the same probe, the same classifier and the same notification channel as
# the GitHub Actions workflow: when the bracelet becomes purchasable it opens an
# issue on nl2992/vca-stock-monitor, which makes GitHub email you. A single open
# issue labelled vca-in-stock is the duplicate guard, shared with Actions, so
# whichever checker sees it first notifies and the other stays quiet.
#
# Read-only with respect to the retailer: it never clicks, signs in, adds to
# bag or buys anything.
set -uo pipefail

# launchd gives an almost-empty PATH, so Homebrew tools must be named explicitly.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Derived from this script's own location, so the same file works from the
# Desktop working copy or from the launchd runtime clone. launchd cannot
# execute anything under ~/Desktop, ~/Documents or ~/Downloads without Full
# Disk Access, which is why the scheduled copy lives outside those folders.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="nl2992/vca-stock-monitor"
LABEL="vca-in-stock"
LOG="$HOME/Library/Logs/vca-monitor.log"
PY="$REPO_DIR/.venv/bin/python"
# Scratch files live outside the repo. Writing them inside it left the runtime
# clone permanently dirty, which made the self-updating `git pull` abort.
STATE_DIR="$HOME/Library/Application Support/vca-monitor"
mkdir -p "$STATE_DIR"
EVIDENCE="$STATE_DIR/probe.json"
BODY="$STATE_DIR/issue-body.md"

mkdir -p "$(dirname "$LOG")"

# Simulated runs are tagged in the log. Without this a test run writes a bare
# "AVAILABLE" line that is indistinguishable from a real stock hit when the log
# is read back days later.
LOG_TAG=""
[ "${VCA_SIMULATE:-none}" != "none" ] && LOG_TAG="[SIMULATED] "
log() { printf '%s  %s%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_TAG" "$*" >> "$LOG"; }

notify() {  # $1 = title, $2 = message
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" 2>/dev/null || true
}

cd "$REPO_DIR" || { log "FATAL: repo directory missing"; exit 1; }

# Keep the runtime clone in step with main, so a fix pushed to GitHub reaches
# the scheduled checker without a manual step. Failure here is not fatal: an
# older but working checkout still beats no check at all.
if [ "${VCA_SELF_UPDATE:-1}" = "1" ] && command -v git >/dev/null 2>&1; then
  if git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$REPO_DIR" pull --quiet --ff-only 2>>"$LOG" || log "note: git pull failed, continuing with the current checkout"
  fi
fi

if [ ! -x "$PY" ]; then
  log "FATAL: virtualenv python not found at $PY"
  notify "VCA monitor broken" "Python environment missing; no checking is happening."
  exit 1
fi

# --- look at the page -------------------------------------------------------
if ! "$PY" scripts/browser_probe.py --json-out "$EVIDENCE" >/dev/null 2>&1; then
  : # a non-zero exit means inconclusive; decide.py reads the evidence either way
fi

STATE=$("$PY" scripts/decide.py --evidence "$EVIDENCE" --body-out "$BODY" \
          --simulate "${VCA_SIMULATE:-none}" 2>/dev/null \
        | sed -n 's/^state=\([A-Z_]*\).*/\1/p')
STATE="${STATE:-INCONCLUSIVE}"

REASON=$("$PY" -c "
import json
try:
    print(json.load(open('$EVIDENCE')).get('reason',''))
except Exception as exc:
    print(f'evidence unreadable: {exc}')
" 2>/dev/null)

# --- act --------------------------------------------------------------------
case "$STATE" in
  NOT_AVAILABLE)
    log "not available  ($REASON)"
    exit 0
    ;;
  INCONCLUSIVE)
    # Never treated as out of stock. Logged quietly; the local notification
    # only appears if it keeps happening, which the log makes visible.
    log "INCONCLUSIVE  ($REASON)  no availability claim made, no purchase attempted"
    exit 0
    ;;
  AVAILABLE)
    log "AVAILABLE  ($REASON)"
    ;;
  *)
    log "unexpected state '$STATE'"
    exit 1
    ;;
esac

# --- AVAILABLE: notify through the shared GitHub channel --------------------
if ! command -v gh >/dev/null 2>&1; then
  log "AVAILABLE but gh is not installed; cannot open an issue"
  notify "VCA bracelet AVAILABLE" "Could not open a GitHub issue. Check the page now."
  exit 1
fi

EXISTING=$(gh issue list --repo "$REPO" --label "$LABEL" --state open \
             --json number --jq '.[0].number // empty' 2>/dev/null)

if [ -n "$EXISTING" ]; then
  log "AVAILABLE but issue #$EXISTING is already open; not re-notifying"
  exit 0
fi

TITLE="VCA Alhambra bracelet AVAILABLE - Vintage Alhambra 5 motifs Tiger Eye"
if [ "${VCA_SIMULATE:-none}" != "none" ]; then
  TITLE="[TEST] $TITLE"
fi

gh label create "$LABEL" --repo "$REPO" --color B60205 \
  --description "VCARD35600 appears purchasable online" >/dev/null 2>&1 || true

if URL=$(gh issue create --repo "$REPO" --title "$TITLE" --label "$LABEL" \
           --body-file "$BODY" 2>&1); then
  log "issue opened: $URL"
  notify "VCA bracelet AVAILABLE" "Issue opened. Check your email and the product page."
else
  log "AVAILABLE but opening the issue FAILED: $URL"
  notify "VCA bracelet AVAILABLE" "GitHub issue failed. Check the page now."
  exit 1
fi
