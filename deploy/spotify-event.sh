#!/bin/sh
# librespot --onevent hook: forward playback events to taco-core so the screen
# can show what's playing.
jq -n --arg event "$PLAYER_EVENT" --arg name "$NAME" --arg artists "$ARTISTS" \
  '{event: $event, name: $name, artists: ($artists | split("\n") | join(", "))}' |
  curl -s -m 2 -X POST -H 'Content-Type: application/json' --data @- \
    http://127.0.0.1:8700/api/media/spotify >/dev/null 2>&1
exit 0
