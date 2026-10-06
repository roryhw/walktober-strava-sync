#!/usr/bin/env python3
"""Read-only exploration of the Walktober API. Never POSTs anything but sign-in."""
import http.cookiejar, json, os, sys, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
env = {}
for line in (HERE / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("'\"")

BASE = env["WALKTOBER_BASE"].rstrip("/") + "/api/v1"
OUT = HERE / "state" / "probe"
OUT.mkdir(parents=True, exist_ok=True)

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def csrf():
    for c in jar:
        if c.name in ("_Host-CSRF-TOKEN", "CSRF-TOKEN"):
            return urllib.parse.unquote(c.value)
    return None


def call(method, path, body=None):
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Origin": env["WALKTOBER_BASE"], "Referer": env["WALKTOBER_BASE"] + "/"}
    tok = csrf()
    if tok:
        headers["X-CSRF-Token"] = tok
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with opener.open(req, timeout=30) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        parsed = json.loads(raw or b"null")
    except ValueError:
        parsed = raw[:500].decode(errors="replace")
    return status, parsed


def save(name, obj):
    (OUT / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str))


if __name__ == "__main__":
    st, me = call("POST", "/auth/sign_in", {"email": env["WALKTOBER_EMAIL"],
                                            "password": env["WALKTOBER_PASSWORD"],
                                            "stay_logged_in": True})
    print("sign_in", st, "cookies:", sorted(c.name for c in jar))
    save("sign_in", me)
    if st >= 300:
        print(json.dumps(me)[:500]); sys.exit(1)
    paths = sys.argv[1:] or ["/promotions/current"]
    for p in paths:
        st, body = call("GET", p)
        name = p.strip("/").replace("/", "_").replace("?", "_")[:80]
        save(name, body)
        print(p, st, (json.dumps(body)[:300] if not isinstance(body, str) else body[:300]))
