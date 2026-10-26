"""`python -m saytheline login`: get a Jellyfin access token by Quick Connect and save it as
[jellyfin] api_key, for when copying an API key from the Dashboard to this machine is awkward
(Jellyfin on another box, a phone in hand). Prints only the six-digit code, never the token.

Approve the code in any signed-in Jellyfin app: profile > Quick Connect. The token is listed
under Dashboard > Devices as "Say the Line" and can be revoked there."""
import hashlib
import json
import os
import platform
import re
import time
import urllib.error
import urllib.request

from . import config

WAIT_S = 600


def _auth():
    host = platform.node() or "this machine"
    device_id = "saytheline-" + hashlib.sha1(host.encode()).hexdigest()[:12]
    return f'MediaBrowser Client="Say the Line", Device="{host}", DeviceId="{device_id}", Version="1.0"'


def _call(url, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"Authorization": _auth(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def save_key(path, url, token):
    """Put api_key (and url, if missing) under [jellyfin] in the TOML at `path`, leaving the rest
    of the file as it is. Creates the file when there is none."""
    text = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    line = f'api_key = "{token}"'
    if re.search(r'(?m)^\s*api_key\s*=.*$', text):
        text = re.sub(r'(?m)^\s*api_key\s*=.*$', lambda m: line, text, count=1)
    elif re.search(r'(?m)^\[jellyfin\]\s*$', text):
        text = re.sub(r'(?m)^\[jellyfin\]\s*$', lambda m: m.group(0) + "\n" + line, text, count=1)
    else:
        text = f'[jellyfin]\nurl = "{url}"\n{line}\n\n' + text
    if not re.search(r'(?m)^\s*url\s*=', text):
        text = re.sub(r'(?m)^\[jellyfin\]\s*$', lambda m: m.group(0) + f'\nurl = "{url}"', text, count=1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main(url=None):
    url = (url or os.environ.get("SAYTHELINE_JELLYFIN_URL") or "").rstrip("/")
    if not url:
        raise SystemExit('set [jellyfin] url in saytheline.toml first (or: login --url http://host:8096)')
    try:
        init = _call(url, "POST", "/QuickConnect/Initiate")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Quick Connect is not available ({e.code}): turn it on in Jellyfin's "
                         "Dashboard > General > Quick Connect, or paste an API key instead")
    except OSError as e:
        raise SystemExit(f"can't reach Jellyfin at {url}: {e}")
    print(f"Quick Connect code: {init['Code']}\n"
          "Approve it in a signed-in Jellyfin app: profile > Quick Connect. Waiting...", flush=True)
    end = time.time() + WAIT_S
    while not _call(url, "GET", f"/QuickConnect/Connect?secret={init['Secret']}").get("Authenticated"):
        if time.time() > end:
            raise SystemExit("no approval within 10 minutes: run login again for a new code")
        time.sleep(2)
    token = _call(url, "POST", "/Users/AuthenticateWithQuickConnect", {"Secret": init["Secret"]})["AccessToken"]
    path = config.loaded_from() or os.path.join(config.REPO, "saytheline.toml")
    save_key(path, url, token)
    print(f"Approved. Token saved as [jellyfin] api_key in {path} (revoke it under Dashboard > Devices).")
