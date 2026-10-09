#!/usr/bin/env bash
# Restart the kiosk loop and its browser (picks up kiosk.sh changes without a
# reboot). Safe to run over ssh.
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0
pidfile="$XDG_RUNTIME_DIR/taco-kiosk.pid"
[[ -f "$pidfile" ]] && kill "$(cat "$pidfile")" 2>/dev/null
"$(dirname "$0")/reload-ui.sh"
setsid nohup "$(dirname "$0")/labwc/kiosk.sh" >/tmp/taco-kiosk.log 2>&1 </dev/null &
echo "kiosk restarted"
