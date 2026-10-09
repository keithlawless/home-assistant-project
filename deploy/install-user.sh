#!/usr/bin/env bash
# Install/refresh Taco as the taco user (no sudo). Run on the Pi from ~/taco.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

uv sync --python 3.12 -q

# Taco persona for Hermes (back up the original once).
[[ -f ~/.hermes/SOUL.md.orig ]] || cp ~/.hermes/SOUL.md ~/.hermes/SOUL.md.orig
cp hermes/SOUL.md ~/.hermes/SOUL.md

# Keep the HDMI audio output from suspending (see the conf file).
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
if ! cmp -s deploy/wireplumber/51-taco-no-suspend.conf ~/.config/wireplumber/wireplumber.conf.d/51-taco-no-suspend.conf; then
  cp deploy/wireplumber/51-taco-no-suspend.conf ~/.config/wireplumber/wireplumber.conf.d/
  systemctl --user restart wireplumber
fi

mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now taco-core.service taco-voice.service
systemctl --user restart taco-core.service taco-voice.service hermes-gateway.service

# Kiosk: boot straight into the Taco UI.
mkdir -p ~/.config/labwc
if ! cmp -s deploy/labwc/autostart ~/.config/labwc/autostart; then
  [[ -f ~/.config/labwc/autostart && ! -f ~/.config/labwc/autostart.orig ]] && \
    cp ~/.config/labwc/autostart ~/.config/labwc/autostart.orig
  cp deploy/labwc/autostart ~/.config/labwc/autostart
  echo "Kiosk autostart installed; it takes effect at next login/reboot."
fi
echo "Taco services installed."
