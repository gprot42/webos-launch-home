#!/bin/sh
# Disable Launch Home button watcher (run as root via hbchannel exec).

PIDF="/tmp/launch-home-watcher.pid"
OLD_PIDF="/tmp/lounge-home-watcher.pid"
INITD="/var/lib/webosbrew/init.d/40-launch-home-watcher"
OLD_INITD="/var/lib/webosbrew/init.d/40-lounge-home"

for pf in "$PIDF" "$OLD_PIDF"; do
  if [ -f "$pf" ]; then
    old=$(cat "$pf" 2>/dev/null)
    if [ -n "$old" ]; then
      kill "$old" 2>/dev/null || true
    fi
  fi
done
killall home-watcher.sh 2>/dev/null || true
pkill -f home-watcher.sh 2>/dev/null || true
rm -f "$PIDF" "$OLD_PIDF" "$INITD" "$OLD_INITD"
echo disabled
exit 0
