#!/usr/bin/env bash
# Install/refresh Taco as the taco user (no sudo). Run on the Pi from ~/taco.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

uv sync --python 3.12 -q

hermes/configure.sh

# Keep the HDMI audio output from suspending (see the conf file).
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
if ! cmp -s deploy/wireplumber/51-taco-no-suspend.conf ~/.config/wireplumber/wireplumber.conf.d/51-taco-no-suspend.conf; then
  cp deploy/wireplumber/51-taco-no-suspend.conf ~/.config/wireplumber/wireplumber.conf.d/
  systemctl --user restart wireplumber
fi

# The "Taco Music" sink that radio and Spotify play into (see the conf file).
mkdir -p ~/.config/pipewire/pipewire.conf.d
if ! cmp -s deploy/pipewire/60-taco-music.conf ~/.config/pipewire/pipewire.conf.d/60-taco-music.conf; then
  cp deploy/pipewire/60-taco-music.conf ~/.config/pipewire/pipewire.conf.d/
  systemctl --user restart pipewire wireplumber
fi

# Spotify Connect: unpack librespot from the raspotify .deb (no sudo needed).
if [[ ! -x ~/.local/bin/librespot ]]; then
  repo=https://dtcooper.github.io/raspotify
  deb=$(curl -s "$repo/dists/raspotify/main/binary-arm64/Packages" |
        awk '/^Package: raspotify$/{p=1} p&&/^Filename:/{print $2; exit}')
  tmp=$(mktemp -d)
  curl -sSL -o "$tmp/raspotify.deb" "$repo/$deb"
  dpkg-deb -x "$tmp/raspotify.deb" "$tmp/x"
  install -m 755 "$tmp/x/usr/bin/librespot" ~/.local/bin/librespot
  rm -rf "$tmp"
fi

mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now taco-core.service taco-voice.service taco-spotify.service
systemctl --user restart taco-core.service taco-voice.service taco-spotify.service hermes-gateway.service
# Notification webhook tunnel, once it's set up (deploy/cloudflared/README.md).
if [[ -f ~/.cloudflared/config.yml ]]; then
  systemctl --user enable --now taco-tunnel.service
  systemctl --user restart taco-tunnel.service
fi

# Kiosk: boot straight into the Taco UI.
mkdir -p ~/.config/labwc
if ! cmp -s deploy/labwc/autostart ~/.config/labwc/autostart; then
  [[ -f ~/.config/labwc/autostart && ! -f ~/.config/labwc/autostart.orig ]] && \
    cp ~/.config/labwc/autostart ~/.config/labwc/autostart.orig
  cp deploy/labwc/autostart ~/.config/labwc/autostart
  echo "Kiosk autostart installed; it takes effect at next login/reboot."
fi
echo "Taco services installed."
