// Copy to secrets.h (git-ignored) and fill in.
#pragma once

#define WIFI_SSID      "your-wifi"
#define WIFI_PASSWORD  "your-password"

// Taco's kitchen Pi and the matching [[audio.satellites]] entry in
// /srv/taco/config.toml.
#define TACO_HOST      "192.168.4.239"
#define TACO_PORT      10700
#define SAT_NAME       "living_room"
#define SAT_TOKEN      "paste-a-long-random-token"

// Mic preamp gain in dB (ES7210 tops out at 37.5). Raise it if Taco
// misses quiet speech from across the room; lower it if audio clips.
#define MIC_GAIN_DB    30.0
