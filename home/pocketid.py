"""Pocket ID sign-in (OpenID Connect: authorization code with PKCE) for an app that keeps its own accounts.

Pocket ID (pve1 LXC 130) is the household's passkey sign-in. An app gets a "Sign in with Pocket ID" button next to its
password form: start() sends the browser to Pocket ID, finish() takes the code it comes back with and returns who
signed in. The app then signs in its own account of the same name (Pocket ID's username); nobody new is created, and
passwords keep working. Client ID and secret: ~/.config/pocketid/<app>.json (made by
~/claude/ServerMaintenance/pocketid-clients-setup.sh); without that file the button stays hidden.

The same file is copied into Pantry, Designer Studio and the price tracker front: keep the copies in step.
Standard library only; calls block, so async apps run them in a thread.
"""
import base64, hashlib, json, os, secrets, time, urllib.parse, urllib.request
from pathlib import Path

ISSUER = os.environ.get("POCKETID_ISSUER", "https://linusdesktop.tail8e139e.ts.net:8021")
STATE_COOKIE = "pocketid_state"      # ties the callback to the browser that started the sign-in
PENDING_SECONDS = 600


class PocketIDError(Exception):
    pass


class PocketID:
    def __init__(self, app, config_dir=None):
        self.file = Path(config_dir or os.environ.get("POCKETID_DIR", Path.home() / ".config/pocketid")) / f"{app}.json"
        self.pending = {}            # state -> (expires, code verifier, redirect uri, where to go after)
        self._meta = None

    @property
    def enabled(self):
        return self.file.is_file()

    def _client(self):
        try:
            c = json.loads(self.file.read_text())
            return c["client_id"], c["client_secret"]
        except (OSError, ValueError, KeyError) as e:
            raise PocketIDError("Pocket ID sign-in isn't set up for this app") from e

    def _get(self, url, headers=None, data=None):
        req = urllib.request.Request(url, data=data, headers={"Accept": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise PocketIDError(f"Pocket ID said {e.code}: {e.read()[:200].decode(errors='replace')}") from e
        except (OSError, ValueError) as e:
            raise PocketIDError(f"Can't reach Pocket ID ({e})") from e

    def meta(self):
        if not self._meta:
            self._meta = self._get(ISSUER + "/.well-known/openid-configuration")
        return self._meta

    def start(self, redirect_uri, next_url="/"):
        """Returns (URL to send the browser to, state to put in the STATE_COOKIE cookie)."""
        client_id, _ = self._client()
        now = time.time()
        self.pending = {s: p for s, p in self.pending.items() if p[0] > now}
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        self.pending[state] = (now + PENDING_SECONDS, verifier, redirect_uri, next_url)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": "openid profile email",
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        return self.meta()["authorization_endpoint"] + "?" + urllib.parse.urlencode(q), state

    def finish(self, query, cookie_state):
        """query: the callback's query parameters (dict). Returns (claims, where to go after). Claims include
        preferred_username, email and sub."""
        if query.get("error"):
            raise PocketIDError(f"Pocket ID sign-in stopped: {query.get('error_description') or query['error']}")
        state = query.get("state") or ""
        p = self.pending.pop(state, None)
        if not p or not cookie_state or not secrets.compare_digest(state, cookie_state) or p[0] < time.time():
            raise PocketIDError("That sign-in link has expired or came from another browser: try again")
        _, verifier, redirect_uri, next_url = p
        client_id, client_secret = self._client()
        basic = base64.b64encode(f"{urllib.parse.quote(client_id)}:{urllib.parse.quote(client_secret)}".encode()).decode()
        tok = self._get(self.meta()["token_endpoint"], {"Authorization": "Basic " + basic,
                        "Content-Type": "application/x-www-form-urlencoded"},
                        urllib.parse.urlencode({"grant_type": "authorization_code", "code": query.get("code", ""),
                                                "redirect_uri": redirect_uri, "code_verifier": verifier}).encode())
        claims = self._get(self.meta()["userinfo_endpoint"], {"Authorization": "Bearer " + tok["access_token"]})
        return claims, next_url


def account_for(claims, names):
    """The app's own account for this Pocket ID user (same username, any case), or None."""
    want = str(claims.get("preferred_username") or "").lower()
    return next((n for n in names if want and n.lower() == want), None)


def safe_next(url, default="/"):
    """Only same-site paths, so the sign-in can't be used to bounce someone to another website."""
    return url if isinstance(url, str) and url.startswith("/") and not url.startswith("//") else default
