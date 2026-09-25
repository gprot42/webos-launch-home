#!/bin/sh
# Turn off Launch Home's voice assistant (run as root via hbchannel exec).
# Stops the daemon, removes the boot hook and the voice card, and gives the
# voice button back to LG. Settings and the SuperGrok sign-in are kept.
# Another voice assistant (VoxRelay) is never stopped or changed.

APPDIR="${LH_APPDIR:-/media/developer/apps/usr/palm/applications/org.webosbrew.lounge.launcher}"
RUN="$APPDIR/voice-run.sh"
CARD_ID=org.webosbrew.lounge.voice
CARD_DIR=/media/developer/apps/usr/palm/applications/$CARD_ID
INITD=/var/lib/webosbrew/init.d/45-launch-home-voice

rm -f "$INITD"

if [ -f "$RUN" ]; then
  sh "$RUN" stop >/dev/null 2>&1
else
  # Launch Home files already gone: stop by pidfile.
  for pf in /tmp/launch-home-voice-run.pid /tmp/launch-home-voice.pid; do
    p=$(cat "$pf" 2>/dev/null)
    if [ -n "$p" ]; then
      kill -TERM -"$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null
    fi
  done
  rm -f /tmp/launch-home-voice-run.pid /tmp/launch-home-voice.pid /tmp/launch-home-voice.ver
fi

luna-send -n 1 -w 3000 -f luna://com.webos.applicationManager/closeByAppId \
  "{\"id\":\"$CARD_ID\"}" >/dev/null 2>&1 </dev/null
if [ -d "$CARD_DIR" ]; then
  luna-send -n 6 -w 30000 -f luna://com.webos.appInstallService/dev/remove \
    "{\"id\":\"$CARD_ID\",\"subscribe\":true}" >/dev/null 2>&1 </dev/null &
  remover=$!
  i=0
  while [ "$i" -lt 40 ] && [ -d "$CARD_DIR" ]; do
    sleep 0.5
    i=$((i + 1))
  done
  kill "$remover" 2>/dev/null
fi

# The daemon blocked LG's own voice trigger and may have stopped LG's voice
# services. Undo that, unless another voice assistant still relies on it.
if ! pgrep -f "[v]oxrelay_daemon.py" >/dev/null 2>&1 &&
   ! grep -q ':21E5 00000000:0000 0A' /proc/net/tcp 2>/dev/null; then
  rm -f /tmp/voiceinput_disable_set_input_event
  if command -v systemctl >/dev/null 2>&1; then
    for svc in voiceinput.service voiceconductor.service; do
      if ! systemctl is-active "$svc" >/dev/null 2>&1; then
        systemctl start "$svc" >/dev/null 2>&1 </dev/null &
      fi
    done
  fi
fi

echo disabled
exit 0
