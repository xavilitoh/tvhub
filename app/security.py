"""Accounts, sessions and request protection for TVHub.

Design (OWASP ASVS / NIST SP 800-63B oriented):
  * Passwords: Argon2id (argon2-cffi defaults = RFC 9106 low-memory profile), per-hash random salt, transparent re-hash
    when the parameters are raised. Minimum 12 characters, up to 128, no composition rules, common-password and
    username checks (NIST 800-63B 5.1.1.2). Never logged, never returned.
  * Sessions: 256-bit random tokens; only their SHA-256 is stored (server side), so a leaked sessions file is useless.
    Cookie is HttpOnly + SameSite=Strict (+ Secure and the __Host- prefix over HTTPS). Idle and absolute timeouts,
    a fresh token on every login / password change, all sessions of a user die when the password changes.
  * CSRF: SameSite=Strict, a per-session token in the X-CSRF-Token header for every state-changing request and an
    Origin check (also on WebSocket handshakes, which prevents cross-site WebSocket hijacking).
  * Brute force: failures are counted per (IP, user), per IP and per account with exponential lock-outs; unknown users
    cost the same time as wrong passwords and get the same message (no user enumeration).
  * First run: nobody can use TVHub until the first administrator exists, and creating it needs a one-time setup code
    that is only printed in the container log (so a stranger who finds the port first cannot take over).
  * Everything security relevant is written to an append-only audit log (never with secrets).
  * Files are written atomically with mode 0600.

Recovery for a lost administrator password (needs shell access to the container):
    docker compose exec tvhub python app/security.py reset-password <username>
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

DATA = os.environ.get("DATA_DIR", "/data")
TRUST_PROXY = os.environ.get("TVHUB_TRUST_PROXY", "") == "1"

IDLE = int(os.environ.get("TVHUB_SESSION_IDLE", "7200"))               # 2 h without activity
ABSOLUTE = int(os.environ.get("TVHUB_SESSION_MAX", "43200"))           # 12 h in any case
IDLE_REMEMBER = 7 * 86400                                              # "keep me signed in": 7 d idle ...
ABSOLUTE_REMEMBER = 30 * 86400                                         # ... 30 d absolute
MAX_SESSIONS_PER_USER = 10

MIN_PW, MAX_PW = 12, 128
USER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
ROLES = ("admin", "user")

# A few of the most common long passwords / words; also compared after stripping digits and symbols.
COMMON = {
    "password1234", "password12345", "passwordpassword", "123456789012", "1234567890123", "qwertyuiop12", "qwertyuiopas",
    "qwertyuiop123", "iloveyou1234", "administrator", "administrator1", "letmein123456", "welcome12345", "welcome123456",
    "changeme1234", "changemenow", "adminadmin12", "abc123abc123", "1q2w3e4r5t6y", "1qaz2wsx3edc", "qazwsxedcrfv",
    "contrasena123", "contrasena1234", "contrasenasegura", "tvhubtvhub12", "monkey123456", "dragon123456", "football1234",
    "baseball1234", "superman1234", "trustno1trustno1", "000000000000", "111111111111", "aaaaaaaaaaaa",
}
COMMON_WORDS = {
    "password", "contrasena", "qwertyuiop", "qwerty", "administrator", "admin", "letmein", "welcome", "iloveyou", "tvhub",
    "television", "changeme", "football", "baseball", "monkey", "dragon", "superman", "bienvenido", "123456",
}

_LOCK_WINDOW = 15 * 60
_hasher = PasswordHasher()                                             # argon2id
_DUMMY_HASH = _hasher.hash("tvhub-dummy-password-for-timing")
_hash_slots = asyncio.Semaphore(2)                                     # bounds memory used by concurrent hashes


def now():
    return time.time()


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _write_json(path, obj):
    """Atomic write, owner-only permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------- password policy

def norm_username(u):
    return unicodedata.normalize("NFKC", str(u or "")).strip().lower()


