"""Screenshot the kiosk UI (optionally at a #view) through Chromium's
DevTools port on localhost. Dev tool; run on the Pi:

    ~/taco/.venv/bin/python ~/taco/deploy/ui-shot.py /tmp/shot.png [#devices]
"""

import base64
import json
import sys
import time

import httpx
from websockets.sync.client import connect

out = sys.argv[1]
view = sys.argv[2] if len(sys.argv) > 2 else ""

page = next(t for t in httpx.get("http://127.0.0.1:9222/json").json() if t["type"] == "page")
with connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
    def call(method, **params):
        msg_id = int(time.time() * 1000) % 1_000_000
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            reply = json.loads(ws.recv())
            if reply.get("id") == msg_id:
                return reply.get("result", {})

    if view is not None:
        call("Runtime.evaluate", expression=f"location.hash = {json.dumps(view)}; location.reload()")
        time.sleep(4)
    shot = call("Page.captureScreenshot", format="png")
    with open(out, "wb") as f:
        f.write(base64.b64decode(shot["data"]))
print(out)
