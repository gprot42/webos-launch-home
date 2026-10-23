#!/bin/sh
# Launch Home — intercept stock Home and open Launch Home instead.
#
# webOS always opens stock Home on the Home button first. We react when Home
# is foreground, bring Launch Home forward, close Home, then launch Launch Home AGAIN so
# webOS routes remote INPUT to our surface (launch alone often leaves input on
# Home even though Launch Home is painted on top — dock appears dead).
#
# Start detached only:
#   setsid nohup home-watcher.sh >/tmp/launch-home-watcher.log 2>&1 </dev/null &
#
# Hardened for webOS 4.x+: luna-send -w / setsid may be missing; subscribe can
# die after boot; we fall back to polling and auto-restart the subscribe loop.

APP_ID="org.webosbrew.lounge.launcher"
PIDFILE="/tmp/launch-home-watcher.pid"
LOG="/tmp/launch-home-watcher.log"
COOLDOWN_SEC=1
# When stock Home last came to the front (seconds), for the Home key watcher.
HOME_SEEN="/tmp/launch-home-home-seen"

log() {
  echo "$(date '+%H:%M:%S' 2>/dev/null) $*" >>"$LOG" 2>/dev/null || true
}

# Every process below $1 (children first found, then theirs), from /proc.
descendants() {
  for status in /proc/[0-9]*/status; do
    ppid=$(sed -n 's/^PPid:[[:space:]]*//p' "$status" 2>/dev/null)
    if [ "$ppid" = "$1" ]; then
      cpid=${status#/proc/}
      cpid=${cpid%/status}
      echo "$cpid"
      descendants "$cpid"
    fi
  done
}

# home-watcher.sh --stop: end the running watcher and everything below it.
# A plain kill leaves the Home key watcher's dd readers blocked on the remote
# until its next key press. (enable/disable-home-watcher.sh call this.)
if [ "$1" = "--stop" ]; then
  pid=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    # Listed before any kill: once a parent dies its children move to init.
    # shellcheck disable=SC2046 # one pid per word
    kill $(descendants "$pid") "$pid" 2>/dev/null
  fi
  rm -f "$PIDFILE"
  exit 0
fi

if [ -f "$PIDFILE" ]; then
  oldpid=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
    if [ "$oldpid" != "$$" ]; then
      log "already running pid=$oldpid, exiting"
      exit 0
    fi
  fi
fi
echo $$ >"$PIDFILE"
log "start pid=$$"

key_pids=""
# shellcheck disable=SC2086 # one pid per word
trap '[ -n "$key_pids" ] && kill $key_pids 2>/dev/null; rm -f "$PIDFILE"; log "exit"; exit 0' INT TERM HUP

is_home() {
  case "$1" in
    com.webos.app.home|com.webos.app.launcher|com.webos.app.homelauncher|\
    com.webos.app.homeupdater|com.webos.app.dashboard|com.palm.app.home|\
    com.webos.app.gamehome|com.webos.app.seniorhome)
      return 0
      ;;
  esac
  return 1
}

