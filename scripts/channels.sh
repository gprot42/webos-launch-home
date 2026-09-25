#!/bin/sh
# Launch Home: Live TV channels for the Channels chip (run as root via
# hbchannel exec). Uses LG's API adapter, the service LG's phone apps use.
#
#   channels.sh list           one line per channel:
#                              id<TAB>number<TAB>name<TAB>radio<TAB>hidden<TAB>favourite
#   channels.sh now            number<TAB>name<TAB>programme on now
#   channels.sh open ID NUMBER switch Live TV to that channel; prints "opened"
#
# A channel list can be big (a satellite scan has 1000+ channels at about 1 KB
# each), so it is cut down here to one short line per channel.

API=luna://com.webos.service.apiadapter/tv

# One Luna reply on a single line.
call() {
  luna-send -i -n 1 -w "${3:-8000}" "$1" "$2" 2>/dev/null </dev/null | tr -d '\n'
}

# awk helper: the value of "key" in the current line (a JSON fragment): a
# string (without quotes), a number or true/false, or an [array].
AWK_FIELD='
function field(key,    v) {
  if (!match($0, "\"" key "\" *: *(\"[^\"]*\"|\\[[^]]*\\]|[^,}]*)")) return ""
  v = substr($0, RSTART, RLENGTH)
  sub(/^[^:]*: */, "", v)
  gsub(/^"|"$/, "", v)
  return v
}'

case "$1" in
  list)
    # Each channel object starts with "channelId": split there, one per line.
    call "$API/getChannelList" '{}' 15000 | sed 's/"channelId"/\
"channelId"/g' | awk "$AWK_FIELD"'
      /^"channelId"/ {
        id = field("channelId"); num = field("channelNumber"); name = field("channelName")
        if (id == "" || name == "") next
        radio = (field("Radio") == "true") ? 1 : 0
        hidden = (field("Invisible") == "true" || field("skipped") == "true") ? 1 : 0
        fav = field("favoriteGroup")
        fav = (fav != "" && fav != "[]") ? 1 : 0
        gsub(/\t/, " ", name)
        printf "%s\t%s\t%s\t%d\t%d\t%d\n", id, num, name, radio, hidden, fav
      }'
    ;;
  now)
    cur=$(call "$API/getCurrentChannel" '{}')
    prog=$(call "$API/getChannelProgramInfo" '{}')
    printf '%s\n%s\n' "$cur" "$prog" | awk "$AWK_FIELD"'
      NR == 1 { num = field("channelNumber"); name = field("channelName") }
      NR == 2 { prog = field("programName") }
      END { printf "%s\t%s\t%s\n", num, name, prog }'
    ;;
  open)
    # Only characters channel ids and numbers use: nothing else reaches JSON.
    id=$(printf '%s' "$2" | tr -cd 'A-Za-z0-9_.-')
    num=$(printf '%s' "$3" | tr -cd '0-9.-')
    luna-send -n 1 -w 5000 luna://com.webos.applicationManager/launch \
      '{"id":"com.webos.app.livetv"}' >/dev/null 2>&1 </dev/null
    try=0
    while [ "$try" -lt 2 ]; do
      for req in "$API/openChannel {\"channelId\":\"$id\"}" \
                 "$API/openChannel {\"channelNumber\":\"$num\"}" \
                 "luna://com.webos.service.iepg/openChannel {\"channelNumber\":\"$num\"}"; do
        uri=${req%% *}
        body=${req#* }
        case "$body" in *'""'*) continue ;; esac
        if call "$uri" "$body" | grep -q '"returnValue": *true'; then
          echo opened
          exit 0
        fi
      done
      # Live TV may still be starting: give it a moment, then try once more.
      try=$((try + 1))
      sleep 1.5
    done
    echo failed
    exit 1
    ;;
  *)
    echo "usage: channels.sh list | now | open ID NUMBER"
    exit 2
    ;;
esac
