#!/usr/bin/env bash
# Switch an ESP32 satellite's speech front end (ESP-SR AFE source separation)
# on or off, for A/B testing (satellite/esp32/README.md). taco-voice notices
# within a second and tells the board, which restarts with the new setting
# (kept in its NVS) and reports it in its handshake; clips record `afe`.
#
#   deploy/satellite-afe.sh on|off|status [satellite name]   # default "living room"
set -euo pipefail
PI="${TACO_PI:-taco@192.168.4.239}"
WANT="${1:-status}"
NAME="${2:-living room}"
FILE="/srv/taco/satellite/${NAME// /_}.afe"

case "$WANT" in
  on|off)
    ssh "$PI" "install -d -m 700 /srv/taco/satellite && echo $WANT > '$FILE'"
    echo "Asked for the '$NAME' front end $WANT; the board restarts in a few seconds."
    ;;
  status) ;;
  *) echo "usage: $0 on|off|status [satellite name]" >&2; exit 2 ;;
esac
ssh "$PI" "cat '$FILE' 2>/dev/null | sed 's/^/wanted: /'; journalctl --user-unit taco-voice --no-pager -n 2000 | grep -E \"satellite '$NAME': (connected|switching)\" | tail -2"
