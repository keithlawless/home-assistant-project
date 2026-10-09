#!/usr/bin/env bash
# Show the Taco UI full-screen, restarting Chromium if it ever exits.
URL="http://localhost:8700/"
echo $$ > "${XDG_RUNTIME_DIR:-/tmp}/taco-kiosk.pid"

until curl -sf "$URL" >/dev/null; do sleep 1; done

while true; do
  chromium \
    --kiosk --incognito --noerrdialogs --disable-infobars \
    --disable-session-crashed-bubble --disable-features=TranslateUI \
    --check-for-update-interval=31536000 \
    --ozone-platform=wayland --touch-events=enabled \
    --disable-pinch --overscroll-history-navigation=0 \
    --password-store=basic \
    --autoplay-policy=no-user-gesture-required \
    --remote-debugging-port=9222 \
    "$URL"
  sleep 2
done