extract_app_id() {
  # Prefer top-level "appId"; fall back to foregroundAppInfo[0].appId (some
  # older webOS builds only fill the array form).
  raw=$(printf '%s' "$1" | tr '\n' ' ')
  id=$(printf '%s' "$raw" | sed -n 's/.*"appId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
  if [ -n "$id" ]; then
    printf '%s' "$id"
    return 0
  fi
  printf '%s' "$raw" | sed -n 's/.*"foregroundAppInfo"[^[]*\[[^]{]*{[^}]*"appId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}

# One-shot luna-send with webOS 4-compatible fallbacks (-w / -i may be missing).
luna_once() {
  uri=$1
  payload=$2
  out=$(luna-send -i -n 1 -w 3000 -f "$uri" "$payload" 2>/dev/null) && {
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

subscribe_fg() {
  # Prefer interactive subscribe; fall back if -i is unavailable.
  if luna-send -i \
    'luna://com.webos.applicationManager/getForegroundAppInfo' \
    '{"subscribe":true}' 2>/dev/null; then
    return 0
  fi
  luna-send \
    'luna://com.webos.applicationManager/getForegroundAppInfo' \
    '{"subscribe":true}' 2>/dev/null
}

launch_app() {
  log "launch $APP_ID"
  luna_once 'luna://com.webos.applicationManager/launch' "{\"id\":\"${APP_ID}\"}" >>"$LOG" 2>&1 || true
}

close_home() {
  for id in com.webos.app.home com.webos.app.launcher com.webos.app.homelauncher \
            com.webos.app.dashboard com.palm.app.home; do
    luna_once 'luna://com.webos.applicationManager/closeByAppId' "{\"id\":\"${id}\"}" >/dev/null 2>&1 || true
  done
}

# Bring Launch Home to front AND own input. Order matters on webOS 25:
#   1) launch Launch Home (graphics)
#   2) close stock Home (drop its input surface)
#   3) launch Launch Home again (reclaim input after Home is gone)
activate_app() {
  log "activate Launch Home"
  launch_app
  sleep 0.25
  close_home
  sleep 0.15
  launch_app
}

now_sec() {
  date +%s 2>/dev/null || echo 0
}

last_app=""
last_action=0
primed=0

handle_fg() {
  appId=$1
  [ -n "$appId" ] || return 0

  if [ "$primed" -eq 0 ]; then
    primed=1
    last_app="$appId"
    log "primed fg=$appId"
    return 0
  fi

  if [ "$appId" = "$APP_ID" ]; then
    last_app="$appId"
    return 0
  fi

  if is_home "$appId"; then
    now=$(now_sec)
    echo "$now" >"$HOME_SEEN" 2>/dev/null
    elapsed=$((now - last_action))
    if [ "$last_action" -ne 0 ] && [ "$elapsed" -lt "$COOLDOWN_SEC" ]; then
      return 0
    fi
    last_action=$now
    prev="$last_app"
    last_app="$appId"
    log "home after $prev -> activate Launch Home"
    activate_app
    return 0
  fi

  last_app="$appId"
}

run_subscribe() {
  log "subscribe start"
  buf=""
  subscribe_fg | while IFS= read -r line || [ -n "$line" ]; do
    buf="$buf $line"
    case "$buf" in
      *returnValue*)
        appId=$(extract_app_id "$buf")
        buf=""
        if [ -n "$appId" ]; then
          handle_fg "$appId"
        fi
        ;;
    esac
  done
  log "subscribe ended"
  return 0
}

# ── Home key ─────────────────────────────────────────────────────────────
# On some TVs (webOS 5 at least) stock Home is an overlay drawn by the surface
# manager, not an app: the foreground app never changes, so handle_fg never
# sees it. There we watch the remote's Home key itself (code 773 on LG's
# input devices) and open Launch Home when it is pressed.
HOME_KEY=773

# Bytes per struct input_event (two longs of timestamp, then type, code and
# value): 16 for a 32-bit userspace, 24 for a 64-bit one. Byte 4 of an ELF
# file is its class: 1 = 32-bit, 2 = 64-bit.
event_size() {
  class=$(od -An -tu1 -j4 -N1 /bin/sh 2>/dev/null | tr -d ' ')
  if [ "$class" = "2" ]; then echo 24; else echo 16; fi
}

# Event devices of the remote, e.g. "LGE M-RCU - Builtin" (Magic Remote).
remote_devices() {
  awk '
    /^N: Name=/ { name = $0 }
    /^H: Handlers=/ && name ~ /RCU|[Rr]emote/ {
      for (i = 2; i <= NF; i++) {
        f = $i
        sub(/^Handlers=/, "", f)
        if (f ~ /^event[0-9]+$/) print "/dev/input/" f
      }
    }
    /^$/ { name = "" }
  ' /proc/bus/input/devices 2>/dev/null
}

# True when a batch of events (od -tu2 words, `n` words per event) holds a
# Home key press: type 1 (EV_KEY), code HOME_KEY, value 1 (press, not repeat).
# The last four words of every event are type, code and the value's two halves.
has_home_press() {
  awk -v n="$1" -v key="$HOME_KEY" '
    { for (i = 1; i <= NF; i++) w[c++] = $i }
    END {
      for (s = 0; s + n <= c; s += n) {
        t = s + n - 4
        if (w[t] == 1 && w[t + 1] == key && w[t + 2] == 1 && w[t + 3] == 0) found = 1
      }
      exit !found
    }'
}

# After a Home press: if stock Home came up as an app, handle_fg (and the
# home activity, activities.sh) act on it, and Launch Home may be in front
# again already. Otherwise the Home overlay is over whatever is in front
# (Launch Home too), and launching Launch Home brings it back over the overlay.
on_home_key() {
  pressed=$(now_sec)
  sleep 1
  seen=$(cat "$HOME_SEEN" 2>/dev/null)
  if [ "${seen:-0}" -ge $((pressed - 1)) ]; then
    return 0
  fi
  fg=$(extract_app_id "$(luna_once 'luna://com.webos.applicationManager/getForegroundAppInfo' '{"subscribe":true}')")
  if is_home "$fg"; then
    return 0
  fi
  log "home key over ${fg:-unknown} -> activate Launch Home"
  activate_app
}

# Run in the background, one per device. A read of an event device returns
# whole events, every one queued (up to the buffer), so one dd + od + awk per
# batch rather than per event.
watch_home_key() {
  dev=$1
  size=$2
  exec 3<"$dev" || return 0
  log "home key watch $dev (${size}-byte events)"
  while true; do
    words=$(dd bs=$((size * 64)) count=1 <&3 2>/dev/null | od -An -v -tu2)
    if [ -z "$words" ]; then
      log "home key watch $dev ended"
      return 0
    fi
    if printf '%s\n' "$words" | has_home_press $((size / 2)); then
      on_home_key
    fi
  done
}

# Wait briefly for LS2 after cold boot (webOS 4 init.d can start us early).
boot_wait=0
while [ "$boot_wait" -lt 30 ]; do
  probe=$(luna_once 'luna://com.webos.applicationManager/getForegroundAppInfo' '{"subscribe":true}' 2>/dev/null)
  if [ -n "$probe" ]; then
    log "ls2 ready after ${boot_wait}s"
    break
  fi
  boot_wait=$((boot_wait + 1))
  sleep 1
done

size=$(event_size)
for dev in $(remote_devices); do
  watch_home_key "$dev" "$size" &
  key_pids="$key_pids $!"
done
[ -n "$key_pids" ] || log "no remote input device found; Home key not watched"

while true; do
  sub_start=$(now_sec)
  run_subscribe
  sub_lasted=$(( $(now_sec) - sub_start ))
  # Polling fallback while subscribe is down / between restarts. Every poll
  # spawns luna-send plus sed, so poll once a second, and when the subscribe
  # died at once (it isn't coming back soon) poll a minute before retrying it
  # instead of hammering both.
  polls=10
  if [ "$sub_lasted" -lt 5 ]; then
    polls=60
  fi
  i=0
  while [ "$i" -lt "$polls" ]; do
    raw=$(luna_once 'luna://com.webos.applicationManager/getForegroundAppInfo' '{"subscribe":true}')
    handle_fg "$(extract_app_id "$raw")"
    i=$((i + 1))
    sleep 1
  done
done
