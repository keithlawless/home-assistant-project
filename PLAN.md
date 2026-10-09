# Taco: Kitchen Voice Assistant & Smart Display (Raspberry Pi 5)

## Context
The goal is a self-built, Echo Show-style assistant called **Taco**. It lives in the kitchen and controls the main living spaces. It runs on a Raspberry Pi 5 (8 GB) that boots from the 512 GB SSD, with a Kogoda touchscreen used for display, touch and speakers. Its brain is **Hermes Agent** (Nous Research, open source), and **all LLM inference goes through OpenRouter**. The project directory `~/code/home-assistant-project` is empty, so this is a greenfield build. Development happens on the Mac and deploys to the Pi.

Required features: the "Hey Taco" wake word, a weather home screen, a screensaver that wakes on touch, web-search answers, Kasa HS103/HS105 plugs, Matter-over-Wi-Fi devices with discovery, a device registry and voice control, kitchen timers, and natural TTS with several voices to choose from.

## Hardware prerequisites (gaps to fill)
- **Microphone (required):** Kogoda monitors have speakers but no mic. Add a USB far-field mic. Recommended: **ReSpeaker Lite (USB, 2-mic, with onboard AEC)** or a USB conference speakerphone such as the Anker PowerConf. Echo cancellation matters so Taco can hear "stop" over its own voice.
- **Current mic:** a directional mic, which has no echo cancellation. A USB speakerphone will replace it later. Until then, the voice loop runs **half-duplex**: wake detection pauses while Taco is speaking, and the touchscreen or a short "stop" window right after speech handles interrupts. Audio devices are chosen by name in config, so swapping in the speakerphone is a config change. Full barge-in gets switched on once the speakerphone is in.
- **Audio out:** HDMI audio to the monitor speakers, or the speakerphone if one is used.
- **Boot from SSD:** flash Raspberry Pi OS Bookworm 64-bit (Desktop, Wayland/labwc) directly onto the SSD and set the EEPROM boot order to USB/NVMe first.
- **Bluetooth:** use the Pi 5's onboard radio. Matter commissions new devices over BLE.
- **Network:** the router must allow IPv6 link-local and mDNS on the LAN, which Matter needs. The Pi ("taco") has a reserved IP of **192.168.4.239** on the 192.168.4.x LAN. Deploy targets (rsync/ssh) and Kasa discovery broadcasts use this subnet.

## Architecture
```
            ┌──────────────── Raspberry Pi 5 ─────────────────────────────┐
 Mic ──►  taco-voice (Python)                                              │
          openWakeWord("hey_taco") → VAD → faster-whisper STT              │
                │ text                                                     │
                ▼                                                          │
          taco-core (FastAPI, :8700) ── fast-path intent router ──┐        │
                │ (complex/unknown requests)                      │        │
                ▼                                                 ▼        │
          Hermes Agent gateway + API server (:8642, OpenAI-compatible)     │
                │  LLM calls ─────────────► OpenRouter                     │
                │  MCP tools ─────────────► taco-mcp (home, timers, lists, │
                │                            display, media)               │
                │  built-in web search / skills / memory / cron            │
                ▼                                                          │
          reply text → TTS (Piper / Kokoro / cloud) → speaker              │
                                                                           │
          taco-core ◄──► SQLite registry (/srv/taco/taco.db)               │
                    ◄──► python-kasa (HS103/HS105, LAN)                    │
                    ◄──► python-matter-server (Docker, ws://:5580)         │
                    ◄──► Open-Meteo weather                                │
                    ──WebSocket──► taco-ui (Chromium kiosk on touchscreen) │
            └──────────────────────────────────────────────────────────────┘
```
Key design decisions:
- **Hermes is the brain, not the voice loop.** Hermes has a built-in voice mode and wake word, but they are tied to its CLI and desktop app. Taco runs its own always-on voice daemon and talks to Hermes through its **API server** (`API_SERVER_ENABLED=true`, `http://127.0.0.1:8642/v1`). That gives full control over the wake word, the screen and barge-in, while Hermes still provides its agent loop, memory, skills, cron, web tools and MCP.
- **Taco's own capabilities go to Hermes as one MCP server (`taco-mcp`).** It is a thin layer over taco-core's REST API, so the voice path, the touchscreen UI and the LLM all act on the same state.
- **A fast path for latency and reliability.** A small local intent router (regex/grammar) handles "turn on/off X", "set a timer for N minutes", "stop", "volume up" and "what time is it" without an LLM round trip. Everything else goes to Hermes. Timers and alarms never depend on the network.
- **A fast voice model:** set Hermes `model` and `auxiliary.voice_chat` to a fast, cheap OpenRouter model for spoken turns, for example a Haiku- or Flash-class model. Spoken replies are capped at 1–3 sentences by Taco's personality prompt.

