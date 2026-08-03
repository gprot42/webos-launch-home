#!/bin/sh
# Enable Launch Home boot-on-start via Homebrew init.d (run as root via hbchannel exec).

LAUNCH="/media/developer/apps/usr/palm/applications/org.webosbrew.lounge.launcher/boot-launch.sh"
LOG="/tmp/launch-home-boot.log"
INITD="/var/lib/webosbrew/init.d/50-launch-home-boot"
# Legacy name from when the app was branded "Lounge Launcher".
OLD_INITD="/var/lib/webosbrew/init.d/50-lounge-boot"

if [ ! -f "$LAUNCH" ]; then
  echo missing_boot_script
  exit 1
fi

chmod 755 "$LAUNCH"
mkdir -p /var/lib/webosbrew/init.d
rm -f "$OLD_INITD"

cat >"$INITD" <<EOF
#!/bin/sh
LOG="$LOG"
LAUNCH="$LAUNCH"
if command -v setsid >/dev/null 2>&1; then
  setsid nohup "\$LAUNCH" >>"\$LOG" 2>&1 </dev/null &
elif command -v nohup >/dev/null 2>&1; then
  nohup "\$LAUNCH" >>"\$LOG" 2>&1 </dev/null &
else
  "\$LAUNCH" >>"\$LOG" 2>&1 </dev/null &
fi
exit 0
EOF
chmod 755 "$INITD"

echo enabled boot=$INITD
exit 0