def check_username(u):
    if not USER_RE.match(u):
        return "El usuario debe tener de 3 a 32 caracteres: minúsculas, números, punto, guion o guion bajo"
    return None


def check_password(pw, username=""):
    """Returns a (Spanish) message when the password is not acceptable, else None."""
    if not isinstance(pw, str):
        return "Contraseña no válida"
    if len(pw) < MIN_PW:
        return f"La contraseña debe tener al menos {MIN_PW} caracteres"
    if len(pw) > MAX_PW:
        return f"La contraseña es demasiado larga (máximo {MAX_PW})"
    low = unicodedata.normalize("NFKC", pw).lower()
    letters = re.sub(r"[^a-zñ]", "", low)
    if low in COMMON or letters in COMMON_WORDS or low.strip("0123456789") in COMMON_WORDS:
        return "Esa contraseña es demasiado común; elige otra"
    if username and len(username) >= 3 and username in low:
        return "La contraseña no puede contener el nombre de usuario"
    if len(set(low)) < 5 or low in "01234567890123456789" or low in "abcdefghijklmnopqrstuvwxyz":
        return "Usa una contraseña menos repetitiva o predecible"
    return None


async def hash_password(pw):
    async with _hash_slots:
        return await asyncio.get_running_loop().run_in_executor(None, _hasher.hash, pw)


async def _verify(h, pw):
    def run():
        try:
            return _hasher.verify(h, pw)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    async with _hash_slots:
        return await asyncio.get_running_loop().run_in_executor(None, run)


# --------------------------------------------------------------------------- brute-force limiter

class Limiter:
    """Sliding-window failure counter with exponential lock-outs."""

    LIMITS = {"pair": 5, "ip": 20, "user": 30}

    def __init__(self):
        self.fails = {}                                  # key -> [timestamps]
        self.locked = {}                                 # key -> (until, strikes)

    def _keys(self, ip, user):
        return [("pair", f"{ip}|{user}"), ("ip", ip), ("user", user)]

    def wait(self, ip, user):
        t = now()
        worst = 0
        for kind, k in self._keys(ip, user):
            until = self.locked.get((kind, k), (0, 0))[0]
            worst = max(worst, until - t)
        return int(worst) + 1 if worst > 0 else 0

    def fail(self, ip, user):
        t = now()
        for kind, k in self._keys(ip, user):
            key = (kind, k)
            lst = [x for x in self.fails.get(key, []) if t - x < _LOCK_WINDOW] + [t]
            self.fails[key] = lst
            if len(lst) >= self.LIMITS[kind]:
                strikes = self.locked.get(key, (0, 0))[1] + 1
                self.locked[key] = (t + min(_LOCK_WINDOW * 2 ** (strikes - 1), 86400), strikes)
                self.fails[key] = []
        if len(self.fails) > 5000:                      # cheap garbage collection under attack
            self.fails = {k: v for k, v in self.fails.items() if v and t - v[-1] < _LOCK_WINDOW}
            self.locked = {k: v for k, v in self.locked.items() if v[0] > t or t - v[0] < 86400}

    def success(self, ip, user):
        self.fails.pop(("pair", f"{ip}|{user}"), None)
        self.fails.pop(("user", user), None)
        self.locked.pop(("pair", f"{ip}|{user}"), None)


# --------------------------------------------------------------------------- the store

