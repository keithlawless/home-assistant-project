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
 plugs, the Matter server (Docker), Open-Meteo, mpv and PipeWire.
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
USB devices need the power), an Active Cooler or a case with a fan (Whisper
keeps the CPU busy), and a micro-HDMI-to-HDMI cable for the screen.

Optional extras Taco can use: TP-Link Kasa HS103/HS105 smart plugs, Matter
over Wi-Fi plugs and bulbs, a Bluetooth speaker for music, and a NAS with
WebDAV for a music collection.

### Accounts

- **OpenRouter** API key (required). Pick a fast, cheap model for spoken turns.
- Optional: a web-search key for Hermes (Tavily, Exa or Firecrawl), a domain on
  Cloudflare for the notification webhook, and Spotify Premium for Spotify
  Connect.

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
4. Optional: add a web-search backend key so Taco can answer from the web.

You don't need to change Hermes's persona or tools by hand; step 8 does it.

### 6. Start the Matter server

Needed only for Matter devices (Kasa plugs work without it):

```bash
cd ~/taco/deploy && docker-compose up -d
```

### 7. Write `/srv/taco/config.toml`

Every setting has a default in [`taco/config.py`](taco/config.py), which also
documents each one. The file only needs what you change. All passwords, tokens
and private URLs belong here, never in the repo. A starting point:

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
- replaces the desktop with a full-screen Chromium kiosk

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
- **Calendar:** put your calendar's private iCal address in
  `[calendar] ical_urls`.
- **Music collection on a NAS:** set `url`, `user` and `password` under
  `[music]` (WebDAV). Taco indexes it and plays it by artist, album or song.
- **Bluetooth speakers:** pair the speaker once with `bluetoothctl`, then say
  use its name, as in "play the radio on Sonos". Taco's voice stays in the kitchen.
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
taco/core/     FastAPI app: devices, timers, weather, lists, scenes, media, webhook
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
