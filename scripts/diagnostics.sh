#!/bin/sh
# Launch Home TV check: model, CPU, memory, background activity, and how long
# TV Settings takes to open. Run as root (Settings -> TV check, or over SSH):
#   sh diagnostics.sh [--open-settings]
# Prints a short summary (shown on the TV and in its QR code), then details.
# The full report is also saved to /tmp/launch-home-diagnostics.txt.

OUT=/tmp/launch-home-diagnostics.txt
TOPTMP=/tmp/launch-home-diag-top.txt
SPAWNTMP=/tmp/launch-home-diag-spawn.txt
OPEN_SETTINGS=0
[ "$1" = "--open-settings" ] && OPEN_SETTINGS=1

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

cpu_ticks() {
  # total and idle(+iowait) jiffies from the first line of /proc/stat
  awk '/^cpu /{t=0; for (i=2; i<=NF; i++) t+=$i; print t, $5 + $6; exit}' /proc/stat
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
  set -- $(cpu_ticks); t0=$1; i0=$2
  p0=$(last_pid)
  top -b -n 2 -d 4 > "$TOPTMP" 2>/dev/null
  p1=$(last_pid)
  set -- $(cpu_ticks); t1=$1; i1=$2
  busy=$(( (100 * ((t1 - t0) - (i1 - i0))) / ((t1 - t0) > 0 ? (t1 - t0) : 1) ))
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
  settings_line="not tested"
  settings_top=""
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
      s0=$(uptime_cs)
      luna 'luna://com.webos.applicationManager/launch' "{\"id\":\"$sid\"}" >/dev/null
      s1=$(uptime_cs)
      shown=0
      while [ $(( $(uptime_cs) - s0 )) -lt 2000 ]; do
        if luna 'luna://com.webos.applicationManager/getForegroundAppInfo' '{"extraInfo":true}' |
            grep -q "\"$sid\""; then
          shown=1
          break
        fi
        sleep 0.2
      done
      s2=$(uptime_cs)
      sleep 4
      luna 'luna://com.webos.applicationManager/closeByAppId' "{\"id\":\"$sid\"}" >/dev/null
      wait
      if [ "$shown" = 1 ]; then
        settings_line=$(awk -v a="$s0" -v b="$s1" -v c="$s2" 'BEGIN {
          printf "opened in %.1f s (launch call %.1f s)", (c - a) / 100, (b - a) / 100 }')
      else
        settings_line="did not appear within 20 s"
      fi
      settings_line="$settings_line [$sid]"
      settings_top=$(busiest "$TOPTMP" 4)
    else
      settings_line="settings app not found"
    fi
  fi

  home_default=$(luna 'luna://com.webos.settingsservice/getSystemSettings' \
    '{"category":"general","keys":["defaultApps"]}' | sed -n 's/.*"home": *"\([^"]*\)".*/\1/p')
  case "$home_default" in
    ""|com.webos.app.home) home_line="${home_default:-default}" ;;
    *) home_line="$home_default  <-- NOT LG home: Home button does nothing" ;;
  esac
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
  echo "Home button watcher: $watcher"
  echo "Dev apps: ${apps:-none}"
  echo "Dev services: ${services:-none}"
  echo "Kernel: $(echo "$os" | json_field core_os_kernel_version)"
}

report | tee "$OUT"
rm -f "$TOPTMP" "$SPAWNTMP"