## Repository layout (`~/code/home-assistant-project`)
```
taco/
  core/            FastAPI app: registry, devices, timers, weather, lists, scenes, WebSocket hub
    devices/       kasa.py, matter.py, base.py (Device protocol: on/off/level/color/state)
    intents.py     fast-path router
    timers.py      persistent timers/alarms (survive restart)
    weather.py     Open-Meteo client + cache
    db.py          SQLite (SQLModel): devices, rooms, aliases, scenes, timers, lists, settings
  voice/           wake word, VAD, STT, TTS engines, audio I/O, barge-in, earcons
    tts/           piper.py, kokoro.py, cloud.py (common interface + voice catalog)
  mcp/             taco-mcp server (stdio) exposing tools to Hermes
  ui/              Svelte + Vite kiosk app (weather, timers, devices, now-playing, screensaver)
hermes/
  config.yaml      model/provider (OpenRouter), MCP servers, web tools, voice off
  SOUL/persona     Taco personality and spoken-style instructions
  skills/          Taco skills (SKILL.md, agentskills.io format): kitchen, home-control, recipes…
wakeword/          training notes + hey_taco.onnx (openWakeWord custom model)
deploy/
  systemd/         taco-core, taco-voice, hermes-gateway, matter-server units
  labwc/autostart  Chromium --kiosk http://localhost:8700
  install.sh       provisioning script (apt deps, uv, docker, models, voices)
  docker-compose.yml  python-matter-server (host network)
tests/             unit tests with fake Kasa/Matter backends
```
Runtime data lives on the SSD under `/srv/taco/`: the DB, models, voices, logs and Hermes home (`~/.hermes`).

## Components in detail

### 1. Wake word: "Hey Taco"
- **openWakeWord** (ONNX, light on Pi CPU). Train a custom `hey_taco` model with openWakeWord's synthetic-speech training notebook (Colab/GPU, about an hour) and ship the `.onnx` in `wakeword/`.
- Until it is trained, use the built-in "hey jarvis" model as a placeholder.
- Tune the threshold and add a false-reject/false-accept test harness that replays recorded kitchen audio.
- On detection: chime, the UI shows a listening overlay, music ducks, and the screensaver wakes.

### 2. Speech-to-text
- **faster-whisper `base.en` int8** runs locally on the Pi, with Silero VAD to detect the end of speech.
- Optional cloud fallback is Groq Whisper. It isn't LLM inference, but it is off by default to keep everything local.

### 3. Text-to-speech (voice options)
A common `TTSEngine` interface streams sentence by sentence, so speech starts before the full reply is done. Voice is selectable by voice ("Taco, change your voice") or in Settings.
- **Piper (default, local, fast):** a curated set of 4–6 voices, for example `en_US-amy`, `en_US-ryan-high`, `en_US-lessac`, `en_GB-alba` and `en_US-libritts_r` speakers.
- **Kokoro-82M (local, most natural, slower):** about 10 voices (`af_heart`, `am_michael`, `bf_emma`, …). Benchmark it on the Pi. If its real-time factor is too slow for long replies, use it only for short ones.
- **Cloud (optional):** ElevenLabs or OpenAI TTS for premium voices, behind an API key setting.
- The UI has a voice preview button.

