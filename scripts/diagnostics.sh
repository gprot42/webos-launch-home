#!/bin/sh
# Launch Home TV check: model, CPU, memory, background activity, and how long
# TV Settings takes to open. Run as root (Settings -> TV check, or over SSH):
#   sh diagnostics.sh [--open-settings] [--app "Launch Home settings line"]
# Prints a short summary (shown on the TV and in its QR code), then details.
# The full report is also saved to /tmp/launch-home-diagnostics.txt.

OUT=/tmp/launch-home-diagnostics.txt
TOPTMP=/tmp/launch-home-diag-top.txt
SPAWNTMP=/tmp/launch-home-diag-spawn.txt
LOGTMP=/tmp/launch-home-diag-log.txt
SYSLOG=/var/log/messages
OPEN_SETTINGS=0
APP_LINE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --open-settings) OPEN_SETTINGS=1 ;;
    --app) shift; APP_LINE=$1 ;;
  esac
  shift
done

# This TV generation's luna-send prints nothing without -i.
luna() {
  luna-send -i -n 1 -w 4000 -f "$1" "$2" 2>/dev/null </dev/null | tr -s ' \n' '  '
}

# Value of "key" in flattened JSON text on stdin.
json_field() {
  sed -n "s/.*\"$1\": *\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" | head -n 1
}

uptime_cs() {
  awk '{printf "%d", $1 * 100}' /proc/uptime
}

# Per-core CPU counters: "cpuN total idle iowait" lines. The TV switches cores
# off and on, which makes the combined "cpu" line jump backwards, so usage is
# worked out from the cores present in both snapshots (cpu_usage).
cpu_snapshot() {
  awk '/^cpu[0-9]/{t=0; for (i=2; i<=NF; i++) t+=$i; print $1, t, $5, $6}' /proc/stat
}

# "busy wait" percentages (0-100) between two cpu_snapshot outputs.
cpu_usage() {
  printf '%s\n--\n%s\n' "$1" "$2" | awk '
    $1 == "--" { second = 1; next }
    !second { t[$1] = $2; i[$1] = $3; w[$1] = $4; next }
    ($1 in t) && $2 >= t[$1] && $3 >= i[$1] && $4 >= w[$1] {
      dt += $2 - t[$1]; di += $3 - i[$1]; dw += $4 - w[$1]
    }
    END {
      if (dt <= 0) { print 0, 0; exit }
      b = int(100 * (dt - di - dw) / dt); x = int(100 * dw / dt)
      if (b < 0) b = 0; if (b > 100) b = 100; if (x < 0) x = 0; if (x > 100) x = 100
      print b, x
    }'
}

swapin_pages() {
  awk '$1 == "pswpin" {print $2; exit}' /proc/vmstat
}

# Boot-clock time of the first system log line on stdin ("[330.457257700]"),
# in centiseconds.
log_cs() {
  sed -n 's/^[^[]*\[\([0-9]*\)\.\([0-9][0-9]\)[0-9]*\].*/\1\2/p' | head -n 1
}

settings_on_screen() {
  luna 'luna://com.webos.applicationManager/getForegroundAppInfo' '{"extraInfo":true}' |
    grep -q "\"$1\""
}

# Close TV Settings ($1) and check it really went: on some TVs (webOS 9)
# closeByAppId alone left it on screen. Tries closeByAppId, then closing its
# process, then bringing Launch Home back. Sets closed (1 or 0) and close_how.
close_settings() {
  csid=$1
  closed=0
  close_how="not closed"
  for how in closeByAppId process launch-home; do
    case "$how" in
      closeByAppId)
        luna 'luna://com.webos.applicationManager/closeByAppId' "{\"id\":\"$csid\"}" >/dev/null
        ;;
      process)
        cpid=$(luna 'luna://com.webos.applicationManager/running' '{}' | tr '}' '\n' |
          grep "\"id\": *\"$csid\"" | grep -o -i '"processid": *"\{0,1\}[0-9]*' |
          grep -o '[0-9]*$' | head -n 1)
        [ -n "$cpid" ] && luna 'luna://com.webos.applicationManager/close' \
          "{\"processId\":\"$cpid\"}" >/dev/null
        ;;
      launch-home)
        luna 'luna://com.webos.applicationManager/launch' '{"id":"org.webosbrew.lounge.launcher"}' >/dev/null
        ;;
    esac
    i=0
    while [ "$i" -lt 8 ]; do
      sleep 0.4
      if ! settings_on_screen "$csid"; then
        closed=1
        close_how=$how
        return 0
      fi
      i=$((i + 1))
    done
  done
  return 1
}

