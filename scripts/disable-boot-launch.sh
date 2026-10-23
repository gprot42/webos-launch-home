#!/bin/sh
# Disable Launch Home boot-on-start (run as root via hbchannel exec).

INITD="/var/lib/webosbrew/init.d/50-launch-home-boot"
OLD_INITD="/var/lib/webosbrew/init.d/50-lounge-boot"
ACTIVITIES="/media/developer/apps/usr/palm/applications/org.webosbrew.lounge.launcher/activities.sh"
PIDFILE="/tmp/launch-home-boot.pid"

rm -f "$INITD" "$OLD_INITD"
sh "$ACTIVITIES" disarm boot 2>/dev/null || true

# Stop the wake watcher if it is running; its TERM trap ends its luna-send
# subscription too.
if [ -f "$PIDFILE" ]; then
  pid=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
fi

echo disabled
exit 0
