# Taco mic satellite

The satellite lets Taco hear "Hey Taco" from another room. It streams the room's microphone to taco-voice over the LAN. Wake word, VAD and STT all still run on the kitchen Pi, so the satellite is just a pipe. Replies play in the kitchen.

**Using the Waveshare ESP32-S3-AUDIO-Board?** See [esp32/README.md](esp32/README.md). The rest of this page covers a Linux satellite.

Hardware: a Raspberry Pi Zero 2 W with a USB mic (through a micro-USB OTG adapter) or a ReSpeaker 2-Mic HAT. Any Linux box with `arecord` and Python 3 works too.

## Set up the satellite
1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. Set the hostname (for example `taco-living`), Wi-Fi and SSH there.
2. Find the mic: `arecord -l`. A USB mic shows up as `card 1` or similar, which makes its device `plughw:1,0`.
3. Copy this directory over and install:
   ```sh
   scp -r satellite pi@taco-living.local:
   ssh pi@taco-living.local 'cd satellite && sudo ./install.sh'
   ```
   The first run writes `/etc/taco-satellite.conf` with a random `SAT_TOKEN`. Edit the file to set `SAT_DEVICE`, then run `sudo systemctl restart taco-satellite`.
4. Check that it's running: `journalctl -fu taco-satellite`. It retries until Taco accepts the connection.

## Tell Taco about it
Add an entry to `/srv/taco/config.toml` on the kitchen Pi, using the token from the satellite:
```toml
[[audio.satellites]]
name  = "living room"
port  = 10700
token = "<SAT_TOKEN from /etc/taco-satellite.conf>"
allow = ["192.168.4.0/24"]
```
Then run `systemctl --user restart taco-voice`. The log shows `satellite 'living room': connected from …`, and a wake from that room logs as `wake word (0.62, living room)`.

The token and the allow list are the only protection, and the audio isn't encrypted. Keep the port on the LAN; don't forward it from the router.

## Testing without hardware
Run the satellite on any machine and feed it a recording in place of a mic:
```sh
TACO_HOST=192.168.4.239 SAT_TOKEN=... SAT_NAME=test \
SAT_COMMAND="sox hey-taco.wav -t raw -r 16000 -c 1 -b 16 -e signed - " \
python3 satellite/taco_satellite.py
```
