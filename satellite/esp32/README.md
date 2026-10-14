# ESP32 mic satellite (Waveshare ESP32-S3-AUDIO-Board)

This firmware turns the Waveshare ESP32-S3-AUDIO-Board into a Taco mic satellite. It mixes the board's two mics to mono and streams them to taco-voice over Wi-Fi. It uses the same protocol as `../taco_satellite.py`, so nothing changes on the Pi side. The speaker, LED ring and buttons aren't used yet.

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
- The audio isn't encrypted. The token and the `allow` list are the only protection, so keep port 10700 on the LAN.
- Possible next steps on this board:
  - a status/listening light on the LED ring
  - the buttons as a mic mute
  - over-the-air updates
  - playing replies on its speaker (the ES7210 has a speaker loopback channel that echo cancellation could use)
