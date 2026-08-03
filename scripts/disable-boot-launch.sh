#!/bin/sh
# Disable Launch Home boot-on-start (run as root via hbchannel exec).

INITD="/var/lib/webosbrew/init.d/50-launch-home-boot"
OLD_INITD="/var/lib/webosbrew/init.d/50-lounge-boot"

rm -f "$INITD" "$OLD_INITD"
echo disabled
exit 0
