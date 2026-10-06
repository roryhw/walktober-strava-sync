"""Minimal client for the HES Walktober participant API (<org>.walktober.com).

Reverse-engineered from the web app; see README.md for the contract. Standard library only.
"""
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env(path=HERE / ".env"):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


class WalktoberError(RuntimeError):
    pass


class Walktober:
    def __init__(self, base, email, password):
        self.site = base.rstrip("/")
        self.api = self.site + "/api/v1"
        self.email, self.password = email, password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.user = None

    def _csrf(self):
        for c in self.jar:
            if c.name in ("_Host-CSRF-TOKEN", "CSRF-TOKEN"):
                return urllib.parse.unquote(c.value)
        return None

    def _request(self, method, path, body=None, content_type="application/json"):
        headers = {"Accept": "application/json", "Origin": self.site, "Referer": self.site + "/"}
        tok = self._csrf()
        if tok:
            headers["X-CSRF-Token"] = tok
        data = None
        if body is not None:
            if content_type == "application/json":
                data = json.dumps(body).encode()
            else:
                data = body
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            parsed = json.loads(raw or b"null")
        except ValueError:
            parsed = raw[:300].decode(errors="replace")
        if status >= 300:
            msg = parsed.get("error") if isinstance(parsed, dict) else parsed
            raise WalktoberError(f"{method} {path} -> HTTP {status}: {msg}")
        return parsed.get("data", parsed) if isinstance(parsed, dict) else parsed

    def sign_in(self):
        self.user = self._request("POST", "/auth/sign_in", {
            "email": self.email, "password": self.password, "stay_logged_in": True})
        return self.user

    def promotion(self):
        return self._request("GET", "/promotions/current")

    def exercise_activities(self, promotion_id):
        return self._request("GET", f"/promotions/{promotion_id}/exercise_activities")

    def entries(self):
        """All of the user's day entries, keyed by recorded_on (YYYY-MM-DD)."""
        rows = self._request("GET", f"/users/{self.user['id']}/entries")
        return {r["recorded_on"][:10]: r for r in rows or []}

    def log_day(self, date, exercises, manual_steps=None):
        """Update-or-create one day. `exercises` REPLACES the day's exercise list.

        manual_steps=None leaves steps to the device sync (manually_recorded false).
        """
        payload = {
            "exercises": [{"id": int(e["id"]), "minutes": int(e["minutes"])} for e in exercises],
            "manually_recorded": manual_steps is not None,
            "manual_steps": int(manual_steps or 0),
        }
        return self._request("POST", "/logging/exercise", {"date": date, "payload": payload})

    def update_user(self, fields):
        """PUT /users/:id as multipart form `user[a][b]=v`, the way the web app sends it."""
        boundary = uuid.uuid4().hex
        parts = []

        def walk(prefix, val):
            if isinstance(val, dict):
                for k, v in val.items():
                    walk(f"{prefix}[{k}]", v)
            else:
                parts.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{prefix}"\r\n\r\n'
                    f"{'' if val is None else val}\r\n")
        for k, v in fields.items():
            walk(f"user[{k}]", v)
        body = ("".join(parts) + f"--{boundary}--\r\n").encode()
        return self._request("PUT", f"/users/{self.user['id']}", body,
                             content_type=f"multipart/form-data; boundary={boundary}")