class Security:
    def __init__(self, data_dir=DATA):
        self.dir = Path(data_dir)
        self.users_path = self.dir / "users.json"
        self.sessions_path = self.dir / "sessions.json"
        self.audit_path = self.dir / "audit.log"
        self.users = {}                                  # id -> record
        self.tokens = {}                                 # id -> record (API tokens, hashed)
        self.sessions = {}                               # sha256(token) -> record
        self.limiter = Limiter()
        self.setup_lock = asyncio.Lock()
        self._mtime = 0
        self._dirty = False
        self.setup_code = ""
        self.load()
        if not self.users:
            self.setup_code = os.environ.get("TVHUB_SETUP_TOKEN", "").strip() or self._new_code()

    # ---- persistence
    def load(self):
        try:
            self._mtime = self.users_path.stat().st_mtime
        except OSError:
            self._mtime = 0
        d = _read_json(self.users_path, {})
        self.users = {u["id"]: u for u in d.get("users", [])}
        self.tokens = {t["id"]: t for t in d.get("tokens", [])}
        now_ = now()
        self.sessions = {h: s for h, s in _read_json(self.sessions_path, {}).items() if self._alive(s, now_)}

    def _maybe_reload(self):
        try:
            m = self.users_path.stat().st_mtime
        except OSError:
            m = 0
        if m != self._mtime:                             # edited by the CLI (password reset)
            self.load()

    def save_users(self):
        _write_json(self.users_path, {"users": list(self.users.values()), "tokens": list(self.tokens.values())})
        try:
            self._mtime = self.users_path.stat().st_mtime
        except OSError:
            pass

    def save_sessions(self):
        _write_json(self.sessions_path, self.sessions)
        self._dirty = False

    async def flusher(self):
        """Persist last-activity times and drop expired sessions once a minute."""
        while True:
            await asyncio.sleep(60)
            t = now()
            self.sessions = {h: s for h, s in self.sessions.items() if self._alive(s, t)}
            if self._dirty:
                self.save_sessions()

    @staticmethod
    def _new_code():
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no look-alike characters
        c = "".join(secrets.choice(alphabet) for _ in range(12))
        return f"{c[:4]}-{c[4:8]}-{c[8:]}"

    # ---- audit
    def audit(self, event, request=None, user="", **kw):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "user": user}
        if request is not None:
            rec["ip"] = self.client_ip(request)
        rec.update({k: v for k, v in kw.items() if v not in (None, "")})
        line = json.dumps(rec, ensure_ascii=False)
        print("AUDIT", line, file=sys.stderr, flush=True)
        try:
            if self.audit_path.exists() and self.audit_path.stat().st_size > 5_000_000:
                os.replace(self.audit_path, self.audit_path.with_suffix(".log.1"))
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ---- request helpers
    def client_ip(self, request):
        if TRUST_PROXY:
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[-1].strip()[:64]       # the hop our own proxy appended
        return (request.remote or "?")[:64]

    def is_secure(self, request):
        if request.secure:
            return True
        return TRUST_PROXY and request.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def cookie_name(self, request):
        return "__Host-tvhub" if self.is_secure(request) else "tvhub_session"

    def origin_ok(self, request):
        """Same-origin check for browsers (they always send Origin on cross-site and WebSocket requests)."""
        origin = request.headers.get("Origin")
        if origin is None:
            return True                                       # non-browser client (API token, curl)
        try:
            host = urlsplit(origin).netloc
        except ValueError:
            return False
        return bool(host) and hmac.compare_digest(host.lower(), request.host.lower())

    # ---- users
    def has_users(self):
        self._maybe_reload()
        return bool(self.users)

    def find(self, username):
        self._maybe_reload()
        for u in self.users.values():
            if u["username"] == username:
                return u
        return None

    def public(self, u):
        return {"id": u["id"], "username": u["username"], "role": u["role"], "created": u.get("created"),
                "disabled": bool(u.get("disabled"))}

    def admins(self):
        return [u for u in self.users.values() if u["role"] == "admin" and not u.get("disabled")]

    async def verify_login(self, username, pw):
        """Constant-ish time: an unknown user is hashed against a dummy so timing does not reveal it."""
        u = self.find(username)
        if u is None or u.get("disabled"):
            await _verify(_DUMMY_HASH, pw)
            return None
        if not await _verify(u["pw"], pw):
            return None
        if _hasher.check_needs_rehash(u["pw"]):
            u["pw"] = await hash_password(pw)
            self.save_users()
        return u

    async def add_user(self, username, pw, role):
        u = {"id": secrets.token_hex(8), "username": username, "role": role, "pw": await hash_password(pw),
             "created": int(now()), "pw_changed": 0}
        self.users[u["id"]] = u
        self.save_users()
        return u

    async def set_password(self, u, pw):
        u["pw"] = await hash_password(pw)
        u["pw_changed"] = now()                         # every session created before this instant is void
        self.save_users()

    def delete_user(self, uid):
        self.users.pop(uid, None)
        self.revoke_user(uid)
        self.save_users()

    # ---- sessions
    def _alive(self, s, t):
        u = self.users.get(s.get("uid"))
        if not u or u.get("disabled"):
            return False
        if s["created"] < u.get("pw_changed", 0):
            return False
        idle, absolute = (IDLE_REMEMBER, ABSOLUTE_REMEMBER) if s.get("remember") else (IDLE, ABSOLUTE)
        return t - s["last"] < idle and t - s["created"] < absolute

    def create_session(self, u, request, remember=False):
        mine = sorted((h for h, s in self.sessions.items() if s["uid"] == u["id"]), key=lambda h: self.sessions[h]["last"])
        for h in mine[: max(0, len(mine) - MAX_SESSIONS_PER_USER + 1)]:
            self.sessions.pop(h, None)
        token = secrets.token_urlsafe(32)
        t = now()
        self.sessions[_sha(token)] = {"uid": u["id"], "created": t, "last": t, "remember": bool(remember),
                                      "csrf": secrets.token_urlsafe(24), "ip": self.client_ip(request),
                                      "ua": request.headers.get("User-Agent", "")[:120]}
        self.save_sessions()
        return token, self.sessions[_sha(token)]

    def session_for(self, token):
        if not token:
            return None, None
        h = _sha(token)
        s = self.sessions.get(h)
        if s is None:
            return None, None
        t = now()
        if not self._alive(s, t):
            self.sessions.pop(h, None)
            self._dirty = True
            return None, None
        if t - s["last"] > 30:
            s["last"] = t
            self._dirty = True
        return h, s

    def revoke(self, h):
        if self.sessions.pop(h, None) is not None:
            self.save_sessions()

    def revoke_user(self, uid, keep=None):
        for h in [h for h, s in self.sessions.items() if s["uid"] == uid and h != keep]:
            self.sessions.pop(h, None)
        self.save_sessions()

    # ---- API tokens (for integrations such as Home Assistant)
    def create_api_token(self, name):
        raw = "tvh_" + secrets.token_urlsafe(32)
        rec = {"id": secrets.token_hex(6), "name": name[:40], "hash": _sha(raw), "created": int(now()), "last_used": 0}
        self.tokens[rec["id"]] = rec
        self.save_users()
        return raw, rec

    def api_token(self, raw):
        h = _sha(raw)
        for t in self.tokens.values():
            if hmac.compare_digest(t["hash"], h):
                if now() - t.get("last_used", 0) > 300:
                    t["last_used"] = int(now())
                    self._dirty = True
                return t
        return None

    def delete_api_token(self, tid):
        if self.tokens.pop(tid, None) is not None:
            self.save_users()
            return True
        return False


# --------------------------------------------------------------------------- headers

def security_headers(secure, ws_host="", nonce=""):
    h = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    }
    if secure:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if nonce:
        h["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
            f"connect-src 'self' ws://{ws_host} wss://{ws_host}; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'")
    return h


# --------------------------------------------------------------------------- CLI

def _cli(argv):
    if len(argv) < 2 or argv[0] != "reset-password":
        print("usage: python app/security.py reset-password <username>")
        return 2
    sec = Security()
    u = sec.find(norm_username(argv[1]))
    if not u:
        print("No such user.")
        return 1
    new = secrets.token_urlsafe(18)
    u["pw"] = _hasher.hash(new)
    u["pw_changed"] = now()
    u.pop("disabled", None)
    sec.save_users()
    sec.audit("password_reset_cli", user=u["username"])
    print(f"New password for {u['username']} (shown once, change it after signing in):\n\n  {new}\n")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
