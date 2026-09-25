#!/bin/sh
# Turn on Launch Home's voice assistant (run as root via hbchannel exec).
# Safe to run again: it only installs or restarts what is missing or outdated,
# so Launch Home runs it at every start while voice is on.
#
# Prints "enabled ..." when done, "other_voice=voxrelay:running|installed"
# when another voice assistant is also on this TV (it is left alone), or an
# error word: missing_voice, missing_python, card_install_failed.

APPDIR="${LH_APPDIR:-/media/developer/apps/usr/palm/applications/org.webosbrew.lounge.launcher}"
VOICE="$APPDIR/voice"
RUN="$APPDIR/voice-run.sh"
CONF_DIR=/home/root/.config/launch-home-voice
CARD_ID=org.webosbrew.lounge.voice
CARD_DIR=/media/developer/apps/usr/palm/applications/$CARD_ID
CARD_TMP=/tmp/launch-home-voice-card.ipk
INSTALL_LOG=/tmp/launch-home-voice-install.log
INITD=/var/lib/webosbrew/init.d/45-launch-home-voice
# 8678 in /proc/net/tcp notation, listening.
PORT_LISTEN=':21E6 00000000:0000 0A'

if [ ! -f "$VOICE/daemon/voice_daemon.py" ] || [ ! -f "$RUN" ]; then
  echo missing_voice
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo missing_python
  exit 1
fi
chmod 755 "$RUN" 2>/dev/null

# Settings and the SuperGrok sign-in live here and stay when voice is off.
mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"
if [ ! -f "$CONF_DIR/config.json" ]; then
  cp "$VOICE/config.example.json" "$CONF_DIR/config.json"
fi
chmod 600 "$CONF_DIR/config.json" 2>/dev/null
chmod 600 "$CONF_DIR/oauth.json" 2>/dev/null

# The voice card: a small hidden app that shows the question and answer and
# plays the spoken reply. It is packed inside Launch Home as voice/card.pkg.
card_version() {
  sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -1
}
want=$(cat "$VOICE/card-version" 2>/dev/null)
have=$(card_version "$CARD_DIR/appinfo.json")
if [ -n "$want" ] && [ "$have" != "$want" ]; then
  cp -f "$VOICE/card.pkg" "$CARD_TMP"
  luna-send -n 6 -w 90000 -f luna://com.webos.appInstallService/dev/install \
    "{\"id\":\"com.ares.defaultName\",\"ipkUrl\":\"$CARD_TMP\",\"subscribe\":true}" \
    >"$INSTALL_LOG" 2>&1 </dev/null &
  installer=$!
  i=0
  while [ "$i" -lt 90 ]; do
    have=$(card_version "$CARD_DIR/appinfo.json")
    if [ "$have" = "$want" ]; then
      break
    fi
    if ! kill -0 "$installer" 2>/dev/null && [ "$i" -gt 10 ]; then
      break
    fi
    sleep 0.5
    i=$((i + 1))
  done
  kill "$installer" 2>/dev/null
  rm -f "$CARD_TMP"
  if [ "$have" != "$want" ]; then
    echo card_install_failed
    tail -5 "$INSTALL_LOG" 2>/dev/null
    exit 1
  fi
  echo "card_installed=$want"
fi

# Start at power-on.
mkdir -p /var/lib/webosbrew/init.d
cat >"$INITD" <<EOF
#!/bin/sh
# Launch Home voice assistant (turned on in Launch Home Settings > AI Voice).
if [ -f "$RUN" ]; then
  sh "$RUN" start >/dev/null 2>&1 </dev/null
fi
exit 0
EOF
chmod 755 "$INITD"

# Start the daemon, or restart it when Launch Home was updated.
state=$(sh "$RUN" status)
case "$state" in
  *current*)
    ;;
  running*)
    sh "$RUN" stop >/dev/null 2>&1
    sh "$RUN" start >/dev/null || true
    ;;
  *)
    sh "$RUN" start >/dev/null || true
    ;;
esac

# Another voice assistant answering the same button would clash with ours.
# Report it; never stop or change it.
other=""
if pgrep -f "[v]oxrelay_daemon.py" >/dev/null 2>&1 ||
   grep -q ':21E5 00000000:0000 0A' /proc/net/tcp 2>/dev/null; then
  other=running
elif [ -d /media/developer/apps/usr/palm/applications/com.webosbrew.app.voxrelay ] ||
     [ -e /var/lib/webosbrew/init.d/60-voxrelay ]; then
  other=installed
fi
if [ -n "$other" ]; then
  echo "other_voice=voxrelay:$other"
fi

# Wait (briefly) for the daemon to listen, so Settings can load right away.
i=0
while [ "$i" -lt 40 ]; do
  if grep -q "$PORT_LISTEN" /proc/net/tcp 2>/dev/null; then
    echo "enabled listening pid=$(cat /tmp/launch-home-voice.pid 2>/dev/null)"
    exit 0
  fi
  sleep 0.5
  i=$((i + 1))
done
echo "enabled starting"
exit 0
