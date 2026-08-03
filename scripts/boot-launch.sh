#!/bin/sh
# Launch Home — launch on TV boot (webOS Homebrew init.d).
#
# Waits for applicationManager / LS2 to come up after cold boot, then launches
# Launch Home a few times so webOS 4.x (slow startup) still ends on our home screen.
#
# Invoked detached from /var/lib/webosbrew/init.d/50-launch-home-boot.

APP_ID="org.webosbrew.lounge.launcher"
LOG="/tmp/launch-home-boot.log"

log() {
  echo "$(date '+%H:%M:%S' 2>/dev/null) $*" >>"$LOG" 2>/dev/null || true
}

log "boot-launch start"

luna_once() {
  uri=$1
  payload=$2
  out=$(luna-send -i -n 1 -w 4000 -f "$uri" "$payload" 2>/dev/null) && {
    printf '%s' "$out"
    return 0
  }
  out=$(luna-send -i -n 1 -f "$uri" "$payload" 2>/dev/null) && {
    printf '%s' "$out"
    return 0
  }
  out=$(luna-send -n 1 -f "$uri" "$payload" 2>/dev/null) && {
    printf '%s' "$out"
    return 0
  }
  return 1
}

# Wait up to ~3 minutes for the app framework after power-on.
i=0
while [ "$i" -lt 90 ]; do
  if luna_once 'luna://com.webos.applicationManager/getForegroundAppInfo' '{"subscribe":true}' >/dev/null 2>&1; then
    log "ls2 ready after $((i * 2))s"
    break
  fi
  i=$((i + 1))
  sleep 2
done

# Extra settle — webOS 4 often reports LS2 before the compositor is ready.
sleep 8

attempt=1
while [ "$attempt" -le 5 ]; do
  log "launch attempt $attempt"
  luna_once 'luna://com.webos.applicationManager/launch' "{\"id\":\"${APP_ID}\"}" >>"$LOG" 2>&1 || true
  sleep 4
  fg=$(luna_once 'luna://com.webos.applicationManager/getForegroundAppInfo' '{"subscribe":true}' 2>/dev/null)
  case "$fg" in
    *"$APP_ID"*)
      log "Launch Home is foreground — done"
      exit 0
      ;;
  esac
  attempt=$((attempt + 1))
  sleep 3
done

log "finished attempts (may still be launching under stock home)"
exit 0