# Open TV Settings ($1) once, wait until it is on screen (20 s at most), then
# close it after $2 seconds (close_settings). Sets open_cs, call_cs, shown,
# closed and, from the system log and counters during the open: timeline,
# busy_pct, wait_pct, swap_mb, nerr (error-looking log lines); the log window
# is left in $LOGTMP.
open_settings() {
  # Take both arguments now: `set --` below reuses $1..$3 for CPU counters.
  # (Reading $2 after it slept for the idle-jiffies count, days, so TV
  # Settings never closed.)
  osid=$1
  ostay=$2
  n0=$(wc -l < "$SYSLOG" 2>/dev/null || echo 0)
  sw0=$(swapin_pages)
  cpu0=$(cpu_snapshot)
  s0=$(uptime_cs)
  luna 'luna://com.webos.applicationManager/launch' "{\"id\":\"$osid\"}" >/dev/null
  s1=$(uptime_cs)
  shown=0
  while [ $(( $(uptime_cs) - s0 )) -lt 2000 ]; do
    if luna 'luna://com.webos.applicationManager/getForegroundAppInfo' '{"extraInfo":true}' |
        grep -q "\"$osid\""; then
      shown=1
      break
    fi
    sleep 0.2
  done
  s2=$(uptime_cs)
  sw1=$(swapin_pages)
  cpu1=$(cpu_snapshot)
  open_cs=$((s2 - s0))
  call_cs=$((s1 - s0))
  set -- $(cpu_usage "$cpu0" "$cpu1")
  busy_pct=$1
  wait_pct=$2
  swap_mb=$(( (${sw1:-0} - ${sw0:-0}) * 4 / 1024 ))
  sleep 0.5
  tail -n +$((n0 + 1)) "$SYSLOG" 2>/dev/null | head -n 400 > "$LOGTMP"
  # Where the time went: launch -> loading spinner -> Settings on screen.
  tb=$(grep 'NL_APP_LAUNCH_BEGIN' "$LOGTMP" | grep "\"$osid\"" | log_cs)
  tsp=$(grep 'NL_VSC' "$LOGTMP" | grep '"com.webos.app.spinner"' | grep '"visible": *true' | log_cs)
  tvis=$(grep 'NL_VSC' "$LOGTMP" | grep "\"$osid\"" | grep '"visible": *true' | log_cs)
  timeline="no log timeline"
  if [ -n "$tb" ] && [ -n "$tvis" ]; then
    timeline=$(awk -v b="$tb" -v s="$tsp" -v v="$tvis" 'BEGIN {
      if (s != "") printf "spinner %.1f s, ", (s - b) / 100
      printf "on screen %.1f s", (v - b) / 100 }')
  fi
  nerr=$(grep -i -E 'error|fail|timeout|timed out|not running|does not exist|denied|refused|oom|low memory|kill' "$LOGTMP" |
    grep -c -v -E 'NL_APP_LAUNCH|NL_VSC')
  sleep "$ostay"
  close_settings "$osid"
}

secs() {
  awk -v c="$1" 'BEGIN {printf "%.1f s", c / 100}'
}

last_pid() {
  cat /proc/sys/kernel/ns_last_pid 2>/dev/null || echo 0
}

# Busiest processes in a `top -b` capture: average %CPU per PID over the
# iterations after the first, with each PID's full command line.
busiest() {
  awk '
    /^top -/ { iter++ ; next }
    /PID/ && /%CPU/ {
      for (i = 1; i <= NF; i++) { if ($i == "%CPU") c = i; if ($i == "PID") p = i }
      next
    }
    iter >= 2 && c && $p ~ /^[0-9]+$/ { sum[$p] += $c; name[$p] = $NF; n = iter - 1 }
    END { if (n > 0) for (pid in sum) if (sum[pid] / n >= 1) printf "%.0f %s %s\n", sum[pid] / n, pid, name[pid] }
  ' "$1" | sort -rn | head -n "${2:-8}" | while read -r pct pid name; do
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null |
      sed 's#^/usr/s*bin/##; s#^/usr/lib/##' | cut -c1-48)
    [ -n "$cmd" ] || cmd="$name (exited)"
    echo "  ${pct}%  $cmd"
  done
}