### 4. Hermes Agent configuration
- Install via the official script on Pi OS arm64. Run `hermes model`, pick the OpenRouter provider, and store `OPENROUTER_API_KEY` in `~/.hermes/.env`.
- Enable the API server and run `hermes gateway` as a systemd service.
- MCP: register `taco-mcp` (stdio) and filter its toolset to Taco's tools plus web search.
- **Web search:** prefer Hermes's web tools with a search backend (Tavily, Exa or Firecrawl key). The alternative that keeps everything in OpenRouter is a model with OpenRouter's `:online` web plugin. Choose during Phase 3 based on cost and answer quality.
- **Persona (SOUL/system prompt):** "You are Taco, a friendly kitchen assistant. Replies are spoken aloud: be brief, no markdown, no lists unless asked." It includes the room and device context the MCP tools provide.
- Disable risky toolsets such as terminal and file write for the voice-facing session. Hermes's memory and skills stay on, so Taco learns household preferences.
- Optional: the Hermes **Telegram gateway** for remote control ("Taco, turn off the living room lamp" from your phone). It comes for free with Hermes.

### 5. Device support and registry
- **Kasa HS103/HS105** via `python-kasa`: `Discover.discover()` on a broadcast, then on/off/state. No cloud is needed. Note that newer firmware may need the KLAP auth credentials (TP-Link account), which go in settings.
- **Matter over Wi-Fi** via **python-matter-server** (Docker, host networking, WebSocket API):
  - Commissioning uses `commission_with_code` with the QR or 11-digit setup code. New devices go through BLE with the Wi-Fi credentials set via `set_wifi_credentials`. Devices already in Apple Home, Google Home or Alexa join through multi-admin (pairing code).
  - Supported clusters: OnOff, LevelControl (dimmers), ColorControl (bulbs), plus read-only sensors (temperature, contact, occupancy) and plugs with energy reporting where present.
- **Registry (SQLite):** device id, protocol, native address/node id, friendly name, aliases, room, type and capabilities, last seen. Rooms are seeded with Kitchen, Living Room, Dining Room and Family Room, and are editable.
- **Discovery flow, voice:** "Hey Taco, find new devices" → scan Kasa plus Matter → "I found 2 new plugs. What should I call the first one?" → name and room assigned conversationally through the MCP tools.
- **Discovery flow, touch:** Devices screen → "Add device" → scan, or enter a Matter code (or use the camera if one is added later).
- **Groups and room control:** "Turn off the living room" acts on all devices in that room.
- A periodic state poller pushes updates to the UI over WebSocket.

### 6. taco-mcp tools (exposed to Hermes)
- `list_devices`, `get_device_state`, `set_power`, `set_level`, `set_color`, `control_room`
- `discover_devices`, `add_device`, `commission_matter`, `rename_device`, `assign_room`, `remove_device`
- `create_scene`, `run_scene`, `list_scenes`
- `set_timer`, `list_timers`, `cancel_timer`, `set_alarm`, `set_reminder`
- `get_weather`, `get_forecast`
- `list_add`, `list_remove`, `list_read`, `list_clear` (shopping/to-do)
- `show_on_screen` (cards: recipe step, conversion, search result, image), `set_volume`, `set_voice`
- `play_media`, `stop_media`

### 7. Display UI (Chromium kiosk, Svelte)
- **Home (default):** large current weather (temp, condition icon, feels-like, hi/lo), hourly strip, 5-day forecast, clock and date. Active timers are pinned as countdown chips.
- **Other screens:** Devices (room tiles with toggles and dimmers), Timers, Lists, Now Playing, Settings (voice, wake sensitivity, location, screensaver timeout, night mode).
- **Conversation overlay:** live transcript plus Taco's reply card, which auto-dismisses back to Home.
- **Screensaver / burn-in protection:**
  - After N minutes idle (default 5), switch to a near-black screen with a dim clock that drifts slowly. An optional photo-frame mode crossfades photos from `/srv/taco/photos`.
  - All static UI elements also get a ±few-pixel periodic shift.
  - Touch, the wake word or a timer alarm dismisses it. The touch that wakes the screen is swallowed so it doesn't trigger a button.
  - **Night mode** (scheduled, or sunset/sunrise): the panel goes fully off via `wlr-randr --output HDMI-A-1 --off`. Touch still wakes it (verify that the Kogoda touch controller stays powered when the panel is off). Otherwise fall back to black plus minimum brightness.

