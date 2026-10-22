# Taco

Taco is a do-it-yourself, Echo Show-style kitchen assistant. It runs on a
Raspberry Pi 5 with a touchscreen. You wake it by saying "Hey Taco". Speech
recognition, the wake word, timers and device control all run on the Pi, and
the conversation goes to an LLM agent
([Hermes Agent](https://github.com/NousResearch/hermes-agent)) through
OpenRouter.

What it does:

- **"Hey Taco" wake word** (a custom openWakeWord model) with local speech-to-text (faster-whisper) and a choice of local voices (Piper, Kokoro)
- **Weather home screen** with hourly and 5-day forecasts, NWS severe-weather alerts, a burn-in-safe screensaver, and a night mode that turns the panel off
- **Your own weather station (optional):** live outdoor and indoor readings (Netatmo built in, others can be added), highlighted on screen, with the forecast as a fallback
- **Kitchen timers and alarms** that survive a reboot, plus reminders, and recipes you can step through hands-free
- **Smart home control:** Kasa HS103/HS105 plugs and Matter-over-Wi-Fi devices, with discovery, rooms, scenes and schedules (including sunrise and sunset)
- **Shopping and to-do lists**, a calendar briefing, and web-search answers
- **Music:** internet radio, Spotify Connect, a music collection on a NAS, and "play on Sonos" for any Bluetooth speaker
- **Mic satellites** for other rooms (an ESP32-S3 board that streams its mic to the Pi)
- **Notifications** on screen from Pi health checks, local scripts, or outside senders through a locked-down webhook

For the design and the reasoning behind it, see [PLAN.md](PLAN.md).

## How it fits together

```
 Mic(s) ──► taco-voice: wake word → VAD → Whisper STT
                 │ text
                 ▼
            taco-core (FastAPI :8700) ── fast-path intents (timers, on/off, stop…)
                 │ anything else
                 ▼
            Hermes Agent API (:8642) ──► OpenRouter (LLM)
                 │ tools over MCP (taco-mcp) → back into taco-core
                 ▼
            reply → Piper/Kokoro TTS → speaker

 taco-core also serves the touchscreen UI (Chromium kiosk), talks to Kasa
 plugs, the Matter server (Docker), Open-Meteo, an optional weather station,
 mpv and PipeWire.
```

Four user services run on the Pi: `taco-core`, `taco-voice`, `hermes-gateway`
and `taco-spotify`, plus a Docker container for Matter.

## Shopping list

These are the parts this Taco is built from. Equivalents should work; notes
say what each part is for.

| Part | What it's for |
|---|---|
| **Raspberry Pi 5, 8 GB** | Runs everything. Whisper, the wake word and TTS fit comfortably in 8 GB. |
| **External SSD** (USB 3; 256 GB or more) | The Pi boots from it, and it holds models, voices and data under `/srv/taco`. Much faster and longer-lived than an SD card. |
| **Kogoda 10.1" Touch Screen for Raspberry Pi** | Display, touch input (USB) and built-in speakers (HDMI audio). |
| **EMEET OfficeCore M0 Plus speakerphone** (USB) | The kitchen mic and speaker. Its echo cancellation lets Taco hear "Hey Taco, stop" while it's talking (barge-in). |
| **Waveshare ESP32-S3 AI Smart Speaker Development Board** | Optional. A mic satellite for another room; it streams audio to the Pi over Wi-Fi. One per extra room. |

Also needed: the official Raspberry Pi 27 W USB-C power supply (the SSD and
USB devices need the power). The Kogoda kit comes with every cable the screen
needs; with a different screen you may need a micro-HDMI-to-HDMI cable. No case or cooler is needed: this Pi is mounted bare on the back of the
display and runs fine. Taco's health monitor only warns about heat during
long CPU-heavy jobs, such as running the test suite on the Pi.

Optional extras Taco can use: TP-Link Kasa HS103/HS105 smart plugs, Matter
over Wi-Fi plugs and bulbs, a Bluetooth speaker for music, a NAS with WebDAV
for a music collection, and a home weather station (Netatmo Weather Station,
sold in the EU but no longer in the US, or any other with an API, by adding a
small provider; see the weather station section below for US alternatives).

### Accounts

- **OpenRouter** API key (required). Pick a fast, cheap model for spoken turns.
- Optional: a domain on Cloudflare for the notification webhook, Spotify
  Premium for Spotify Connect, and a free Netatmo developer app if you have a
  Netatmo weather station.

Web search needs no account: with no key set, Hermes searches DuckDuckGo. A
Tavily, Exa or Firecrawl key is an optional upgrade.

## Building your own

Commands below run **on the Pi** unless they say otherwise. Wherever a doc in
this repo uses `192.168.4.239` or `192.168.4.0/24`, that's this build's
network; use your Pi's address and LAN subnet instead.

### 1. Install the OS on the SSD

1. With Raspberry Pi Imager, write **Raspberry Pi OS (64-bit) Bookworm,
   Desktop** directly onto the SSD. In the Imager settings, set the hostname to
   `taco`, create the user **`taco`** (the scripts and services assume this
   user and `~/taco`), set up Wi-Fi, and turn on SSH.
2. Plug the SSD into a blue USB 3 port and boot without an SD card. If the Pi
   doesn't boot from USB, put the OS on an SD card once and run
   `sudo raspi-config` → Advanced Options → Boot Order → USB Boot.
3. In your router, give the Pi a DHCP reservation. Matter needs mDNS and IPv6
   link-local traffic allowed on the LAN, which most home routers do by default.

### 2. Connect the hardware

- Screen: HDMI into the Pi's **HDMI 0** port (next to USB-C power), and the
  screen's touch USB cable into a USB port.
- Speakerphone: USB.
- Check audio: `wpctl status` should list the speakerphone as a source and a
  sink. `arecord -d 3 -f S16_LE -r 16000 test.wav && aplay test.wav` should
  play back what you said. Note the PipeWire node names
  (`pw-cli ls Node | grep node.name`); you'll put them in the config.

### 3. Get the code onto the Pi

On the Pi:

```bash
git clone https://github.com/keithlawless/home-assistant-project.git ~/taco
```

Or, if you develop on another machine, push your working copy with
`TACO_PI=taco@<pi-ip> deploy/sync.sh` from that machine.

### 4. Install system packages

```bash
cd ~/taco
sudo bash deploy/install-system.sh
sudo reboot
```

This installs Docker, mpv, audio libraries and BlueZ, adds `taco` to the
groups it needs, creates `/srv/taco/` and enables lingering, so Taco's user
services start at boot without a login.

### 5. Install uv, Node and Hermes Agent

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # Python package manager
sudo apt-get install -y nodejs npm                    # to build the UI
```

Install Hermes Agent with its official install script for Linux arm64 (see the
Hermes Agent docs). Then:

1. Run `hermes model`, choose the **OpenRouter** provider, and pick a model.
   The key ends up in `~/.hermes/.env` as `OPENROUTER_API_KEY`.
2. Turn on Hermes's OpenAI-compatible API server by adding
   `API_SERVER_ENABLED=true` to `~/.hermes/.env`. It listens on
   `http://127.0.0.1:8642/v1`.
3. Install Hermes's gateway as a systemd user service named `hermes-gateway`
   (Hermes's gateway setup does this), so it starts at boot.
4. Web search works out of the box through DuckDuckGo. Optionally, add a
   Tavily, Exa or Firecrawl key to `~/.hermes/.env` for a paid search backend.

You don't need to change Hermes's persona or tools by hand; step 8 does it.

### 6. Start the Matter server

Needed only for Matter devices (Kasa plugs work without it):

```bash
cd ~/taco/deploy && docker-compose up -d
```

### 7. Write `/srv/taco/config.toml`

This file lives on the Pi, outside the repo, and holds your settings and all
of your secrets: passwords, tokens and private URLs. Never put secrets in
`taco/config.py` or anywhere else in the repo.

[`taco/config.py`](taco/config.py) is only a reference: it lists every
setting with its default and what it does. `config.toml` needs just the
settings you change. A starting point:

```toml
[audio]
# Node names from `pw-cli ls Node`; leave out to use the PipeWire default.
input_device = "<speakerphone source node name>"
output_device = "<speakerphone sink node name>"
barge_in = true          # the speakerphone cancels Taco's own voice

[wake]
model = "/srv/taco/models/wakeword/hey_taco.onnx"
```

Copy the wake-word model into place:

```bash
mkdir -p /srv/taco/models/wakeword
cp ~/taco/wakeword/hey_taco.onnx /srv/taco/models/wakeword/
```

Set your town, voice and screensaver timeout later on the Settings screen.

### 8. Build the UI and install Taco

```bash
cd ~/taco/ui && npm ci && npm run build      # builds ui/dist, which taco-core serves
cd ~/taco && deploy/install-user.sh
sudo reboot
```

`install-user.sh` is safe to re-run after every update. It:

- installs Python dependencies into `~/taco/.venv`
- gives Hermes Taco's persona ([`hermes/SOUL.md`](hermes/SOUL.md)) and tools ([`hermes/configure.sh`](hermes/configure.sh))
- installs the PipeWire and WirePlumber config and librespot (Spotify Connect)
- installs and starts the systemd user services
- replaces the desktop with a full-screen Chromium kiosk, and turns off labwc's touch mouse emulation so swiping scrolls ([`deploy/labwc/touch.sh`](deploy/labwc/touch.sh))

After the reboot the screen shows Taco's home screen. The first start
downloads the Whisper and Piper models, which takes a few minutes. Check on
things with:

```bash
systemctl --user status taco-core taco-voice hermes-gateway
journalctl --user-unit taco-voice -f
```

Say **"Hey Taco, what's the weather?"**

### 9. Add devices

Say "Hey Taco, find new devices", or use **Devices → Add device** on the
screen. Taco scans for Kasa plugs and asks what to call each one and which room
it's in. For Matter, give it the device's pairing code. Brand-new devices are
set up over Bluetooth and need `wifi_ssid` and `wifi_password` under
`[matter]` in `config.toml`; devices already in Apple Home, Google Home or
Alexa can be shared from that app with a pairing code.

Then try "turn off the living room", "set a pasta timer for 9 minutes", or
"add eggs to the shopping list".

### 10. Optional extras

- **Your own wake word:** `hey_taco.onnx` is in the repo. To train a different
  phrase, or to tune false wakes from a TV, see [`wakeword/README.md`](wakeword/README.md).
- **A mic in another room:** flash the Waveshare ESP32-S3 board; see
  [`satellite/esp32/README.md`](satellite/esp32/README.md). Any Linux box with
  a mic can be a satellite too ([`satellite/README.md`](satellite/README.md)).
- **Notifications from outside** (for example, camera motion through an Alexa
  routine): a Cloudflare tunnel that exposes only the webhook; see
  [`deploy/cloudflared/README.md`](deploy/cloudflared/README.md).
- **A weather station at home:** Taco uses every reading your station has
  (outdoor temperature, humidity, wind) for the current conditions, shows them
  in brighter text with a dot next to the town name, and adds an "Inside" line
  (temperature, humidity, CO₂). Anything the station doesn't measure, and the
  whole forecast, still comes from Open-Meteo, which also takes over if the
  station is offline or its reading is over 30 minutes old. Say "what's the
  temperature inside?" for the indoor reading.

  Netatmo no longer sells its weather stations in the United States, but they
  should still be available in the EU. In the US, these stations have APIs
  that a small provider could use (only Netatmo is built in so far; see
  "Adding another station" in the stations README):
  - **Ecowitt** (for example, a GW2000 or GW1100 gateway with its sensors):
    serves readings over a local HTTP API on your LAN, with no cloud account.
  - **WeatherFlow Tempest:** a cloud REST API with a personal access token,
    and it also broadcasts readings on the LAN.
  - **Ambient Weather** (for example, the WS-2902): a cloud REST API with an
    API key and application key.
  - **Davis WeatherLink Live:** a local HTTP API on the LAN, plus Davis's
    cloud API.

  For a Netatmo station:
  1. At [dev.netatmo.com](https://dev.netatmo.com), create an app under
     **My apps**, then use its **Token generator** with the scope
     `read_station`.
  2. Put the app's client ID and secret and the generated refresh token in
     `config.toml`:

     ```toml
     [weather_station]
     provider = "netatmo"

     [weather_station.netatmo]
     client_id = "..."
     client_secret = "..."
     refresh_token = "..."
     ```
  3. Restart taco-core (`systemctl --user restart taco-core`) and check
     `curl -s localhost:8700/api/station`.

  Netatmo replaces the refresh token each time it's used, so Taco keeps the
  live tokens in `/srv/taco/data/oauth/` (readable only by `taco`); if access
  is ever lost, generate a new refresh token and put it in `config.toml`.
  Taco checks every 10 minutes, well under Netatmo's 500 requests an hour. To
  add a different station, or for more detail, see
  [`taco/core/stations/README.md`](taco/core/stations/README.md).
- **Calendar:** put your calendar's private iCal address in
  `[calendar] ical_urls`.
- **Music collection on a NAS:** set `url`, `user` and `password` under
  `[music]` (WebDAV). Taco indexes it and plays it by artist, album or song.
- **Bluetooth speakers:** pair the speaker once with `bluetoothctl`, then say
  "play the radio on Sonos", using its name. Taco's voice stays in the kitchen.
- **Spotify Connect:** Taco shows up as a speaker named "Taco" in the Spotify
  app.

## Developing

Edit on a laptop, then push to the Pi with `deploy/sync.sh` and run
`deploy/install-user.sh` there.

```bash
uv run --extra dev pytest          # unit tests; fake Kasa/Matter backends
cd ui && npm run dev                # UI dev server; see ui/vite.config.js for
                                    # tunnelling the API from the Pi
```

`deploy/reload-ui.sh` restarts the kiosk browser after a UI deploy, and
`deploy/ui-shot.py` takes a screenshot of it.

## Repository layout

```
taco/core/     FastAPI app: devices, timers, weather (+ stations/), lists, scenes, media, webhook, OAuth
taco/voice/    wake word, VAD, speech-to-text, TTS, satellites, Hermes client
taco/mcp/      taco-mcp, the tools Hermes uses to act on Taco
ui/            Svelte kiosk UI
hermes/        Taco's persona and Hermes settings
wakeword/      the "Hey Taco" model and how to train one
satellite/     mic satellites (ESP32-S3 firmware, Linux client)
deploy/        install scripts, systemd units, kiosk, PipeWire, Matter, tunnel
tests/         pytest unit tests
```

## License

[MIT](LICENSE)