report() {
  # --- TV -------------------------------------------------------------------
  sys=$(luna 'luna://com.webos.service.tv.systemproperty/getSystemInfo' \
    '{"keys":["modelName","firmwareVersion","sdkVersion","boardType"]}')
  os=$(tr -s ' \n' '  ' < /var/run/nyx/os_info.json 2>/dev/null)
  model=$(echo "$sys" | json_field modelName)
  fw=$(echo "$sys" | json_field firmwareVersion)
  release=$(echo "$os" | json_field webos_release)
  cores=$(grep -c '^processor' /proc/cpuinfo)
  mhz=$(awk '{printf "%d", $1 / 1000}' /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null)
  soc=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null | sed 's/^LG Electronics, //')
  mem=$(awk '/^MemTotal/{t=$2} /^MemAvailable/{a=$2} /^SwapTotal/{st=$2} /^SwapFree/{sf=$2}
    END {printf "RAM %d MB (%d MB free) | swap used %d of %d MB", t/1024, a/1024, (st-sf)/1024, st/1024}' /proc/meminfo)
  qs=$(luna 'luna://com.webos.settingsservice/getSystemSettings' \
    '{"category":"option","keys":["quickStartMode"]}' | json_field quickStartMode)
  up=$(awk '{printf "%dh%02d", $1 / 3600, ($1 % 3600) / 60}' /proc/uptime)
  load=$(cut -d' ' -f1-3 /proc/loadavg)

  # --- Load over 4 s: CPU busy, new processes, busiest processes -----------
  cpu0=$(cpu_snapshot)
  p0=$(last_pid)
  top -b -n 2 -d 4 > "$TOPTMP" 2>/dev/null
  p1=$(last_pid)
  set -- $(cpu_usage "$cpu0" "$(cpu_snapshot)")
  busy=$1
  spawn=$(( (p1 - p0) / 4 ))
  top_now=$(busiest "$TOPTMP" 5)

  # --- Who keeps starting processes (3 s sample) ----------------------------
  # Task ids count threads too, so keep only real processes (Tgid == id).
  self_cmd=$(tr '\0' ' ' < /proc/$$/cmdline 2>/dev/null | cut -c1-50)
  (
    last=$(last_pid); end=$(( $(uptime_cs) + 300 ))
    while [ "$(uptime_cs)" -lt "$end" ]; do
      cur=$(last_pid); p=$((last + 1))
      while [ "$p" -le "$cur" ]; do
        tgid=$(awk '/^Tgid:/{print $2; exit}' "/proc/$p/status" 2>/dev/null)
        if [ "$tgid" = "$p" ] && [ -r "/proc/$p/cmdline" ]; then
          c=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | cut -c1-60)
          pp=$(awk '{print $4}' "/proc/$p/stat" 2>/dev/null)
          pc=$(tr '\0' ' ' < "/proc/$pp/cmdline" 2>/dev/null | cut -c1-50)
          # Skip this script's own helpers (sort, awk, ...).
          [ -n "$c" ] && [ "$pc" != "$self_cmd" ] && echo "$pc -> $c"
        fi
        p=$((p + 1))
      done
      last=$cur
    done
  ) > "$SPAWNTMP"
  procs_rate=$(( $(wc -l < "$SPAWNTMP") / 3 ))
  spawners=$(sed 's/[0-9]\{3,\}/N/g; s#/usr/s*bin/##g; s#/usr/lib/##g' "$SPAWNTMP" |
    sort | uniq -c | sort -rn | head -n 4 | cut -c1-96)
  [ -n "$spawners" ] || spawners="  none"

  # --- TV Settings open time --------------------------------------------------
  # Opened twice: the first open after a reboot loads everything from scratch,
  # the second shows the usual speed. The first also records what the TV did
  # meanwhile, from the system log.
  settings_line="not tested"
  settings_closed=""
  settings_why=""
  settings_top=""
  settings_log=""
  if [ "$OPEN_SETTINGS" = 1 ]; then
    sid=""
    for id in com.palm.app.settings com.webos.app.settings; do
      if luna 'luna://com.webos.applicationManager/getAppInfo' "{\"id\":\"$id\"}" |
          grep -q '"returnValue": true'; then
        sid=$id
        break
      fi
    done
    if [ -n "$sid" ]; then
      top -b -n 8 -d 1 > "$TOPTMP" 2>/dev/null &
      open_settings "$sid" 4
      wait
      settings_top=$(busiest "$TOPTMP" 4)
      if [ "$shown" = 1 ]; then
        settings_line="opened in $(secs "$open_cs")"
        settings_why="  first open: $timeline | CPU busy ${busy_pct}%, disk wait ${wait_pct}% | swapped in ${swap_mb} MB | ${nerr} errors logged"
        n_log=$(wc -l < "$LOGTMP")
        settings_log=$(sed 's/^[^[]*\[\([0-9]*\.[0-9][0-9]\)[0-9]*\] [^ ]* /\1 /' "$LOGTMP" | cut -c1-150 | head -n 40)
        settings_log_head="TV Settings system log, first open ($n_log lines, first 40):"
        first_call=$call_cs
        settings_closed="first: $close_how"
        if [ "$closed" = 1 ]; then
          sleep 3
          open_settings "$sid" 2
          if [ "$shown" = 1 ]; then
            settings_line="$settings_line, again in $(secs "$open_cs")"
          else
            settings_line="$settings_line, second time not within 20 s"
          fi
          settings_closed="$settings_closed, second: $close_how"
          if [ "$closed" != 1 ]; then
            settings_line="$settings_line, then didn't close by itself (press Exit)"
          fi
        else
          # Don't open it again over itself: that measures nothing.
          settings_line="$settings_line, didn't close by itself (press Exit)"
        fi
        settings_line="$settings_line (launch call $(secs "$first_call"))"
      else
        settings_line="did not appear within 20 s"
      fi
      settings_line="$settings_line [$sid]"
    else
      settings_line="settings app not found"
    fi
  fi

  # TV Settings gives up on a Luna call after 10 s and logs SETTINGS_NO_RES.
  # The service never answered: it is stopped or stubbed out (privacy tools
  # do that), and each such call is 10 s of Settings waiting with the CPU idle.
  nores_n=$(grep -c 'SETTINGS_NO_RES' "$SYSLOG" 2>/dev/null)
  nores_top=""
  if [ "${nores_n:-0}" -gt 0 ]; then
    nores_top=$(grep -h 'SETTINGS_NO_RES' "$SYSLOG" 2>/dev/null | awk '{
        svc = $0; sub(/.*"service": *"(luna|palm):\/\//, "", svc); sub(/".*/, "", svc)
        m = $0; if (sub(/.*"method": *"/, "", m)) sub(/".*/, "", m); else m = "?"
        sub(/^com\.webos\.service\./, "", svc); sub(/\/+$/, "", svc)
        print svc "/" m
      }' | sort | uniq -c | sort -rn | head -n 4 | awk '{printf "%s%s x%s", (NR > 1 ? ", " : ""), $2, $1}')
  fi

  home_default=$(luna 'luna://com.webos.settingsservice/getSystemSettings' \
    '{"category":"general","keys":["defaultApps"]}' | sed -n 's/.*"home": *"\([^"]*\)".*/\1/p')
  case "$home_default" in
    ""|com.webos.app.home) home_line="${home_default:-default}" ;;
    *) home_line="$home_default  <-- NOT LG home: Home button does nothing" ;;
  esac
  inputs=$(luna 'luna://com.webos.service.eim/getAllInputStatus' '{}' | tr '{' '\n' |
    sed -n 's/.*"label": *"\([^"]*\)".*"id": *"\([A-Z0-9_]*\)".*/\2=\1/p' | tr '\n' ' ')
  hooks=$(ls /var/lib/webosbrew/init.d 2>/dev/null | tr '\n' ' ')
  apps=$(ls /media/developer/apps/usr/palm/applications 2>/dev/null | tr '\n' ' ')
  services=$(ls /media/developer/apps/usr/palm/services 2>/dev/null | tr '\n' ' ')
  watcher="off"
  if [ -f /tmp/launch-home-watcher.pid ] && kill -0 "$(cat /tmp/launch-home-watcher.pid)" 2>/dev/null; then
    watcher="running"
  fi

  echo "=== TV check ==="
  echo "TV: $model | webOS $release | fw $fw"
  echo "CPU: $cores cores ${mhz:+@ ${mhz} MHz} ${soc:+| $soc}"
  echo "$mem"
  echo "Quick Start+: ${qs:-unknown} | up $up | load $load"
  echo "CPU busy: ${busy}% (4 s) | new processes: ${procs_rate}/s (normal: 0-2) | new threads+processes: ${spawn}/s"
  echo "TV Settings: $settings_line"
  [ -n "$settings_why" ] && echo "$settings_why"
  echo "Settings calls that got no answer since boot (10 s wait each): ${nores_n:-0}${nores_top:+ | $nores_top}"
  echo "Home button app: $home_line"
  echo "Busiest now:"
  echo "$top_now"
  if [ -n "$settings_top" ]; then
    echo "Busiest while TV Settings opened:"
    echo "$settings_top"
  fi
  echo "Most started processes (3 s):"
  echo "$spawners" | sed 's/^ */  /'
  echo "Startup hooks: ${hooks:-none}"
  echo "=== details ==="
  echo "Launch Home: ${APP_LINE:-not given}"
  [ -n "$settings_closed" ] && echo "TV Settings closed by: $settings_closed"
  echo "Inputs the TV reports: ${inputs:-none}"
  echo "Home button watcher: $watcher"
  echo "Dev apps: ${apps:-none}"
  echo "Dev services: ${services:-none}"
  echo "Kernel: $(echo "$os" | json_field core_os_kernel_version)"
  if [ -n "$settings_log" ]; then
    echo "$settings_log_head"
    echo "$settings_log" | sed 's/^/  /'
  fi
}

report | tee "$OUT"
rm -f "$TOPTMP" "$SPAWNTMP" "$LOGTMP"
