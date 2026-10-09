#!/usr/bin/env bash
# Restart the kiosk browser so it loads a freshly deployed UI. kiosk.sh
# relaunches Chromium within a couple of seconds. (Debian's wrapper puts its
# own flags first, so match the real binary with --kiosk anywhere after it.)
pkill -f '^/usr/lib/chromium/chromium .*--kiosk' || true
