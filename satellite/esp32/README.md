# ESP32 mic satellite (Waveshare ESP32-S3-AUDIO-Board)

This firmware turns the Waveshare ESP32-S3-AUDIO-Board into a Taco mic satellite. It mixes the board's two mics to mono and streams them to taco-voice over Wi-Fi. It uses the same protocol as `../taco_satellite.py`, so nothing changes on the Pi side. The speaker, LED ring and buttons aren't used yet.

## One-time setup on the Mac
```sh
brew install platformio
```
The first build downloads the ESP-IDF toolchain into `~/.platformio`, which takes a few minutes and about 1 GB.

The project pins `espressif32@7.1.3` (ESP-IDF 6.1). Older platform releases ship an Intel-only CMake, which fails on Apple Silicon without Rosetta ("Bad CPU type in executable"). If a build fails with `No module named 'intelhex'`, install esptool into PlatformIO's Python environment, for example `$(brew --prefix platformio)/libexec/bin/pip install esptool`.

## Configure
```sh
cd satellite/esp32
cp main/secrets.example.h main/secrets.h
```
Edit `main/secrets.h` and fill in:
- your Wi-Fi name and password (2.4 GHz; the ESP32 has no 5 GHz radio)
- `TACO_HOST` (the kitchen Pi, 192.168.4.239)
- a long random `SAT_TOKEN`, for example from `openssl rand -base64 18`

`secrets.h` is git-ignored.

## Flash
1. Plug the board into the Mac with a USB-C data cable.
2. Run:
   ```sh
   pio run -t upload
   pio device monitor        # Ctrl-C to quit
   ```
3. If the upload can't find the board, hold **BOOT**, tap **RESET**, release **BOOT**, and run the upload again.

The monitor should show:
```
mic ready: 16000 Hz, gain 30.0 dB
wifi up, ip 192.168.4.x
streaming to 192.168.4.239:10700
```
Until Taco is configured, the last line reads `can't reach Taco … retrying`.

## Tell Taco about it
Add this to `/srv/taco/config.toml` on the kitchen Pi. Use the same token as `secrets.h`.
```toml
[[audio.satellites]]
name  = "living room"
port  = 10700
token = "<SAT_TOKEN>"
allow = ["192.168.4.0/24"]
```
Then run `systemctl --user restart taco-voice`. Its log should show `satellite 'living room': connected from 192.168.4.x`. Saying "Hey Taco" in the living room then logs `wake word (0.xx, living room)`.

For a stable setup, give the board a DHCP reservation in the router and narrow `allow` to that one address, for example `["192.168.4.50/32"]`.

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
