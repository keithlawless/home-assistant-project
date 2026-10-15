# Notification webhook over a Cloudflare tunnel

Outside services (for example an Alexa routine when a Blink camera sees motion)
can put a notification card on Taco's screen. A Cloudflare tunnel carries the
request to the Pi without opening a port on the router.

## How it's protected
- The tunnel forwards only `/hook/notify`. The UI, `/api` and `/ws` return 404
  from the internet.
- Every request needs a sender's secret. A wrong or missing secret gets the same
  404 as a path that doesn't exist.
- An IP that sends 5 wrong secrets in 10 minutes is locked out for an hour.
- Each sender is limited to 20 notifications a minute, and at most 10 cards from
  outside senders stay on screen (older ones make room).
- Titles are cut to 80 characters and bodies to 300, and they show as plain text.

## 1. Add a sender on the Pi
Make a secret (hex, so it's safe in a URL):
```sh
openssl rand -hex 24
```
Add it to `/srv/taco/config.toml`. Use one entry per service, so you can revoke one
without touching the others:
```toml
[[webhooks.senders]]
name  = "alexa"
token = "<the secret>"
```
Then run `systemctl --user restart taco-core`. Test it locally:
```sh
curl -H "X-Taco-Token: <the secret>" -d '{"title":"Test","body":"Hello from curl"}' \
  localhost:8700/hook/notify
```

## 2. Install cloudflared (needs sudo)
```sh
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared
```

## 3. Create the tunnel (as the taco user)
```sh
cloudflared tunnel login                 # opens a URL; pick your domain
cloudflared tunnel create taco           # prints the tunnel ID, writes ~/.cloudflared/<ID>.json
cloudflared tunnel route dns taco taco-hook.example.com   # your chosen name
cp ~/taco/deploy/cloudflared/config.example.yml ~/.cloudflared/config.yml
nano ~/.cloudflared/config.yml           # fill in <TUNNEL-ID> (twice) and the hostname
cloudflared tunnel --config ~/.cloudflared/config.yml ingress validate
cloudflared tunnel --config ~/.cloudflared/config.yml ingress rule https://taco-hook.example.com/api/state
```
The last command should match the `http_status:404` rule, not port 8700.

Then start it. `deploy/install-user.sh` also enables it whenever `~/.cloudflared/config.yml` exists.
```sh
systemctl --user enable --now taco-tunnel
journalctl --user-unit taco-tunnel -f     # look for "Registered tunnel connection"
```

## 4. Check from outside (a phone on cellular)
- `https://taco-hook.example.com/hook/notify/<secret>?title=Hello` puts a card on screen.
- The same URL with a wrong secret returns 404.
- `https://taco-hook.example.com/` and `/api/state` return 404.

## Sending notifications
`POST /hook/notify` with the secret in an `X-Taco-Token` header and a JSON body.
Senders that can't set headers can use `GET /hook/notify/<secret>?title=…` with
the fields as query parameters instead.

| Field   | Meaning |
|---------|---------|
| `title` | Required, up to 80 characters. |
| `body`  | Optional, up to 300 characters. |
| `kind`  | `camera`, `warning` or `info` (the default). Sets the icon and color. |
| `key`   | Optional. Repeats with the same key within 2 minutes merge into one card ("×3"). |
| `wake`  | Default true: wake the screen, even from night mode. |
| `chime` | Default true: play a chime. Always silent in quiet hours (22:00–06:00, `[notifications] quiet_hours`). |

### Blink motion through Alexa
In the Alexa app, create a routine. Set **When this happens** to Smart Home → your Blink camera → motion. Alexa
routines have no built-in "open a URL" action, so the action has to go through a
bridge that can make a web request, such as a webhook-capable Alexa skill or IFTTT. Have it
request:
```
https://taco-hook.example.com/hook/notify/<secret>?kind=camera&title=Motion:%20front%20door&key=front%20door
```
Make one routine per camera, each with its own title and key.
