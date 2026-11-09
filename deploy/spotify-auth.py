#!/usr/bin/env python3
"""Get a Spotify refresh token for Taco's voice control (taco/core/spotify.py).

Run once, on a computer with a browser:

    python3 deploy/spotify-auth.py

It asks for the app's Client ID and Secret (from developer.spotify.com, where
the app's redirect URI must be exactly http://127.0.0.1:8888/callback), opens
the Spotify login, and prints the three lines to put under [spotify] in
/srv/taco/config.toml on the Pi. Nothing is saved here; keep the output out of
the repo.
"""

import base64
import getpass
import http.server
import json
import secrets
import urllib.parse
import urllib.request
import webbrowser

REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative user-library-read")


def main() -> None:
    client_id = input("Client ID: ").strip()
    client_secret = getpass.getpass("Client Secret (not shown): ").strip()
    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": SCOPES, "state": state, "show_dialog": "true"})

    got: dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if urllib.parse.urlsplit(self.path).path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            got.update(q)
            ok = q.get("state") == state and "code" in q
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Done: go back to the terminal." if ok else
                              f"Spotify said: {q.get('error', 'no code')}").encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 8888), Callback)
    print("\nOpening the Spotify login in your browser. If it doesn't open, visit:\n" + auth_url + "\n")
    webbrowser.open(auth_url)
    while "code" not in got and "error" not in got:
        server.handle_request()
    server.server_close()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"Authorization failed: {got.get('error', 'state mismatch')}")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({"grant_type": "authorization_code", "code": got["code"],
                                     "redirect_uri": REDIRECT}).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as r:
        token = json.load(r)

    print("Add these lines to /srv/taco/config.toml on the Pi, then restart taco-core:\n")
    print("[spotify]")
    print(f'client_id = "{client_id}"')
    print(f'client_secret = "{client_secret}"')
    print(f'refresh_token = "{token["refresh_token"]}"')


if __name__ == "__main__":
    main()
