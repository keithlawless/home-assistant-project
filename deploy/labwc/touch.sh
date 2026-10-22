#!/usr/bin/env bash
# Turn off labwc's touch mouse emulation, so Chromium gets real touch events
# and swiping scrolls. (With emulation on, a drag is a mouse drag, which
# doesn't scroll.) Edits the existing rc.xml in place and keeps its other
# settings:
#   - every <touch> entry with mouseEmulation on (Pi OS's screen setup writes
#     one per touchscreen, named down to the USB port) gets it turned off
#   - a <touch> entry with no deviceName is added; labwc applies it to any
#     touchscreen without its own entry, so moving the cable to another port
#     still works
# Prints "changed" if it rewrote the file. Called by deploy/install-user.sh.
set -euo pipefail
rc="${1:-$HOME/.config/labwc/rc.xml}"

mkdir -p "$(dirname "$rc")"
existed=$([[ -f "$rc" ]] && echo yes || true)
[[ -n "$existed" ]] || printf '<?xml version="1.0"?>\n<openbox_config xmlns="http://openbox.org/3.4/rc">\n</openbox_config>\n' > "$rc"

new=$(mktemp)
sed -E 's/(<touch[^>]*mouseEmulation=")(yes|true)"/\1no"/' "$rc" > "$new"
grep -q '<touch mouseEmulation="no"' "$new" ||
  sed -i 's|</openbox_config>|\t<touch mouseEmulation="no"/>\n</openbox_config>|' "$new"

if cmp -s "$new" "$rc"; then
  rm "$new"
else
  [[ -z "$existed" || -f "$rc.orig" ]] || cp "$rc" "$rc.orig"
  cat "$new" > "$rc" && rm "$new"
  echo changed
fi
