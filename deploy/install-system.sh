#!/usr/bin/env bash
# One-time system provisioning for Taco. Run ON THE PI as root:
#   sudo bash deploy/install-system.sh
# Everything else (uv, Hermes, Taco services) installs as the taco user without sudo.
set -euo pipefail

TACO_USER="${TACO_USER:-taco}"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update
apt-get install -y \
  docker.io docker-compose \
  portaudio19-dev libportaudio2 libasound2-dev \
  build-essential python3-dev \
  mpv sqlite3 jq curl rsync \
  bluez wlr-randr

echo "==> Adding $TACO_USER to docker, audio, video, input and bluetooth groups"
usermod -aG docker,audio,video,input,bluetooth "$TACO_USER"

echo "==> Creating /srv/taco runtime directories on the SSD"
install -d -o "$TACO_USER" -g "$TACO_USER" /srv/taco
for d in data models voices logs photos matter; do
  install -d -o "$TACO_USER" -g "$TACO_USER" "/srv/taco/$d"
done

echo "==> Enabling Docker and user-service lingering"
systemctl enable --now docker
# Lets taco's systemd --user services (core, voice, hermes) start at boot without a login.
loginctl enable-linger "$TACO_USER"

echo
echo "Done. Reboot (sudo reboot) so the new group memberships take effect."
