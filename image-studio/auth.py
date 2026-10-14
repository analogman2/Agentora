"""Accounts for the Image & Video Studio: users, password hashing (scrypt) and sign-in sessions.

users.json    {name: {"salt", "hash", "role": "admin"|"user", "created"}}
sessions.json {sha256(token): {"user", "created", "seen"}}  - only token hashes are stored on disk
"""
import hashlib, hmac, json, re, secrets, threading, time
from pathlib import Path

COOKIE = "gs_session"
SESSION_DAYS = 30
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")
MIN_PASSWORD = 8
LOCK_AFTER, LOCK_SECONDS = 5, 60

lock = threading.Lock()
users, sessions, fails = {}, {}, {}
USERS_FILE = SESSIONS_FILE = None


class AuthError(Exception):
    pass


def _hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32).hex()


def _token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.chmod(0o600)
    tmp.replace(path)


def load(folder):
    global USERS_FILE, SESSIONS_FILE
    USERS_FILE, SESSIONS_FILE = Path(folder) / "users.json", Path(folder) / "sessions.json"
    if USERS_FILE.exists():
        users.update(json.loads(USERS_FILE.read_text()))
    if SESSIONS_FILE.exists():
        now = time.time()
        sessions.update({k: s for k, s in json.loads(SESSIONS_FILE.read_text()).items()
                         if s["user"] in users and now - s["seen"] < SESSION_DAYS * 86400})


def needs_setup():
    return not users


def clean_name(name):
    name = str(name or "").strip().lower()
    if not NAME_RE.match(name):
        raise AuthError("Usernames are 2–32 characters: lowercase letters, digits, dot, dash or underscore")
    return name


def check_password(pw):
    if len(str(pw or "")) < MIN_PASSWORD:
        raise AuthError(f"Passwords need at least {MIN_PASSWORD} characters")
    return str(pw)


def create_user(name, password, role="user"):
    name, password = clean_name(name), check_password(password)
    if role not in ("admin", "user"):
        raise AuthError("Role must be admin or user")
    with lock:
        if name in users:
            raise AuthError("That username is taken")
        salt = secrets.token_hex(16)
        users[name] = {"salt": salt, "hash": _hash(password, salt), "role": role, "created": time.time()}
        _save(USERS_FILE, users)
    return name


def verify(name, password):
    """Check a sign-in. Repeated failures lock the username for a minute."""
    name = str(name or "").strip().lower()
    f = fails.get(name)
    if f and f[0] >= LOCK_AFTER and time.time() < f[1]:
        raise AuthError(f"Too many wrong passwords. Try again in {int(f[1] - time.time()) + 1} seconds.")
    u = users.get(name)
    ok = bool(u) and hmac.compare_digest(_hash(str(password or ""), u["salt"]), u["hash"])
    if not ok:
        count = (f[0] if f and time.time() < f[1] + LOCK_SECONDS else 0) + 1
        fails[name] = [count, time.time() + LOCK_SECONDS]
        raise AuthError("Wrong username or password")
    fails.pop(name, None)
    return name


def new_session(name, ip=None, device=None):
    token = secrets.token_urlsafe(32)
    with lock:
        sessions[_token_key(token)] = {"user": name, "created": time.time(), "seen": time.time(), "ip": ip, "device": device,
                                       "first_ip": ip}
        _save(SESSIONS_FILE, sessions)
    return token


def user_for(token):
    """The signed-in user for a session cookie, or None. Sessions expire after SESSION_DAYS without use."""
    if not token:
        return None
    s = sessions.get(_token_key(token))
    if not s or s["user"] not in users or time.time() - s["seen"] > SESSION_DAYS * 86400:
        return None
    last_seen[s["user"]] = time.time()                 # exact, in memory (Manage users shows it)
    if time.time() - s["seen"] > 3600:                 # refresh "last seen" at most hourly (keeps disk writes rare)
        with lock:
            s["seen"] = time.time()
            _save(SESSIONS_FILE, sessions)
    return {"name": s["user"], "role": users[s["user"]]["role"]}


def end_session(token):
    with lock:
        if sessions.pop(_token_key(token or ""), None):
            _save(SESSIONS_FILE, sessions)


def end_user_sessions(name, keep_token=None):
    keep = _token_key(keep_token) if keep_token else None
    with lock:
        for k in [k for k, s in sessions.items() if s["user"] == name and k != keep]:
            del sessions[k]
        _save(SESSIONS_FILE, sessions)


def set_password(name, password):
    password = check_password(password)
    with lock:
        salt = secrets.token_hex(16)
        users[name].update(salt=salt, hash=_hash(password, salt))
        _save(USERS_FILE, users)


def _admins():
    return [n for n, u in users.items() if u["role"] == "admin"]


def set_role(name, role):
    if role not in ("admin", "user"):
        raise AuthError("Role must be admin or user")
    with lock:
        if role == "user" and _admins() == [name]:
            raise AuthError("There must be at least one admin")
        users[name]["role"] = role
        _save(USERS_FILE, users)


def delete_user(name):
    with lock:
        if _admins() == [name]:
            raise AuthError("You can't delete the only admin")
        users.pop(name)
        _save(USERS_FILE, users)
    end_user_sessions(name)


last_seen = {}                                         # name -> time of their latest request (this run)


def note_client(token, ip, device):
    """Keep each sign-in's latest IP address and device (saved with the next hourly "seen" write, or now if it changed)."""
    s = sessions.get(_token_key(token)) if token else None
    if not s or not ip:
        return
    if s.get("ip") != ip or s.get("device") != device:
        with lock:
            s.update(ip=ip, device=device)
            s.setdefault("first_ip", ip)
            _save(SESSIONS_FILE, sessions)


def listing():
    """Accounts with when each was last seen: the latest of their sessions' saved "seen" (hourly) and this run's exact
    time, and how many devices are signed in."""
    out = []
    for n, u in sorted(users.items()):
        mine = [s for s in sessions.values() if s["user"] == n]
        seen = max([s["seen"] for s in mine] + [last_seen.get(n, 0)] + [0]) or None
        out.append({"name": n, "role": u["role"], "created": u["created"], "last_seen": seen, "sessions": len(mine),
                    "sign_ins": sorted(({"since": s["created"], "seen": max(s["seen"], 0), "ip": s.get("ip"), "device": s.get("device"),
                                         "first_ip": s.get("first_ip")} for s in mine), key=lambda x: -x["seen"])})
    return out


def user_for_name(name):
    return {"name": name, "role": users[name]["role"]}
