#!/usr/bin/env bash
# Build the ESP32 satellite firmware on this Mac and hand it to taco-voice
# on the Pi, which serves it to the board over Wi-Fi (satellite/esp32/README.md).
# The board checks at boot and every 15 minutes; press RESET to check now.
#
#   deploy/satellite-ota.sh [satellite name]     # default "living room"
#
# The image has the Wi-Fi password and satellite token compiled in, so it is
# kept mode 0600 on the Pi and never committed.
set -euo pipefail
PI="${TACO_PI:-taco@192.168.4.239}"
NAME="${1:-living room}"
FILE="${NAME// /_}.bin"
cd "$(dirname "$0")/../satellite/esp32"

# A unique version per build: the board takes any image whose version differs
# from its own. "+" marks uncommitted changes to the firmware.
VERSION="$(date +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD)"
[ -n "$(git status --porcelain -- .)" ] && VERSION="$VERSION+"
echo "$VERSION" > version.txt
touch CMakeLists.txt   # version.txt alone doesn't make CMake look again
pio run
BIN=.pio/build/waveshare-s3-audio/firmware.bin

BUILT="$(python3 -c 'import sys; b = open(sys.argv[1], "rb").read(80); print(b[48:80].split(b"\0")[0].decode())' "$BIN")"
if [ "$BUILT" != "$VERSION" ]; then
  echo "built image says $BUILT, expected $VERSION; not deploying" >&2
  exit 1
fi

ssh "$PI" 'install -d -m 700 /srv/taco/satellite'
scp -q "$BIN" "$PI:/srv/taco/satellite/.$FILE.tmp"
ssh "$PI" "chmod 600 '/srv/taco/satellite/.$FILE.tmp' && mv '/srv/taco/satellite/.$FILE.tmp' '/srv/taco/satellite/$FILE'"
echo "Firmware $VERSION is on $PI as /srv/taco/satellite/$FILE."
echo "The '$NAME' satellite picks it up within 15 minutes (or press its RESET button)."
echo "Watch: ssh $PI journalctl --user-unit taco-voice -f | grep firmware"