### 8. Kitchen skills (required plus planned)
- **Timers (required):** multiple named timers ("pasta timer for 9 minutes"), add or subtract time, "how much time is left", and alarms that repeat until "stop" or a touch. Timers persist across reboot.
- Planned features:
  - **Recipe mode:** hands-free step-through ("next step", "repeat that") with the current step shown large. Ingredients can be scaled.
  - **Conversions and cooking facts:** units, substitutions, safe internal temperatures (LLM, with the answer shown on screen).
  - **Shopping list / to-do lists:** added by voice and shown on screen. Optional sync to phone via the Telegram gateway or a shared list.
  - **Meal planning and pantry notes** (Hermes memory/skill).

### 9. Home and household skills (planned)
- **Scenes and routines:**
  - "Good morning": lights on, weather plus calendar briefing.
  - "Movie time": dim the living room.
  - "Good night": everything off, screen to night mode.
  - "I'm leaving": all off.
- **Schedules and automations:** sunrise/sunset triggers, device on/off schedules, and **vacation mode** (randomized lights). These run in taco-core's local scheduler so they work without the LLM.
- **Reminders and alarms:** one-shot and recurring, via the local scheduler. Hermes cron handles AI-generated briefings.
- **Morning briefing:** weather, calendar (Google Calendar MCP/skill), news headlines.
- **Music and radio:** internet radio via `mpv` (TuneIn/Radio Browser), plus a Spotify Connect endpoint via `raspotify`. Music ducks on wake.
- **General Q&A and web search** (required), including "what's on the screen" follow-ups.
- **Severe weather alerts:** NWS alerts pushed to the screen and spoken.
- **Intercom/announcements** via the Telegram gateway ("tell the kitchen dinner's ready").
- **Volume, mute, privacy:** a "stop listening" mic mute with an on-screen indicator.

## Phased implementation
1. **Phase 0: Platform.** SSD boot, OS, audio in/out verified (`arecord`/`aplay`), Docker, `uv`, Hermes installed with OpenRouter, API server reachable. Run `install.sh` and the systemd units.
2. **Phase 1: Voice loop MVP.** Wake word (placeholder model) → STT → Hermes API → Piper TTS. Measure latency (target: under 3 s to first audio for simple questions).
3. **Phase 2: UI and weather.** taco-core, the Svelte kiosk, the weather home screen, the screensaver, the conversation overlay, autostart.
4. **Phase 3: Timers, web search, fast-path router.** taco-mcp with timer tools. Pick the web search backend.
5. **Phase 4: Devices.** Kasa discovery and control, then python-matter-server and commissioning, the registry, rooms, voice naming flow, Devices screen.
6. **Phase 5: Voices and wake word.** Train `hey_taco`, add Kokoro and cloud voice options, the voice picker, barge-in tuning.
7. **Phase 6: Planned skills.** Scenes, schedules, lists, recipe mode, briefing, music, night mode, Telegram.

## Verification
- **Unit tests (pytest) on the Mac:** intent router parsing, timer persistence and expiry, registry CRUD, fake Kasa/Matter backends for device and room commands, MCP tool schemas.
- **Integration:** run taco-core plus the UI locally on the Mac (`uv run taco-core`, `npm run dev`) against mock devices, and drive the UI in a browser to check the home screen, screensaver timeout and touch wake.
- **Hermes:** `curl http://127.0.0.1:8642/v1/chat/completions` with "turn on the kitchen plug". Confirm the MCP tool call in Hermes logs and the plug actually switching.
- **On the Pi, end to end:**
  - Say "Hey Taco, what's the weather" → spoken answer, weather card shown.
  - "Set a pasta timer for 1 minute" → countdown chip, alarm, "stop".
  - "Find new devices" → HS103 and HS105 found, named and assigned to rooms → "turn off the living room".
  - Commission one Matter plug by code.
  - "Who won the World Series last year" → a web-search-backed answer.
  - Change the voice and hear the new one.
  - Leave the screen idle → screensaver after the timeout → touch wakes it without triggering a button.
- **Soak test:** run for 48 h while checking for memory growth, wake-word false accepts per hour and recovery after a Wi-Fi drop or reboot.
