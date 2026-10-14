#!/usr/bin/env bash
# Install the Taco mic satellite on a Pi Zero 2 W (or any Debian-ish box).
# Run from a copy of this directory: sudo ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

command -v arecord >/dev/null || apt-get install -y alsa-utils
install -m 755 taco_satellite.py /usr/local/bin/taco_satellite.py
install -m 644 taco-satellite.service /etc/systemd/system/taco-satellite.service

if [[ ! -f /etc/taco-satellite.conf ]]; then
  install -m 600 /dev/null /etc/taco-satellite.conf
  cat > /etc/taco-satellite.conf <<CONF
# Where Taco is, and the matching [[audio.satellites]] entry in /srv/taco/config.toml.
TACO_HOST=192.168.4.239
TACO_PORT=10700
SAT_NAME=living_room
SAT_TOKEN=$(head -c 18 /dev/urandom | base64 | tr '+/' '-_')
# ALSA capture device from `arecord -l`; blank uses the default.
SAT_DEVICE=
CONF
  echo "Wrote /etc/taco-satellite.conf. Copy its SAT_TOKEN into Taco's config.toml."
fi

systemctl daemon-reload
systemctl enable --now taco-satellite.service
systemctl restart taco-satellite.service
echo "taco-satellite running; follow it with: journalctl -fu taco-satellite"
