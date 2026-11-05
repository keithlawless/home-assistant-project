# ESP32 mic satellite (Waveshare ESP32-S3-AUDIO-Board)

This firmware turns the Waveshare ESP32-S3-AUDIO-Board into a Taco mic satellite. It mixes the board's two mics to mono and streams them to taco-voice over Wi-Fi. The ES7210 is read as four 16-bit channels (32-bit stereo I2S): 0 is the speaker loopback, 1 and 3 are the mics, and 2 is unused. Firmware before November 2026 read it as 16-bit stereo, which mixed one mic with the silent loopback. It uses the same protocol as `../taco_satellite.py`, so nothing changes on the Pi side. After the first USB flash, updates go over Wi-Fi (see [Firmware updates over Wi-Fi](#firmware-updates-over-wi-fi)). The LED ring shows when Taco is listening, and the middle key mutes the mic. The speaker isn't used yet.

## One-time setup on the Mac
```sh
brew install platformio
```
The first build downloads the ESP-IDF toolchain into `~/.platformio`, which takes a few minutes and about 1 GB.

The project pins `espressif32@7.1.3` (ESP-IDF 6.1). Older platform releases ship an Intel-only CMake, which fails on Apple Silicon without Rosetta ("Bad CPU type in executable"). If the build fails with `No module named 'intelhex'`, install it into the Python environment that Homebrew's PlatformIO uses: `$(brew --prefix platformio)/libexec/bin/python -m pip install intelhex`. A `brew upgrade platformio` replaces that environment, so run the command again after upgrading.

## 1. Configure the firmware
```sh
cd satellite/esp32
cp main/secrets.example.h main/secrets.h
```
Edit `main/secrets.h` and fill in:
- your Wi-Fi name and password (2.4 GHz; the ESP32 has no 5 GHz radio)
- `TACO_HOST` (the kitchen Pi, 192.168.4.239)
- a long random `SAT_TOKEN`, for example from `openssl rand -base64 18`

`secrets.h` is git-ignored.

## 2. Tell Taco about it
taco-voice only listens on the satellite port once a satellite is configured, so do this before flashing. Add this to `/srv/taco/config.toml` on the kitchen Pi, using the same token as `secrets.h`:
```toml
[[audio.satellites]]
name  = "living room"
port  = 10700
token = "<SAT_TOKEN>"
allow = ["192.168.4.0/24"]
```
Then restart taco-voice and leave its log open in another terminal:
```sh
systemctl --user restart taco-voice
journalctl --user-unit taco-voice -f
```

For a stable setup, give the board a DHCP reservation in the router and narrow `allow` to that one address, for example `["192.168.4.50/32"]`.

## 3. Flash
1. Plug the board into the Mac with a USB-C data cable.
2. Run:
   ```sh
   pio run -t upload
   pio device monitor        # Ctrl-C to quit
   ```
3. If the upload can't find the board, hold **BOOT**, tap **RESET**, release **BOOT**, and run the upload again.

A board flashed with the old single-app layout needs one USB flash to switch. That first flash with the OTA layout should start from a clean chip, because the old Wi-Fi calibration data sits where the OTA state now goes:
```sh
pio run -t erase && pio run -t upload
```
NVS holds only Wi-Fi calibration (credentials are compiled in), so nothing is lost.

## 4. Check it
The monitor should show:
```
mic ready: 16000 Hz, gain 30.0 dB
wifi up, ip 192.168.4.x
streaming to 192.168.4.239:10700
```
These lines print once, at boot. The board uses native USB, so opening the monitor doesn't reset it the way older ESP32 boards reset. If the monitor attaches after boot, it stays blank. Press **RESET** with the monitor open to see them. Once streaming starts, the board logs nothing more. The monitor's "Please build project in debug configuration" notice is harmless.

The taco-voice log on the Pi should show `satellite 'living room': connected from 192.168.4.x`. Saying "Hey Taco" in the living room then logs `wake word (0.xx, living room)`.

If the monitor shows `can't reach Taco at 192.168.4.239:10700; retrying`, nothing on the Pi is listening on that port. Check that the `[[audio.satellites]]` entry is in `config.toml` and that taco-voice was restarted after adding it. The board retries on its own, so you don't need to reflash.

