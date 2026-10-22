# Local weather stations

A weather station at the house gives Taco real readings instead of a forecast
model's estimate for the town. It's optional. When a station is set up:

- Every reading the station has (outdoor temperature, humidity, wind) replaces
  the forecast's value for the current conditions. Those values show a little
  brighter on the Home screen, with a small dot next to the town name.
- Indoor readings add an "Inside" line (temperature, humidity, CO₂), and Taco
  answers "what's the temperature inside?"
- Everything else (the condition, feels-like, highs and lows, the hourly and
  daily forecast) still comes from Open-Meteo.
- If the station can't be reached, or its newest reading is over 30 minutes
  old, Taco goes back to forecast values on its own, and switches back when the
  station returns.

Check on it with `curl -s localhost:8700/api/station` on the Pi.

## Netatmo

1. Sign in at [dev.netatmo.com](https://dev.netatmo.com), open **My apps**, and
   create an app (any name). Its page shows the **client ID** and **client secret**.
2. On the same page, use **Token generator** with the scope `read_station` and
   generate a token. Copy the **refresh token**.
3. Add this to `/srv/taco/config.toml` on the Pi (never to the repo):

   ```toml
   [weather_station]
   provider = "netatmo"

   [weather_station.netatmo]
   client_id = "..."
   client_secret = "..."
   refresh_token = "..."
   # device_id = "70:ee:50:..."   # only if the account has more than one station
   ```

4. Restart taco-core: `systemctl --user restart taco-core`.

Netatmo replaces the refresh token every time Taco uses it, so after the first
use, the one in `config.toml` is no longer valid. Taco keeps the live tokens in
`/srv/taco/data/oauth/netatmo.json`, which only the `taco` user can read. If
Taco ever loses access (for example, after a password change), generate a new
refresh token, put it in `config.toml`, and restart taco-core; a new token there
always replaces the stored one.

Netatmo's cloud updates about every 10 minutes, and Taco checks every 5
(`poll_s`).

## Adding another station

A station is a class with a `name` and an `async read(http)` method that
returns a `Reading` (`base.py`). Fill in whichever fields your station measures,
in metric units, and leave the rest as `None`. Raise `httpx.HTTPError` or
`ValueError` when it's unavailable; Taco logs it and uses the forecast.

1. Add `taco/core/stations/<yours>.py`. The constructor gets the
   `[weather_station.<yours>]` table from `config.toml` and a directory for
   private files.
2. Register it in `PROVIDERS` in `__init__.py`.
3. Set `provider = "<yours>"` under `[weather_station]`.

Stations on the LAN (Ecowitt, Davis WeatherLink Live) are plain HTTP calls. For a
cloud API that uses OAuth refresh tokens, use `taco.core.oauth.OAuthToken`, as
`netatmo.py` does: it refreshes before expiry, retries once on an expired
token, and keeps rotated tokens in a private file.
