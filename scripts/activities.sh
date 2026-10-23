#!/bin/sh
# Launch Home: persistent SAM activities (run as root via hbchannel exec).
#
#   activities.sh arm boot|home      create it, unless it is already there
#   activities.sh disarm boot|home   cancel every copy of it
#   activities.sh list               "<id> <name>" for each of ours
#
# An activity is webOS's own scheduler: once created it survives reboots and
# runs with nothing of ours running, even on TVs where init.d hooks don't run.
#   boot  when the TV finishes booting (core-boot-done), launch Launch Home
#   home  when LG's Home (com.webos.app.home) comes to the front, launch
#         Launch Home
# Launch only, never close LG's Home: an activity outlives the app, and one
# that closes Home with Launch Home uninstalled leaves the TV with no home
# screen at all. Launching was enough for the remote to follow (webOS 25).
#
# Every luna-send run is a different creator (com.webos.lunasend-<pid>), and
# an activity belongs to its creator: cancelling by name from a later run
# finds nothing, and creating again with "replace" adds a duplicate. So ours
# are found by name in the full list and cancelled by id.
#
# Arming fires the boot activity at once (the TV has already booted), which
# just relaunches Launch Home, already in front when its Settings arm it.

APP_ID="org.webosbrew.lounge.launcher"
APP_RE="org\\.webosbrew\\.lounge\\.launcher"
AM="luna://com.webos.service.activitymanager"

# One reply, compact JSON (no -f: the list is parsed line by line below).
luna() {
  out=$(luna-send -i -n 1 -w 5000 "$1" "$2" 2>/dev/null) && [ -n "$out" ] && {
    printf '%s\n' "$out"
    return 0
  }
  luna-send -n 1 "$1" "$2" 2>/dev/null
}

# "<id> <name>" for each of our activities. Each activity in the list starts
# with "activityId", so one per line from there, then its id and name.
list_ours() {
  luna "$AM/list" '{"details":true,"subscribers":true,"current":true,"internal":true}' \
    | sed 's/"activityId":/\
/g' \
    | sed -n "s/^\([0-9][0-9]*\),.*\"name\":\"\(${APP_RE}\.[a-z]*\)\".*/\1 \2/p"
}

ids_of() {
  list_ours | awk -v name="$APP_ID.$1" '$2 == name { print $1 }'
}

cancel_id() {
  luna "$AM/cancel" "{\"activityId\":$1}" >/dev/null
}

definition() {
  case $1 in
    boot)
      desc="Launch Home: open at boot"
      trigger='{"method":"luna://com.webos.bootManager/getBootStatus","params":{"subscribe":true},"where":{"prop":["signals","core-boot-done"],"op":"=","val":true}}'
      ;;
    home)
      desc="Launch Home: open when LG Home appears"
      trigger='{"method":"luna://com.webos.applicationManager/getForegroundAppInfo","params":{"subscribe":true},"where":{"prop":["appId"],"op":"=","val":"com.webos.app.home"}}'
      ;;
  esac
  printf '{"activity":{"name":"%s.%s","description":"%s","type":{"foreground":true,"persist":true,"continuous":true},"trigger":%s,"callback":{"method":"luna://com.webos.applicationManager/launch","params":{"id":"%s"}}},"start":true}' \
    "$APP_ID" "$1" "$desc" "$trigger" "$APP_ID"
}

arm() {
  kind=$1
  # shellcheck disable=SC2046 # one id per word
  set -- $(ids_of "$kind")
  if [ "$#" -gt 0 ]; then
    keep=$1
    shift
    for id in "$@"; do
      cancel_id "$id"
    done
    echo "armed $kind id=$keep"
    return 0
  fi
  out=$(luna "$AM/create" "$(definition "$kind")")
  id=$(printf '%s' "$out" | sed -n 's/.*"activityId":\([0-9][0-9]*\).*/\1/p')
  if [ -z "$id" ]; then
    echo "arm_failed $kind $(printf '%s' "$out" | head -c 200)"
    return 1
  fi
  echo "armed $kind id=$id"
}

disarm() {
  for id in $(ids_of "$1"); do
    cancel_id "$id"
  done
  echo "disarmed $1"
}

case "$1 $2" in
  "arm boot"|"arm home") arm "$2" ;;
  "disarm boot"|"disarm home") disarm "$2" ;;
  "list "*) list_ours ;;
  *)
    echo "usage: $0 arm|disarm boot|home, or $0 list" >&2
    exit 2
    ;;
esac