## Tuning
- **Mic gain:** `MIC_GAIN_DB` in `secrets.h`, max 37.5 dB. Raise it if Taco misses quiet speech from the couch; lower it if loud speech distorts.
- To hear what the satellite hears, record from the Pi side with taco-voice stopped. Or temporarily point `TACO_HOST` at your Mac and run `nc -l 10700 > out.raw`, then play it back with `play -t raw -r 16000 -e signed -b 16 -c 1 out.raw` (from sox). The first line of `out.raw` is the handshake text.

## Notes
- Every `LEVELS_EVERY_S` (in `main.c`; 0 turns it off), the board reports each channel's rms and peak dBFS. taco-voice logs it as `channel levels (rms/peak dBFS) …`.
- The audio isn't encrypted. The token and the `allow` list are the only protection, so keep port 10700 on the LAN.
- Possible next steps on this board:
  - playing replies on its speaker (the ES7210 has a speaker loopback channel that echo cancellation could use)

  These are planned in `PLAN.md` under "Future features".

## Firmware updates over Wi-Fi
After one USB flash with this layout (`partitions.csv`: two 4 MB app slots, plus 6 MB kept for speech models), new builds go over Wi-Fi:
```sh
deploy/satellite-ota.sh            # from the repo root; builds and copies to the Pi
```
The script stamps a unique version (date, commit, and "+" for uncommitted changes), builds it, and puts the image in `/srv/taco/satellite/living_room.bin` on the Pi, mode 0600 because it holds the Wi-Fi password and token. No restart is needed.

taco-voice notices the new image within a second and tells the board (`UPDATE`, below). The board also checks at boot and every 15 minutes. It asks taco-voice, on the satellite port with its token, whether there's a build with a different version. If there is, it downloads it, checks its SHA-256, writes the other app slot and restarts. The taco-voice log shows `sending firmware <version>`.

A new image is on trial. Once it has streamed to Taco for 10 s, it's kept (`firmware <version> confirmed` on the monitor). If it crashes, or doesn't reach Taco within 5 minutes, the board goes back to the previous image and won't take that version again. Deploy a fixed build (any new version) to try again.

A reset while an image is still on trial (its first few seconds after an update) also counts as a failure and rolls it back. So after an update, give it about 30 s before pressing **RESET**.

For another satellite, pass its name: `deploy/satellite-ota.sh "bedroom"` serves `/srv/taco/satellite/bedroom.bin`, or set `firmware = "..."` in its `[[audio.satellites]]` entry.

## Control lines from Taco
taco-voice writes short text lines down the satellite's connection, and the board reads them between audio frames. A satellite that doesn't read them (older firmware, `taco_satellite.py`) just ignores them.

| Line | Meaning |
|---|---|
| `STATE idle` | Taco is waiting for the wake word |
| `STATE <listening\|thinking\|speaking> here` | a turn this satellite started (or is recording) |
| `STATE <listening\|thinking\|speaking> elsewhere` | a turn in another room |
| `MUTE on` / `MUTE off` | the kiosk's "stop listening", house-wide. While muted, the board sends silence, so its audio never leaves the room |
| `UPDATE` | new firmware is waiting; check now |

On each connection Taco sends the current `MUTE` and `STATE`, so a board that restarts mid-turn catches up. The monitor logs them (`taco is listening (here)`, `mic muted`).

## Light ring and mute button
The ring lights only for a conversation that started in this room:

| Ring | Meaning |
|---|---|
| off | idle, or a conversation in another room |
| solid blue | listening: speak now |
| cyan dot chasing round | thinking |
| green, breathing | speaking |
| dim red | this mic is muted |

**K2**, the middle key, mutes and unmutes this room's mic. The mute is kept on the board, so it lasts through a reboot or a Pi outage. While muted, the board sends silence. It tells taco-voice with a short `TACO-EVENT <name> <token> MUTE on|off` connection, and the log shows `mute button on (mic muted)`. The kiosk's "stop listening" mutes everything, this mic included, and also turns the ring dim red.

Pins, from Waveshare's demo code: the 7 WS2812 LEDs are on GPIO38 in RGB order. The keys are on the TCA9555 I/O expander (I²C 0x20, on the mic bus): K1 = P9, K2 = P10, K3 = P11, pressed = low. P8 enables the speaker amp. The brightness cap is `RING_MAX` in `main.c`.
