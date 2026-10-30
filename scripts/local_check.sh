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

# A quiet banner lost us a real 13-hour buying window on 2026-10-29. An
# AVAILABLE verdict is rare and time-critical, so it gets the loudest
# treatment the OS allows: a sounded notification repeated a few times, a
# spoken announcement, and the product page opened in the default browser so
# it is physically in front of you.
shout() {  # $1 = message
  for _ in 1 2 3; do
    /usr/bin/osascript -e "display notification \"$1\" with title \"VCA BRACELET AVAILABLE\" sound name \"Sosumi\"" 2>/dev/null || true
    sleep 2
  done
  /usr/bin/osascript -e 'display alert "VCA BRACELET AVAILABLE" message "The Tiger Eye 5-motif bracelet is purchasable online right now. Go buy it." as critical' >/dev/null 2>&1 &
  /usr/bin/say "Van Cleef bracelet is available now" 2>/dev/null || true
  /usr/bin/open "https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/vcard35600---vintage-alhambra-bracelet-5-motifs.html" 2>/dev/null || true
}

# Consecutive-failure tracking, so a dead checker cannot stay silent. The
# chromium cache was wiped on 2026-10-27 and 262 checks failed over three days
# while this script logged quietly and exited 0.
FAILFILE="$STATE_DIR/consecutive_failures"
# Set once per availability window so the cart grab fires a single time, not
# every five minutes for as long as the bracelet stays in stock.
CARTMARK="$STATE_DIR/cart_grabbed"
BROKEN_LABEL="vca-monitor-broken"
read_failures() { cat "$FAILFILE" 2>/dev/null || echo 0; }

# Adds the bracelet to a real, visible cart in Google Chrome using a dedicated
# persistent profile, so you can take the window over and check out yourself.
# It never touches checkout, payment or address fields. Once per window, and
# disable with VCA_AUTO_CART=0.
grab_to_cart() {
  [ "${VCA_AUTO_CART:-1}" = "1" ] || { log "auto-cart disabled by VCA_AUTO_CART"; return 0; }
  [ -f "$CARTMARK" ] && { log "cart grab already done for this window"; return 0; }
  : > "$CARTMARK"
  log "adding to a real Chrome cart"
  nohup "$PY" "$REPO_DIR/scripts/grab_to_cart.py" >>"$STATE_DIR/grab.log" 2>&1 &
  log "cart grab launched (see $STATE_DIR/grab.log)"
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
    rm -f "$CARTMARK"
    if [ "$(read_failures)" != "0" ]; then
      echo 0 > "$FAILFILE"
      if command -v gh >/dev/null 2>&1; then
        B=$(gh issue list --repo "$REPO" --label "$BROKEN_LABEL" --state open --json number --jq '.[0].number // empty' 2>/dev/null)
        [ -n "$B" ] && gh issue close "$B" --repo "$REPO" \
          --comment "Recovered: a check completed successfully." >/dev/null 2>&1 \
          && log "closed monitor-broken issue #$B"
      fi
    fi
    exit 0
    ;;
  INCONCLUSIVE)
    # Never treated as out of stock. Three in a row (~45 minutes) means the
    # checker is broken rather than merely unlucky, and that must be noisy.
    N=$(( $(read_failures) + 1 ))
    echo "$N" > "$FAILFILE"
    log "INCONCLUSIVE #$N  ($REASON)  no availability claim made, no purchase attempted"
    if [ "$N" -eq 3 ] || [ $(( N % 96 )) -eq 0 ]; then
      notify "VCA monitor BROKEN" "$N checks in a row failed. Stock is NOT being monitored."
      if command -v gh >/dev/null 2>&1; then
        gh label create "$BROKEN_LABEL" --repo "$REPO" --color D93F0B \
          --description "The local checker is failing" >/dev/null 2>&1 || true
        if [ -z "$(gh issue list --repo "$REPO" --label "$BROKEN_LABEL" --state open --json number --jq '.[0].number // empty' 2>/dev/null)" ]; then
          printf '@nl2992\n\nThe local launchd checker has failed %s times in a row, so the bracelet is **not** being monitored locally.\n\nLatest reason:\n\n```\n%s\n```\n\nNo availability claim was made. No purchase was attempted.\n\nUsual cause: the Playwright browser cache under `~/Library/Caches/ms-playwright` was cleared. Fix:\n\n```\ncd ~/vca-monitor-runtime && .venv/bin/python -m playwright install chromium\n```\n\nThis issue closes itself on the next successful check.\n' "$N" "$REASON" > "$STATE_DIR/broken-body.md"
          gh issue create --repo "$REPO" --title "VCA monitor is BROKEN - $N consecutive failed checks" \
            --label "$BROKEN_LABEL" --body-file "$STATE_DIR/broken-body.md" >/dev/null 2>&1 \
            && log "opened a monitor-broken issue"
        fi
      fi
    fi
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
  echo 0 > "$FAILFILE"
  grab_to_cart
  shout "Price $( [ -f "$EVIDENCE" ] && echo "\$5,850" ). Issue opened. BUY IT NOW."
else
  log "AVAILABLE but opening the issue FAILED: $URL"
  shout "GitHub issue failed, but the bracelet IS available. Check the page now."
  exit 1
fi
