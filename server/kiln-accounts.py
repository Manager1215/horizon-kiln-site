#!/usr/bin/env python3
"""Accounts and reports from players for horizon-kiln.cc: one small service behind nginx, and its commands.

    kiln-accounts.py serve                        the service (127.0.0.1:8082 unless KILN_LISTEN says otherwise)
    kiln-accounts.py check                        what is set up: mail, sign-in services, the catalogue's builds
    kiln-accounts.py accounts [TEXT]              accounts, newest first, or those whose name contains TEXT
    kiln-accounts.py reports [--mod ID] [--name NAME]
    kiln-accounts.py delete-report N [N...]
    kiln-accounts.py ban NAME | unban NAME        a banned account cannot sign in, and its reports are hidden
    kiln-accounts.py role NAME team|none          the team's reports carry a mark; the team moderates on /account/admin
    kiln-accounts.py rename NAME NEW
    kiln-accounts.py delete-account NAME          the account and all its reports, at once

Kept per account: the public name, the date it was made, and either an e-mail address with a scrypt hash of the
password, or the id the account has at GitHub, Google, Discord or Modrinth (no address and no name from there), or
both. Sessions are kept as hashes of the cookie's value. To cap how often one address may try, a hash of the
address with a salt that changes every day is kept for two days; the address itself is never written. Nothing here
writes a password, a token or a key to a log.

Everything lives in one SQLite file, $KILN_DATA/kiln.db. Settings and keys come from the environment and from the
file KILN_ENV_FILE names, /etc/kiln-accounts/env on the server (see SETUP.md next to this file).
"""
import base64
import contextlib
import datetime
import hashlib
import hmac
import html
import json
import os
import queue
import re
import secrets
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def load_env_file(path):
    """Reads KEY=value lines (# starts a comment) from the file KILN_ENV_FILE names, for the service and for the
    commands alike. Values stay out of the process's arguments, so `ps` never shows them."""
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as error:
        print("settings: cannot read %s (%s)" % (path, type(error).__name__), file=sys.stderr)
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_env_file(os.environ.get("KILN_ENV_FILE", ""))
ENV = os.environ.get
DATA = ENV("KILN_DATA", "/var/lib/kiln-accounts")
SITE_ROOT = ENV("KILN_SITE_ROOT", "/var/www/kiln")
ORIGIN = ENV("KILN_ORIGIN", "https://horizon-kiln.cc").rstrip("/")
LISTEN = ENV("KILN_LISTEN", "127.0.0.1:8082")
MAIL_MODE = ENV("KILN_MAIL", "smtp")  # smtp, or "log" for a local test: letters go to $KILN_DATA/outbox.txt
SMTP_HOST = ENV("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(ENV("SMTP_PORT", "465"))
SMTP_USER = ENV("SMTP_USER", "")
SMTP_PASSWORD = ENV("SMTP_PASSWORD", "")
MAIL_FROM = ENV("MAIL_FROM", "") or SMTP_USER
CONTACT = "horizonkiln@gmail.com"

COOKIE = "__Host-kiln"
DAY = 86400
SESSION_TTL = 30 * DAY        # from the last visit
PENDING_TTL = 15 * 60         # a sign-in with another service, or a password reset, in progress
WELCOME_TTL = 30 * 60         # choosing a name after the first sign-in with another service
CONFIRM_TTL = 2 * DAY
RESET_TTL = 3600
TEXT_MAX = 400
VERSION_MAX = 64
LATEST = 30                   # reports in a mod's JSON
PAGE_REPORTS = 100            # reports on a mod's report page

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,23}$")
EMAIL = re.compile(r"^[^@\s<>\"',;()\[\]\\]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
NEXT = re.compile(r"^/(?![/\\])[A-Za-z0-9._~/%#?=&-]*$")
RESERVED = {"admin", "administrator", "anonymous", "account", "accounts", "help", "kiln", "kiln-team", "kiln_team",
            "kilnteam", "mod", "mods", "moderator", "null", "official", "owner", "root", "signin", "signup",
            "staff", "support", "system", "team", "horizon"}
RESULTS = {"works": "Works", "partly": "Partly", "broken": "Doesn't work"}
MARKS = {"works": "mark-ok", "partly": "mark-maybe", "broken": "mark-bad"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")

# (how many, in how many seconds)
LIMITS = {
    "signup": (5, DAY),               # accounts made from one address
    "signin-fail-addr": (30, 3600),   # wrong passwords from one address
    "signin-fail-mail": (10, 900),    # wrong passwords for one e-mail address
    "reset-addr": (5, 3600),          # password resets asked from one address
    "reset-mail": (3, 3600),          # ... and for one e-mail address
    "letters": (3, 3600),             # confirmation letters for one account
    "auth": (20, 3600),               # sign-ins with another service started from one address
    "report": (40, DAY),              # reports written or changed by one account
    "mail": (400, DAY),               # letters a day in all, under Gmail's own cap
}

PROVIDERS = {
    "github": {
        "label": "GitHub",
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "user": "https://api.github.com/user",
        "scope": "",
        "pkce": True,
    },
    "google": {
        "label": "Google",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "openid",
        "pkce": True,
    },
    "discord": {
        "label": "Discord",
        "authorize": "https://discord.com/oauth2/authorize",
        "token": "https://discord.com/api/oauth2/token",
        "user": "https://discord.com/api/users/@me",
        "scope": "identify",
        "pkce": False,
    },
    "modrinth": {
        "label": "Modrinth",
        "authorize": "https://modrinth.com/auth/authorize",
        "token": "https://api.modrinth.com/_internal/oauth/token",
        "user": "https://api.modrinth.com/v2/user",
        "scope": "USER_READ",
        "pkce": False,
        "secret_in_header": True,
    },
}

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created INTEGER NOT NULL,
    email TEXT UNIQUE COLLATE NOCASE,
    password TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT '',
    banned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS identities (
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    account INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created INTEGER NOT NULL,
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS identities_account ON identities(account);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    account INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created INTEGER NOT NULL,
    expires INTEGER NOT NULL,
    pending TEXT
);
CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account);
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    account INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    expires INTEGER NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    n INTEGER PRIMARY KEY,
    mod TEXT NOT NULL,
    account INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    build TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    UNIQUE (account, mod, build)
);
CREATE INDEX IF NOT EXISTS reports_mod ON reports(mod, updated);
CREATE TABLE IF NOT EXISTS hits (kind TEXT NOT NULL, key TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS hits_kind ON hits(kind, key, at);
CREATE TABLE IF NOT EXISTS log (at INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL);
"""

KEY = b""  # the service's own key for form tokens, made on the first start and kept in the database


# Small helpers

def now():
    return int(time.time())


def log(message):
    print(message, file=sys.stderr, flush=True)


def esc(text):
    return html.escape(str(text if text is not None else ""), quote=True)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def day_of(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).date()


def long_date(ts):
    d = day_of(ts)
    return "%d %s %d" % (d.day, MONTHS[d.month - 1], d.year)


def iso_date(ts):
    return day_of(ts).isoformat()


def clean_text(text, limit):
    text = "".join(ch for ch in str(text or "")
                   if ch == "\n" or (ord(ch) >= 32 and ord(ch) != 127 and not 0x202a <= ord(ch) <= 0x202e
                                     and not 0x2066 <= ord(ch) <= 0x2069))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit].strip()


def clean_line(text, limit):
    return re.sub(r"\s+", " ", clean_text(text, limit * 4)).strip()[:limit]


def norm_email(text):
    text = str(text or "").strip()
    return text.lower() if len(text) <= 254 and EMAIL.match(text) else ""


def safe_next(value, default="/account/"):
    value = str(value or "")
    if value and len(value) <= 300 and NEXT.match(value) and not value.startswith("/account/signout"):
        return value
    return default


def quote(value):
    return urllib.parse.quote(value, safe="/")


# Passwords: scrypt from the standard library, at most two at a time so the memory stays bounded

SCRYPT = (2 ** 15, 8, 1)  # 32 MiB and about a tenth of a second per hash
_scrypt_slots = threading.BoundedSemaphore(2)
_dummy_hash = None


def _scrypt(password, salt, n, r, p):
    with _scrypt_slots:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def hash_password(password):
    n, r, p = SCRYPT
    salt = secrets.token_bytes(16)
    return "scrypt$%d$%d$%d$%s$%s" % (n, r, p, b64e(salt), b64e(_scrypt(password, salt, n, r, p)))


def check_password(password, stored):
    try:
        kind, n, r, p, salt, digest = str(stored or "").split("$")
        if kind != "scrypt":
            return False
        return hmac.compare_digest(_scrypt(password, b64d(salt), int(n), int(r), int(p)), b64d(digest))
    except (ValueError, TypeError):
        return False


def waste_time(password):
    """An unknown address takes as long to refuse as a wrong password."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(12))
    check_password(password, _dummy_hash)


def password_problem(password, name="", email=""):
    if len(password) < 10:
        return "The password needs at least 10 characters."
    if len(password) > 200:
        return "The password can have at most 200 characters."
    low = password.lower()
    if (name and low == name.lower()) or (email and low == email.lower()):
        return "The password cannot be the name or the address."
    return ""


def name_problem(c, name, except_id=0):
    if not NAME.match(name):
        return "A name has 3 to 24 characters: letters, digits, - and _, starting with a letter or a digit."
    if name.lower() in RESERVED:
        return "That name is kept for the site. Choose another."
    row = c.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row and row["id"] != except_id:
        return "That name is taken. Choose another."
    return ""


# The database

def connect():
    c = sqlite3.connect(os.path.join(DATA, "kiln.db"), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@contextlib.contextmanager
def database():
    c = connect()
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def meta(c, key):
    row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def set_meta(c, key, value):
    c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def init_db():
    global KEY
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    c = connect()
    try:
        c.executescript(SCHEMA)
        if not meta(c, "key"):
            set_meta(c, "key", secrets.token_hex(32))
        KEY = bytes.fromhex(meta(c, "key"))
        c.commit()
    finally:
        c.close()


def day_salt(c):
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    day, _, salt = meta(c, "salt").partition(" ")
    if day != today or not salt:
        salt = secrets.token_hex(16)
        set_meta(c, "salt", today + " " + salt)
    return salt


def keyed(value):
    """A stable key for limits on an e-mail address, without keeping the address."""
    return hmac.new(KEY, value.encode("utf-8"), "sha256").hexdigest()[:24]


def recent(c, kind, key):
    _limit, window = LIMITS[kind]
    return c.execute("SELECT COUNT(*) FROM hits WHERE kind = ? AND key = ? AND at > ?",
                     (kind, key, now() - window)).fetchone()[0]


def allowed(c, kind, key):
    return recent(c, kind, key) < LIMITS[kind][0]


def record(c, kind, key):
    c.execute("INSERT INTO hits (kind, key, at) VALUES (?, ?, ?)", (kind, key, now()))


def take(c, kind, key):
    if not allowed(c, kind, key):
        return False
    record(c, kind, key)
    return True


def form_token(purpose):
    ts = str(now())
    return ts + "." + hmac.new(KEY, (purpose + "|" + ts).encode(), "sha256").hexdigest()[:32]


def form_token_age(token, purpose):
    """Seconds since the form was drawn, or None when the token is not ours or older than three hours."""
    ts, _, mac = str(token or "").partition(".")
    if not ts.isdigit():
        return None
    good = hmac.new(KEY, (purpose + "|" + ts).encode(), "sha256").hexdigest()[:32]
    if not hmac.compare_digest(mac, good):
        return None
    age = now() - int(ts)
    return age if 0 <= age <= 3 * 3600 else None


def new_session(c, account, ttl, pending=None):
    raw = secrets.token_urlsafe(32)
    t = now()
    c.execute("INSERT INTO sessions (token, account, csrf, created, expires, pending) VALUES (?, ?, ?, ?, ?, ?)",
              (sha(raw), account, secrets.token_urlsafe(24), t, t + ttl, json.dumps(pending) if pending else None))
    return raw


def new_token(c, kind, account, ttl, data=None):
    raw = secrets.token_urlsafe(32)
    c.execute("DELETE FROM tokens WHERE kind = ? AND account = ?", (kind, account))
    c.execute("INSERT INTO tokens (token, kind, account, expires, data) VALUES (?, ?, ?, ?, ?)",
              (sha(raw), kind, account, now() + ttl, json.dumps(data) if data else None))
    return raw


def find_token(c, raw, kinds):
    if not raw or len(raw) > 100:
        return None
    row = c.execute("SELECT * FROM tokens WHERE token = ? AND expires > ?", (sha(raw), now())).fetchone()
    return row if row and row["kind"] in kinds else None


def cleanup(c):
    t = now()
    c.execute("DELETE FROM sessions WHERE expires <= ?", (t,))
    c.execute("DELETE FROM tokens WHERE expires <= ?", (t,))
    c.execute("DELETE FROM accounts WHERE active = 0 AND created <= ?", (t - CONFIRM_TTL,))
    c.execute("DELETE FROM hits WHERE at <= ?", (t - 2 * DAY,))
    c.execute("DELETE FROM log WHERE at <= ?", (t - 365 * DAY,))


def add_log(c, actor, action, target):
    c.execute("INSERT INTO log (at, actor, action, target) VALUES (?, ?, ?, ?)", (now(), actor, action, target))


def methods_of(c, account):
    return [r["provider"] for r in c.execute("SELECT provider FROM identities WHERE account = ? ORDER BY provider", (account,))]


# The catalogue: which builds a report can name, and each mod's name and version, from the site's own files

_site_cache = {}
_site_lock = threading.Lock()


def site_json(rel, extract):
    path = os.path.join(SITE_ROOT, rel)
    try:
        st = os.stat(path)
    except OSError:
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    with _site_lock:
        hit = _site_cache.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            value = extract(json.load(f))
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    with _site_lock:
        if len(_site_cache) > 5000:
            _site_cache.clear()
        _site_cache[path] = (stamp, value)
    return value


def builds():
    data = site_json("mods.json", lambda d: {"kiln": str(d.get("kiln") or ""),
                                             "dev": str((d.get("development") or {}).get("build") or "")})
    out = []
    if data and data["kiln"]:
        out.append({"value": "release", "build": data["kiln"], "label": build_label("release", data["kiln"])})
    if data and data["dev"]:
        out.append({"value": "dev", "build": data["dev"], "label": build_label("dev", data["dev"])})
    return out


def build_label(channel, build):
    return "Kiln %s (release)" % build if channel == "release" else "Kiln " + build


def mod_info(mod_id):
    if not ID.match(mod_id or ""):
        return None
    return site_json("mods/%s.json" % mod_id, lambda d: {"name": str(d.get("name") or mod_id),
                                                         "version": str(d.get("version") or "")})


def mod_name(mod_id):
    info = mod_info(mod_id)
    return info["name"] if info else mod_id


# Reports

def report_rows(c, mod_id, limit):
    return c.execute("""SELECT r.*, a.name, a.role FROM reports r JOIN accounts a ON a.id = r.account
                        WHERE r.mod = ? AND a.banned = 0 ORDER BY r.updated DESC, r.n DESC LIMIT ?""",
                     (mod_id, limit)).fetchall()


def counts_for(c, mod_id):
    counts = {"works": 0, "partly": 0, "broken": 0}
    for row in c.execute("""SELECT r.result, COUNT(*) AS k FROM reports r JOIN accounts a ON a.id = r.account
                            WHERE r.mod = ? AND a.banned = 0 GROUP BY r.result""", (mod_id,)):
        if row["result"] in counts:
            counts[row["result"]] = row["k"]
    return counts


def report_json(r):
    return {"n": r["n"], "result": r["result"], "resultLabel": RESULTS.get(r["result"], r["result"]),
            "text": r["text"], "channel": r["channel"], "build": r["build"],
            "buildLabel": build_label(r["channel"], r["build"]), "version": r["version"],
            "date": iso_date(r["updated"]), "created": iso_date(r["created"]),
            "name": r["name"], "team": r["role"] == "team"}


def mine_json(c, account, mod_id):
    rows = c.execute("SELECT * FROM reports WHERE account = ? AND mod = ? ORDER BY updated DESC", (account, mod_id))
    return [{"n": r["n"], "result": r["result"], "text": r["text"], "channel": r["channel"], "build": r["build"],
             "buildLabel": build_label(r["channel"], r["build"]), "version": r["version"],
             "date": iso_date(r["updated"])} for r in rows]


def summary_for(c, mod_id):
    counts = counts_for(c, mod_id)
    info = mod_info(mod_id) or {}
    return {"id": mod_id, "works": counts["works"], "partly": counts["partly"], "broken": counts["broken"],
            "total": sum(counts.values()), "version": info.get("version", ""), "builds": builds(),
            "latest": [report_json(r) for r in report_rows(c, mod_id, LATEST)]}


def counts_line(counts):
    total = counts["works"] + counts["partly"] + counts["broken"]
    if not total:
        return "No reports from players yet."
    parts = []
    if counts["works"]:
        parts.append("%d %s" % (counts["works"], "says it works" if counts["works"] == 1 else "say it works"))
    if counts["partly"]:
        parts.append("%d partly" % counts["partly"])
    if counts["broken"]:
        parts.append("%d %s" % (counts["broken"], "says it doesn't" if counts["broken"] == 1 else "say it doesn't"))
    return ("One report: " if total == 1 else "%d reports: " % total) + ", ".join(parts) + "."


# Mail: letters wait in a queue and go out through SMTP after the request's data is saved

_outbox = queue.Queue()


def mail_ready():
    return MAIL_MODE == "log" or bool(SMTP_USER and SMTP_PASSWORD and MAIL_FROM)


def mail_worker():
    while True:
        to, subject, body = _outbox.get()
        msg = EmailMessage()
        msg["From"] = formataddr(("Kiln", MAIL_FROM or CONTACT))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(usegmt=True)
        msg["Message-ID"] = make_msgid(domain="horizon-kiln.cc")
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(body)
        if MAIL_MODE == "log":
            with open(os.path.join(DATA, "outbox.txt"), "a", encoding="utf-8") as f:
                f.write(msg.as_string() + "\n")
            continue
        for attempt in (1, 2, 3):
            try:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as smtp:
                    smtp.login(SMTP_USER, SMTP_PASSWORD)
                    smtp.send_message(msg)
                break
            except (OSError, smtplib.SMTPException) as error:
                log("mail: sending failed (%s), attempt %d of 3" % (type(error).__name__, attempt))
                time.sleep(15 * attempt)


SIGNATURE = "\n\nKiln\n%s\n" % ORIGIN

LETTER_CONFIRM = """Hello %(name)s,

This address was used to create the account "%(name)s" on horizon-kiln.cc, Kiln's website. To confirm the address and sign in, open this link within 48 hours:

%(link)s

If you did not create it, ignore this letter: the account is removed in 48 hours unless the link is opened."""

LETTER_EXISTS = """Hello,

Someone tried to create an account on horizon-kiln.cc with this address, but it already belongs to the account "%(name)s".

If it was you, sign in: %(origin)s/account/signin
Forgot the password? Choose a new one: %(origin)s/account/reset

If it was not you, ignore this letter: nothing has changed."""

LETTER_RESET = """Hello %(name)s,

Someone, probably you, asked to choose a new password for the account "%(name)s" on horizon-kiln.cc. Open this link within an hour to do it:

%(link)s

If you did not ask, ignore this letter: the password stays as it is."""

LETTER_EMAIL = """Hello %(name)s,

You asked to sign in to the account "%(name)s" on horizon-kiln.cc with this address. Open this link within 48 hours to confirm it:

%(link)s

If it was not you, ignore this letter: nothing changes."""


# Sign-in with other services (OAuth 2.0 and OpenID Connect): only the id the account has there is kept

def provider_keys(p):
    return ENV(p.upper() + "_CLIENT_ID", ""), ENV(p.upper() + "_CLIENT_SECRET", "")


def providers_on():
    return [p for p in PROVIDERS if all(provider_keys(p))]


def redirect_uri(p):
    return "%s/account/auth/%s/callback" % (ORIGIN, p)


def http_json(url, form=None, headers=None):
    head = {"User-Agent": "horizon-kiln.cc accounts", "Accept": "application/json"}
    head.update(headers or {})
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    if data is not None:
        head["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=head)
    with urllib.request.urlopen(request, timeout=15, context=ssl.create_default_context()) as response:
        return json.loads(response.read(1_000_000).decode("utf-8"))


def jwt_claims(token):
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    return json.loads(b64d(parts[1]))


def oauth_identity(p, code, verifier):
    """Trades the code for the account's id at the service (and a name to suggest, which is not kept)."""
    conf = PROVIDERS[p]
    client_id, secret = provider_keys(p)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(p), "client_id": client_id}
    headers = {}
    if conf.get("secret_in_header"):
        headers["Authorization"] = secret
    else:
        form["client_secret"] = secret
    if conf["pkce"]:
        form["code_verifier"] = verifier
    token = http_json(conf["token"], form, headers)
    if p == "google":
        # The ID token comes straight from Google's token endpoint over TLS, so its signature need not be checked
        # (OpenID Connect Core 1.0, 3.1.3.7); its audience, issuer and expiry still are.
        claims = jwt_claims(token.get("id_token"))
        if (claims.get("aud") != client_id or claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com")
                or int(claims.get("exp") or 0) < now() or not claims.get("sub")):
            raise ValueError("the ID token does not fit")
        return str(claims["sub"]), ""
    access = token.get("access_token")
    if not access:
        raise ValueError("no access token")
    headers = {"Authorization": access if p == "modrinth" else "Bearer " + access}
    if p == "github":
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    user = http_json(conf["user"], None, headers)
    subject = str(user.get("id") or "")
    if not subject:
        raise ValueError("no id")
    return subject, str(user.get("login") or user.get("username") or "")


# HTML: the same header, footer and stylesheet as the static pages

NAV = (("/index.html#how", "How it works"), ("/index.html#browser", "Mod browser"), ("/index.html#news", "What's new"),
       ("/index.html#roadmap", "Roadmap"), ("/index.html#faq", "FAQ"), ("/index.html#contact", "Contact"),
       ("/mods.html", "Mods"), ("https://github.com/Manager1215/horizon-kiln", "GitHub"))

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<meta name="robots" content="noindex">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f3eee6">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#15110d">
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/site.webmanifest">
<link rel="preload" href="/archivo.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/archivo-expanded.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/style.css">
<script src="/site.js" defer></script>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>

<header class="site-header">
  <div class="doc header-row">
    <a class="brand" href="/index.html" aria-label="Kiln, home page">Kiln</a>
    <nav class="site-nav" aria-label="Sections">
%(nav)s
    </nav>
    %(account)s
  </div>
  <div class="heat-line" aria-hidden="true"></div>
</header>

<main id="main" class="doc">
%(main)s
</main>

<footer class="site-footer">
  <div class="doc footer-grid">
    <div>
      <p class="footer-brand">Kiln</p>
      <p>Java mods in Horizon.</p>
      <p>License: <a href="https://github.com/Manager1215/horizon-kiln/blob/main/LICENSE">GPL-3.0</a> (0.1.0-alpha was MIT).</p>
      <p>The GPL keeps Kiln open, not people away: to use its work in your project, or just to talk, write to <a href="mailto:horizonkiln@gmail.com">horizonkiln@gmail.com</a> or Discord <strong>manager1215</strong>.</p>
    </div>
    <nav aria-label="Project links">
      <ul>
        <li><a href="/index.html">Home</a></li>
        <li><a href="/mods.html">Supported mods</a></li>
        <li><a href="https://github.com/Manager1215/horizon-kiln">Source on GitHub</a></li>
      </ul>
    </nav>
    <nav aria-label="Documents">
      <ul>
        <li><a href="https://github.com/Manager1215/horizon-kiln/releases">Releases</a></li>
        <li><a href="https://github.com/Manager1215/horizon-kiln/blob/main/INSTALL.md">Install guide (INSTALL.md)</a></li>
        <li><a href="https://github.com/horizon-rust/horizon">Horizon</a></li>
      </ul>
    </nav>
  </div>
  <div class="doc">
    <p class="disclaimer">Not affiliated with Horizon, Mojang or Microsoft. Minecraft is a trademark of Mojang/Microsoft. The timer is not an official speedrun.com timer.</p>
    <p class="disclaimer">The one cookie here keeps you signed in, and is set only when you sign in. <a href="/privacy.html">Your data</a>.</p>
  </div>
</footer>
</body>
</html>
"""

NOTES = {
    "confirmed": "Your address is confirmed, and you are signed in.",
    "email-added": "The address is confirmed: you can now sign in with it and its password.",
    "email-sent": "We sent a letter to that address. Open the link in it within 48 hours; until then the address does not sign you in.",
    "password": "The password is changed. Every other device is signed out.",
    "reset": "The new password is set, and you are signed in.",
    "linked": "The service is linked: you can sign in with it.",
    "unlinked": "The service is unlinked.",
    "welcome": "Your account is ready.",
    "signedout": "You are signed out.",
    "saved": "Your report is saved.",
    "deleted": "The report is deleted.",
    "failed": "Signing in with that service did not work. Try again, or sign in another way.",
    "taken": "That account at the service is already linked to another Kiln account.",
    "done": "Done.",
}


def notice(h):
    key = h.query.get("done", "")
    return '<p class="notice" role="status">%s</p>\n' % esc(NOTES[key]) if key in NOTES else ""


def error_block(message):
    return '<p class="form-error" role="alert">%s</p>\n' % esc(message) if message else ""


def sheet(title, note, body):
    return """  <div class="sheet account-sheet">
    <div class="sheet-head">
      <h1 class="sheet-title">%s</h1>
%s    </div>
    <div class="sheet-body">
%s    </div>
  </div>""" % (esc(title), note, body)


def hidden(name, value):
    return '<input type="hidden" name="%s" value="%s">' % (esc(name), esc(value))


def report_item(r, with_mod=False, actions=""):
    head = '<span class="mark %s">%s</span> <span class="report-name">%s</span>' % (
        MARKS.get(r["result"], "mark-unknown"), esc(RESULTS.get(r["result"], r["result"])), esc(r["name"]))
    if r["role"] == "team":
        head += ' <span class="mark mark-team">Kiln team</span>'
    if with_mod:
        head = '<a href="/mods/%s.html">%s</a>: %s' % (esc(r["mod"]), esc(mod_name(r["mod"])), head)
    meta = [build_label(r["channel"], r["build"])]
    if r["version"]:
        meta.append("mod " + r["version"])
    meta.append(long_date(r["updated"]))
    out = '<li class="report">\n<p class="report-head">%s</p>\n' % head
    if r["text"]:
        out += '<p class="report-text">%s</p>\n' % esc(r["text"])
    out += '<p class="report-meta">%s</p>\n' % esc(" · ".join(meta))
    return out + actions + "</li>\n"


def providers_block(next_url, intro="or"):
    on = providers_on()
    if not on:
        return ""
    items = "".join('<li><a class="btn-quiet" href="/account/auth/%s?next=%s">Continue with %s</a></li>\n'
                    % (p, esc(quote(next_url)), esc(PROVIDERS[p]["label"])) for p in on)
    return ('<p class="or-rule"><span>%s</span></p>\n<ul class="provider-list">\n%s</ul>\n'
            '<p class="form-hint">From another service we keep only the id your account has there: no address, no name. '
            '<a href="/privacy.html">Your data</a>.</p>\n' % (esc(intro), items))


# The request handler

class Handler(BaseHTTPRequestHandler):
    server_version = "kiln-accounts"
    sys_version = ""

    def log_message(self, format, *args):  # nginx keeps the access log, without query strings for /account/
        pass

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        """The GET answer without its body; links that act once (letters, sign-in services) only act on GET."""
        if urllib.parse.urlsplit(self.path).path.startswith(("/account/confirm", "/account/auth/", "/account/reset")):
            self.out_headers = []
            self.resp = (405, None, b"", [("Allow", "GET")])
            return self.flush()
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    # plumbing

    def dispatch(self, method):
        self.resp = None
        self.out_headers = []
        self.letters = []
        self.session = None
        self.account = None
        self.form = {}
        self.json_body = None
        url = urllib.parse.urlsplit(self.path)
        self.url_path = url.path
        try:
            self.query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        except ValueError:
            self.query = {}
        try:
            if method == "POST" and not self.read_body():
                return self.flush()
            with database() as c:
                self.c = c
                self.load_session()
                if method == "POST" and not self.same_origin():
                    self.fail(403, "This form was sent from another site, so it was not accepted.")
                else:
                    self.route(method)
            for letter in self.letters:
                _outbox.put(letter)
        except Exception:
            log("error: %s %s\n%s" % (method, self.url_path, traceback.format_exc(limit=8)))
            self.out_headers = []
            self.resp = (500, "text/plain; charset=utf-8", b"Something went wrong on the server. Try again later.\n", [])
        self.flush()

    def route(self, method):
        if self.url_path == "/account":
            return self.redirect("/account/", 301)
        for m, pattern, handler in ROUTES:
            if m == method:
                found = pattern.fullmatch(self.url_path)
                if found:
                    return handler(self, *found.groups())
        allowed_methods = {m for m, pattern, _ in ROUTES if pattern.fullmatch(self.url_path)}
        if allowed_methods:
            self.resp = (405, "text/plain; charset=utf-8", b"Method not allowed.\n", [("Allow", ", ".join(sorted(allowed_methods)))])
            return
        self.not_found()

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 16384:
            self.resp = (413, "text/plain; charset=utf-8", b"Too large.\n", [])
            return False
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            if ctype == "application/json":
                data = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                self.json_body = data
                self.form = {k: str(v) for k, v in data.items() if isinstance(v, (str, int)) and not isinstance(v, bool)}
            else:
                self.form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
        except (ValueError, UnicodeDecodeError):
            self.resp = (400, "text/plain; charset=utf-8", b"Bad request.\n", [])
            return False
        return True

    def same_origin(self):
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin == ORIGIN
        referer = self.headers.get("Referer")
        if referer:
            return referer == ORIGIN or referer.startswith(ORIGIN + "/")
        return True  # neither header: the form tokens still protect every form

    def address(self):
        return (self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0])

    def who(self):
        return hashlib.sha256((day_salt(self.c) + "|" + self.address()).encode()).hexdigest()[:24]

    def cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def set_cookie(self, raw, max_age):
        self.out_headers.append(("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Lax" % (COOKIE, raw, max_age)))

    def clear_cookie(self):
        self.out_headers.append(("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax" % COOKIE))

    def load_session(self):
        raw = self.cookie()
        if not raw:
            return
        row = self.c.execute("SELECT * FROM sessions WHERE token = ? AND expires > ?", (sha(raw[:100]), now())).fetchone()
        if not row:
            self.clear_cookie()
            return
        if row["account"]:
            account = self.c.execute("SELECT * FROM accounts WHERE id = ?", (row["account"],)).fetchone()
            if not account or account["banned"] or not account["active"]:
                self.c.execute("DELETE FROM sessions WHERE token = ?", (row["token"],))
                self.clear_cookie()
                return
            self.account = account
            if row["expires"] - now() < SESSION_TTL - DAY:  # keep it 30 days from the last visit
                self.c.execute("UPDATE sessions SET expires = ? WHERE token = ?", (now() + SESSION_TTL, row["token"]))
                self.set_cookie(raw, SESSION_TTL)
        self.session = row

    def pending(self):
        if not self.session or not self.session["pending"]:
            return {}
        try:
            return json.loads(self.session["pending"])
        except ValueError:
            return {}

    def set_pending(self, pending, ttl=None):
        """Keeps a sign-in or a password reset in progress in the session. A reader who is not signed in gets a
        short session for it (ttl seconds): the same cookie, which signing in replaces."""
        value = json.dumps(pending) if pending else None
        if not self.session:
            raw = new_session(self.c, None, ttl or PENDING_TTL, pending)
            self.set_cookie(raw, ttl or PENDING_TTL)
            self.session = self.c.execute("SELECT * FROM sessions WHERE token = ?", (sha(raw),)).fetchone()
            return
        self.c.execute("UPDATE sessions SET pending = ? WHERE token = ?", (value, self.session["token"]))
        if ttl and not self.session["account"]:
            self.c.execute("UPDATE sessions SET expires = ? WHERE token = ?", (now() + ttl, self.session["token"]))
            self.set_cookie(self.cookie()[:100], ttl)
        self.session = self.c.execute("SELECT * FROM sessions WHERE token = ?", (self.session["token"],)).fetchone()

    def sign_in(self, account_id):
        if self.session:
            self.c.execute("DELETE FROM sessions WHERE token = ?", (self.session["token"],))
        raw = new_session(self.c, account_id, SESSION_TTL)
        self.set_cookie(raw, SESSION_TTL)
        self.session = self.c.execute("SELECT * FROM sessions WHERE token = ?", (sha(raw),)).fetchone()
        self.account = self.c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    def csrf(self):
        return self.session["csrf"] if self.session else ""

    def csrf_ok(self):
        sent = self.form.get("csrf") or self.headers.get("X-CSRF-Token") or ""
        return bool(self.session) and hmac.compare_digest(sent.encode(), self.session["csrf"].encode())

    def wants_json(self):
        return self.json_body is not None or "application/json" in (self.headers.get("Accept") or "")

    def mail(self, to, subject, body):
        if not mail_ready() or not take(self.c, "mail", "all"):
            return False
        self.letters.append((to, subject, body + SIGNATURE))
        return True

    # responses

    def page(self, title, main, code=200, error=False):
        if self.account:
            current = ' aria-current="page"' if self.url_path == "/account/" else ""
            account = '<a class="account-link" href="/account/"%s>%s</a>' % (current, esc(self.account["name"]))
        else:
            current = ' aria-current="page"' if self.url_path == "/account/signin" else ""
            account = '<a class="account-link" href="/account/signin"%s>Sign in</a>' % current
        nav = "\n".join('      <a href="%s">%s</a>' % (esc(href), esc(text)) for href, text in NAV)
        body = PAGE % {"title": esc(("Error: " if error else "") + title + " — Kiln"), "nav": nav, "account": account, "main": main}
        self.resp = (code, "text/html; charset=utf-8", body.encode("utf-8"), [])

    def send_json(self, code, data, cache="no-store"):
        self.resp = (code, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8"),
                     [("Cache-Control", cache)])

    def redirect(self, location, code=303):
        self.resp = (code, None, b"", [("Location", location)])

    def not_found(self):
        if self.url_path.endswith(".json"):
            return self.send_json(404, {"error": "no such page"})
        self.page("Not found", sheet("Not found", "", "<p>There is no such page. <a href=\"/index.html\">Kiln's home page</a>.</p>\n"), 404)

    def fail(self, code, message):
        if self.wants_json():
            return self.send_json(code, {"error": message})
        body = error_block(message) + '<p><a href="/account/">Your account</a> · <a href="/index.html">Kiln\'s home page</a></p>\n'
        self.page("Not accepted", sheet("Not accepted", "", body), code, error=True)

    def flush(self):
        code, ctype, body, headers = self.resp or (500, "text/plain; charset=utf-8", b"No response.\n", [])
        headers = list(headers) + self.out_headers
        names = {k.lower() for k, _ in headers}
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "cache-control" not in names:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        if ctype and ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                             "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def need_account(self, back):
        if self.account:
            return True
        self.redirect("/account/signin?next=" + quote(back))
        return False


# Pages: signing in, signing up, letters

def signin_page(h, error="", email="", code=200):
    nxt = safe_next(h.form.get("next") or h.query.get("next"))
    body = notice(h) + error_block(error)
    body += """<form class="form" method="post" action="/account/signin">
%s%s
<p class="form-field"><label for="f-email">E-mail address</label><input id="f-email" name="email" type="email" autocomplete="username" required maxlength="254" value="%s"></p>
<p class="form-field"><label for="f-password">Password</label><input id="f-password" name="password" type="password" autocomplete="current-password" required maxlength="200"></p>
<p class="form-actions"><button class="btn-fire" type="submit">Sign in</button> <a href="/account/reset">Forgot the password?</a></p>
</form>
""" % (hidden("ftoken", form_token("signin")), hidden("next", nxt), esc(email))
    body += providers_block(nxt)
    note = '      <p class="sheet-note">An account lets you write reports on how mods run in Horizon. No account yet? <a href="/account/signup%s">Create one</a>.</p>\n' % (
        "?next=" + esc(quote(nxt)) if nxt != "/account/" else "")
    h.page("Sign in", sheet("Sign in", note, body), code, error=bool(error))


def signin_get(h):
    if h.account:
        return h.redirect(safe_next(h.query.get("next")))
    signin_page(h)


def signin_post(h):
    f = h.form
    email = norm_email(f.get("email"))
    password = f.get("password", "")[:200]
    if form_token_age(f.get("ftoken"), "signin") is None:
        return signin_page(h, "The form was open too long. Sign in again.", email, 400)
    addr = h.who()
    mail_key = keyed("mail|" + (email or "-"))
    if not allowed(h.c, "signin-fail-addr", addr) or not allowed(h.c, "signin-fail-mail", mail_key):
        return signin_page(h, "Too many wrong passwords. Wait a quarter of an hour, or choose a new password.", email, 429)
    account = h.c.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone() if email else None
    if account and account["password"]:
        good = check_password(password, account["password"])
    else:
        waste_time(password)
        good = False
    if not good:
        record(h.c, "signin-fail-addr", addr)
        record(h.c, "signin-fail-mail", mail_key)
        return signin_page(h, "The address and the password do not match.", email, 400)
    if account["banned"]:
        return signin_page(h, "This account is banned. If you think that is a mistake, write to %s." % CONTACT, email, 403)
    if not account["active"]:
        sent = False
        if take(h.c, "letters", str(account["id"])):
            raw = new_token(h.c, "confirm", account["id"], CONFIRM_TTL)
            sent = h.mail(account["email"], "Confirm your Kiln account",
                          LETTER_CONFIRM % {"name": account["name"], "link": "%s/account/confirm?token=%s" % (ORIGIN, raw)})
        message = ("The address is not confirmed yet. We sent the letter again: open the link in it."
                   if sent else "The address is not confirmed yet. Open the link in the letter we sent; a new one can be sent in an hour.")
        return signin_page(h, message, email, 403)
    h.sign_in(account["id"])
    h.redirect(safe_next(f.get("next")))


def signup_page(h, error="", values=None, code=200):
    values = values or {}
    nxt = safe_next(h.form.get("next") or h.query.get("next"))
    body = error_block(error)
    if mail_ready():
        body += """<form class="form" method="post" action="/account/signup">
%s%s
<p class="form-field"><label for="f-name">Name</label><input id="f-name" name="name" required minlength="3" maxlength="24" pattern="[A-Za-z0-9][A-Za-z0-9_\\-]{2,23}" autocomplete="nickname" spellcheck="false" aria-describedby="f-name-hint" value="%s"><span class="form-hint" id="f-name-hint">Shown with your reports. 3 to 24 letters, digits, - or _.</span></p>
<p class="form-field"><label for="f-email">E-mail address</label><input id="f-email" name="email" type="email" required maxlength="254" autocomplete="email" aria-describedby="f-email-hint" value="%s"><span class="form-hint" id="f-email-hint">Never shown. For signing in and for a new password.</span></p>
<p class="form-field"><label for="f-password">Password</label><input id="f-password" name="password" type="password" required minlength="10" maxlength="200" autocomplete="new-password" aria-describedby="f-password-hint"><span class="form-hint" id="f-password-hint">At least 10 characters.</span></p>
<p class="form-field hp"><label for="f-website">Leave this field empty</label><input id="f-website" name="website" tabindex="-1" autocomplete="off"></p>
<p class="form-actions"><button class="btn-fire" type="submit">Create the account</button></p>
</form>
""" % (hidden("ftoken", form_token("signup")), hidden("next", nxt), esc(values.get("name", "")), esc(values.get("email", "")))
        body += providers_block(nxt)
    elif providers_on():
        body += '<p>Sign-up with an e-mail address is not open at the moment. You can make an account with another service:</p>\n'
        body += providers_block(nxt, "Sign up with")
    else:
        body += '<p>Making an account is not open at the moment. Come back soon.</p>\n'
    note = ('      <p class="sheet-note">Your name shows next to your reports. <a href="/privacy.html">What we keep</a>, '
            'and how to delete it. Have an account? <a href="/account/signin">Sign in</a>.</p>\n')
    h.page("Create an account", sheet("Create an account", note, body), code, error=bool(error))


def signup_get(h):
    if h.account:
        return h.redirect("/account/")
    signup_page(h)


def check_mail_page(h, email):
    body = ("<p>We sent a letter to <strong>%s</strong>. Open the link in it within 48 hours to confirm the address; "
            "the link also signs you in.</p>\n<p>No letter after a few minutes? Look in the spam folder. Signing up again "
            "with the same address sends a new letter.</p>\n" % esc(email))
    h.page("Check your mail", sheet("Check your mail", "", body))


def signup_post(h):
    f = h.form
    name = clean_line(f.get("name"), 40)
    email = norm_email(f.get("email"))
    password = f.get("password", "")
    values = {"name": name, "email": str(f.get("email", ""))[:254]}
    if f.get("website"):  # the hidden field: only a robot fills it
        return check_mail_page(h, values["email"])
    age = form_token_age(f.get("ftoken"), "signup")
    if age is None:
        return signup_page(h, "The form was open too long. Send it again.", values, 400)
    if age < 3:
        return signup_page(h, "That was quick. Check the form and send it again.", values, 400)
    if not mail_ready():
        return signup_page(h, "Sign-up with an e-mail address is not open at the moment.", values, 503)
    problems = []
    existing = h.c.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone() if email else None
    if existing and not existing["active"]:
        h.c.execute("DELETE FROM accounts WHERE id = ?", (existing["id"],))  # an older sign-up that was never confirmed
        existing = None
    # The same checks whether or not the address has an account, so the answer does not tell which.
    problem = name_problem(h.c, name)
    if problem:
        problems.append(problem)
    if not email:
        problems.append("That does not look like an e-mail address.")
    problem = password_problem(password, name, email)
    if problem:
        problems.append(problem)
    if problems:
        return signup_page(h, " ".join(problems), values, 400)
    if not take(h.c, "signup", h.who()):
        return signup_page(h, "Enough accounts were made from here today. Try again tomorrow.", values, 429)
    if existing:
        if take(h.c, "letters", str(existing["id"])):
            h.mail(email, "Your Kiln account", LETTER_EXISTS % {"name": existing["name"], "origin": ORIGIN})
        return check_mail_page(h, email)
    cur = h.c.execute("INSERT INTO accounts (name, created, email, password, active) VALUES (?, ?, ?, ?, 0)",
                      (name, now(), email, hash_password(password)))
    account_id = cur.lastrowid
    record(h.c, "letters", str(account_id))
    raw = new_token(h.c, "confirm", account_id, CONFIRM_TTL)
    if not h.mail(email, "Confirm your Kiln account",
                  LETTER_CONFIRM % {"name": name, "link": "%s/account/confirm?token=%s" % (ORIGIN, raw)}):
        h.c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return signup_page(h, "No more letters can be sent today. Try again tomorrow.", values, 503)
    check_mail_page(h, email)


def link_failed_page(h, title, message):
    body = error_block(message) + ('<p><a href="/account/signin">Sign in</a>, or <a href="/account/reset">choose a new '
                                   'password</a> to get a working link.</p>\n')
    h.page(title, sheet(title, "", body), 400, error=True)


def confirm_get(h):
    row = find_token(h.c, h.query.get("token", ""), ("confirm", "email"))
    if not row:
        return link_failed_page(h, "This link does not work",
                                "The link has expired or was already used. If you confirmed your address already, sign in.")
    account = h.c.execute("SELECT * FROM accounts WHERE id = ?", (row["account"],)).fetchone()
    if account["banned"]:
        return link_failed_page(h, "This account is banned", "This account is banned.")
    done = "confirmed"
    if row["kind"] == "confirm":
        h.c.execute("UPDATE accounts SET active = 1 WHERE id = ?", (account["id"],))
    else:
        data = json.loads(row["data"] or "{}")
        other = h.c.execute("SELECT * FROM accounts WHERE email = ? AND id != ?", (data.get("email"), account["id"])).fetchone()
        if other and other["active"]:
            h.c.execute("DELETE FROM tokens WHERE token = ?", (row["token"],))
            return link_failed_page(h, "This address is taken", "That address now belongs to another account.")
        if other:
            h.c.execute("DELETE FROM accounts WHERE id = ?", (other["id"],))
        h.c.execute("UPDATE accounts SET email = ?, password = ? WHERE id = ?", (data["email"], data["password"], account["id"]))
        done = "email-added"
    h.c.execute("DELETE FROM tokens WHERE account = ? AND kind = ?", (account["id"], row["kind"]))
    h.sign_in(account["id"])
    h.redirect("/account/?done=" + done)


def reset_request_page(h, error="", sent=False, code=200):
    if sent:
        body = ("<p>If an account uses that address, we sent a letter to it with a link to choose a new password. "
                "The link works for one hour.</p>\n")
    else:
        body = error_block(error) + """<form class="form" method="post" action="/account/reset">
%s
<p class="form-field"><label for="f-email">E-mail address of the account</label><input id="f-email" name="email" type="email" required maxlength="254" autocomplete="email"></p>
<p class="form-actions"><button class="btn-fire" type="submit">Send the link</button></p>
</form>
<p class="form-hint">Signed up with another service, with no address? Sign in with that service; if you cannot, write to %s from where we can tell the account is yours.</p>
""" % (hidden("ftoken", form_token("reset")), esc(CONTACT))
    note = '      <p class="sheet-note">We send a link to the account\'s address; it lets you choose a new password.</p>\n'
    h.page("A new password", sheet("A new password", note, body), code, error=bool(error))


def reset_get(h):
    raw = h.query.get("token", "")
    if not raw:
        return reset_request_page(h)
    row = find_token(h.c, raw, ("reset",))
    if not row:
        return link_failed_page(h, "This link does not work", "The link has expired or was already used. Ask for a new one.")
    # The token moves out of the address bar into the session, so it never reaches a Referer or a log.
    h.set_pending({"reset": row["account"], "token": row["token"], "at": now()}, PENDING_TTL)
    h.redirect("/account/reset/new")


def reset_post(h):
    f = h.form
    if form_token_age(f.get("ftoken"), "reset") is None:
        return reset_request_page(h, "The form was open too long. Send it again.", code=400)
    email = norm_email(f.get("email"))
    if not email:
        return reset_request_page(h, "That does not look like an e-mail address.", code=400)
    if not take(h.c, "reset-addr", h.who()) or not take(h.c, "reset-mail", keyed("mail|" + email)):
        return reset_request_page(h, "Enough links were asked for. Try again in an hour.", code=429)
    account = h.c.execute("SELECT * FROM accounts WHERE email = ? AND active = 1 AND banned = 0", (email,)).fetchone()
    if account:
        raw = new_token(h.c, "reset", account["id"], RESET_TTL)
        h.mail(email, "A new password for your Kiln account",
               LETTER_RESET % {"name": account["name"], "link": "%s/account/reset?token=%s" % (ORIGIN, raw)})
    reset_request_page(h, sent=True)


def reset_pending(h):
    p = h.pending()
    if not p.get("reset") or now() - int(p.get("at") or 0) > PENDING_TTL:
        return None
    row = h.c.execute("SELECT * FROM tokens WHERE token = ? AND kind = 'reset' AND expires > ?", (p.get("token", ""), now())).fetchone()
    if not row or row["account"] != p["reset"]:
        return None
    return h.c.execute("SELECT * FROM accounts WHERE id = ? AND banned = 0", (row["account"],)).fetchone()


def reset_new_page(h, account, error="", code=200):
    body = error_block(error) + """<form class="form" method="post" action="/account/reset/new">
%s
<p class="form-field"><label for="f-password">New password</label><input id="f-password" name="password" type="password" required minlength="10" maxlength="200" autocomplete="new-password" aria-describedby="f-password-hint"><span class="form-hint" id="f-password-hint">At least 10 characters.</span></p>
<p class="form-actions"><button class="btn-fire" type="submit">Set the password</button></p>
</form>
""" % hidden("csrf", h.csrf())
    note = '      <p class="sheet-note">For the account <strong>%s</strong>. Every device signed in to it is signed out.</p>\n' % esc(account["name"])
    h.page("Choose a new password", sheet("Choose a new password", note, body), code, error=bool(error))


def reset_new_get(h):
    account = reset_pending(h)
    if not account:
        return link_failed_page(h, "This link does not work", "The link has expired or was already used. Ask for a new one.")
    reset_new_page(h, account)


def reset_new_post(h):
    account = reset_pending(h)
    if not account or not h.csrf_ok():
        return link_failed_page(h, "This link does not work", "The link has expired or was already used. Ask for a new one.")
    password = h.form.get("password", "")
    problem = password_problem(password, account["name"], account["email"] or "")
    if problem:
        return reset_new_page(h, account, problem, 400)
    h.c.execute("UPDATE accounts SET password = ? WHERE id = ?", (hash_password(password), account["id"]))
    h.c.execute("DELETE FROM tokens WHERE account = ? AND kind = 'reset'", (account["id"],))
    h.c.execute("DELETE FROM sessions WHERE account = ?", (account["id"],))
    h.sign_in(account["id"])
    h.redirect("/account/?done=reset")


def signout_post(h):
    if not h.session:
        return h.redirect("/account/signin")
    if not h.csrf_ok():
        return h.redirect("/account/")
    h.c.execute("DELETE FROM sessions WHERE token = ?", (h.session["token"],))
    h.clear_cookie()
    h.account = None
    h.session = None
    h.redirect("/account/signin?done=signedout")


# The profile

def profile_get(h, error="", code=200):
    if not h.need_account("/account/"):
        return
    a = h.account
    c = h.c
    count = c.execute("SELECT COUNT(*) FROM reports WHERE account = ?", (a["id"],)).fetchone()[0]
    linked = methods_of(c, a["id"])
    csrf = hidden("csrf", h.csrf())
    note = '      <p class="sheet-note">Joined %s.%s</p>\n' % (
        esc(long_date(a["created"])), ' <span class="mark mark-team">Kiln team</span>' if a["role"] == "team" else "")
    body = notice(h) + error_block(error)

    body += '<section class="acct-section" aria-labelledby="s-reports">\n<h2 id="s-reports">Your reports</h2>\n'
    if count:
        body += '<p>You have written %s. <a href="/account/reports">See, change or delete them</a>.</p>\n' % (
            "one report" if count == 1 else "%d reports" % count)
    else:
        body += '<p>None yet. Find a mod in the <a href="/mods.html">list of mods</a>; its page has the form.</p>\n'
    body += "</section>\n"

    if a["role"] == "team":
        body += ('<section class="acct-section" aria-labelledby="s-team">\n<h2 id="s-team">Moderation</h2>\n'
                 '<p><a href="/account/admin">Reports and accounts</a>: delete a report, ban or unban an account.</p>\n</section>\n')

    body += '<section class="acct-section" aria-labelledby="s-signin">\n<h2 id="s-signin">Signing in</h2>\n'
    if a["email"] and a["password"]:
        body += """<p>With the address <strong>%s</strong> and its password.</p>
<form class="form" method="post" action="/account/password">
%s
<p class="form-field"><label for="f-current">Current password</label><input id="f-current" name="current" type="password" required maxlength="200" autocomplete="current-password"></p>
<p class="form-field"><label for="f-new">New password</label><input id="f-new" name="password" type="password" required minlength="10" maxlength="200" autocomplete="new-password" aria-describedby="f-new-hint"><span class="form-hint" id="f-new-hint">At least 10 characters. Every other device is signed out.</span></p>
<p class="form-actions"><button class="btn-quiet" type="submit">Change the password</button></p>
</form>
""" % (esc(a["email"]), csrf)
    elif mail_ready():
        body += """<p>No e-mail address yet. Add one to sign in with it and a password as well.</p>
<form class="form" method="post" action="/account/email">
%s
<p class="form-field"><label for="f-email">E-mail address</label><input id="f-email" name="email" type="email" required maxlength="254" autocomplete="email" aria-describedby="f-email-hint"><span class="form-hint" id="f-email-hint">Never shown. We send a letter to confirm it.</span></p>
<p class="form-field"><label for="f-new">Password</label><input id="f-new" name="password" type="password" required minlength="10" maxlength="200" autocomplete="new-password"></p>
<p class="form-actions"><button class="btn-quiet" type="submit">Add the address</button></p>
</form>
""" % csrf
    shown = [p for p in PROVIDERS if p in linked or p in providers_on()]
    if shown:
        can_unlink = bool(a["email"] and a["password"]) or len(linked) > 1
        body += '<ul class="methods">\n'
        for p in shown:
            label = PROVIDERS[p]["label"]
            if p in linked:
                action = ""
                if can_unlink:
                    action = ('<form class="inline-form" method="post" action="/account/unlink">%s%s<button class="link-button" '
                              'type="submit">Unlink<span class="visually-hidden"> %s</span></button></form>' % (csrf, hidden("provider", p), esc(label)))
                body += '<li><span>%s: linked.</span> %s</li>\n' % (esc(label), action)
            else:
                body += '<li><span>%s: not linked.</span> <a href="/account/auth/%s?next=/account/">Link<span class="visually-hidden"> %s</span></a></li>\n' % (
                    esc(label), p, esc(label))
        body += "</ul>\n"
        if linked and not can_unlink:
            body += '<p class="form-hint">The only way to sign in cannot be unlinked; add an address or another service first.</p>\n'
    body += "</section>\n"

    body += """<section class="acct-section" aria-labelledby="s-out">
<h2 id="s-out">Sign out</h2>
<form method="post" action="/account/signout">%s<button class="btn-quiet" type="submit">Sign out</button></form>
</section>
<section class="acct-section" aria-labelledby="s-delete">
<h2 id="s-delete">Delete the account</h2>
<p>This deletes the account, all its reports and every way to sign in to it, at once. It cannot be undone.</p>
<form class="form" method="post" action="/account/delete">
%s
<p class="form-field"><label for="f-confirm">To confirm, type the name: %s</label><input id="f-confirm" name="confirm" required maxlength="40" autocomplete="off" spellcheck="false"></p>
<p class="form-actions"><button class="btn-quiet btn-danger" type="submit">Delete the account and its reports</button></p>
</form>
</section>
""" % (csrf, csrf, esc(a["name"]))
    h.page(a["name"], sheet(a["name"], note, body), code, error=bool(error))


def password_post(h):
    if not h.need_account("/account/"):
        return
    if not h.csrf_ok():
        return profile_get(h, "The form expired. Send it again.", 400)
    a = h.account
    if not a["password"]:
        return profile_get(h, "This account has no password yet.", 400)
    if not allowed(h.c, "signin-fail-mail", keyed("acct|%d" % a["id"])):
        return profile_get(h, "Too many wrong passwords. Wait a quarter of an hour.", 429)
    if not check_password(h.form.get("current", "")[:200], a["password"]):
        record(h.c, "signin-fail-mail", keyed("acct|%d" % a["id"]))
        return profile_get(h, "The current password is not right.", 400)
    password = h.form.get("password", "")
    problem = password_problem(password, a["name"], a["email"] or "")
    if problem:
        return profile_get(h, problem, 400)
    h.c.execute("UPDATE accounts SET password = ? WHERE id = ?", (hash_password(password), a["id"]))
    h.c.execute("DELETE FROM sessions WHERE account = ? AND token != ?", (a["id"], h.session["token"]))
    h.redirect("/account/?done=password")


def email_post(h):
    if not h.need_account("/account/"):
        return
    if not h.csrf_ok():
        return profile_get(h, "The form expired. Send it again.", 400)
    a = h.account
    if a["email"]:
        return profile_get(h, "This account has an address already.", 400)
    email = norm_email(h.form.get("email"))
    password = h.form.get("password", "")
    if not email:
        return profile_get(h, "That does not look like an e-mail address.", 400)
    problem = password_problem(password, a["name"], email)
    if problem:
        return profile_get(h, problem, 400)
    other = h.c.execute("SELECT * FROM accounts WHERE email = ? AND active = 1", (email,)).fetchone()
    if other:
        return profile_get(h, "That address belongs to another account.", 400)
    if not take(h.c, "letters", str(a["id"])):
        return profile_get(h, "Enough letters for now. Try again in an hour.", 429)
    raw = new_token(h.c, "email", a["id"], CONFIRM_TTL, {"email": email, "password": hash_password(password)})
    if not h.mail(email, "Confirm the address for your Kiln account",
                  LETTER_EMAIL % {"name": a["name"], "link": "%s/account/confirm?token=%s" % (ORIGIN, raw)}):
        return profile_get(h, "No more letters can be sent today. Try again tomorrow.", 503)
    h.redirect("/account/?done=email-sent")


def unlink_post(h):
    if not h.need_account("/account/"):
        return
    if not h.csrf_ok():
        return profile_get(h, "The form expired. Send it again.", 400)
    a = h.account
    p = h.form.get("provider", "")
    linked = methods_of(h.c, a["id"])
    if p not in linked:
        return h.redirect("/account/")
    if not (a["email"] and a["password"]) and len(linked) < 2:
        return profile_get(h, "The only way to sign in cannot be unlinked.", 400)
    h.c.execute("DELETE FROM identities WHERE account = ? AND provider = ?", (a["id"], p))
    h.redirect("/account/?done=unlinked")


def delete_post(h):
    if not h.need_account("/account/"):
        return
    if not h.csrf_ok():
        return profile_get(h, "The form expired. Send it again.", 400)
    a = h.account
    if h.form.get("confirm", "").strip().lower() != a["name"].lower():
        return profile_get(h, "The name you typed does not match, so nothing was deleted.", 400)
    h.c.execute("DELETE FROM accounts WHERE id = ?", (a["id"],))
    h.clear_cookie()
    h.account = None
    h.session = None
    body = ("<p>The account <strong>%s</strong>, its reports and every way to sign in to it are deleted.</p>\n"
            '<p><a href="/index.html">Kiln\'s home page</a></p>\n' % esc(a["name"]))
    h.page("Account deleted", sheet("Account deleted", "", body))


# Reports: the JSON the mod pages read, the page that works without JavaScript, and the writes

def me_get(h):
    if not h.account:
        return h.send_json(200, {"signedIn": False})
    data = {"signedIn": True, "name": h.account["name"], "team": h.account["role"] == "team", "csrf": h.csrf()}
    mod_id = h.query.get("mod", "")
    if ID.match(mod_id):
        data["mine"] = mine_json(h.c, h.account["id"], mod_id)
    h.send_json(200, data)


def summary_json_get(h):
    mods = {}
    for row in h.c.execute("""SELECT r.mod, r.result, COUNT(*) AS k FROM reports r JOIN accounts a ON a.id = r.account
                              WHERE a.banned = 0 GROUP BY r.mod, r.result"""):
        mods.setdefault(row["mod"], {"works": 0, "partly": 0, "broken": 0})[row["result"]] = row["k"]
    h.send_json(200, {"mods": mods}, "no-cache")


def report_json_get(h, mod_id):
    if not mod_info(mod_id):
        return h.send_json(404, {"error": "no such mod"})
    h.send_json(200, summary_for(h.c, mod_id), "no-cache")


def reports_page(h, mod_id, error="", values=None, code=200):
    info = mod_info(mod_id)
    if not info:
        return h.not_found()
    c = h.c
    rows = report_rows(c, mod_id, PAGE_REPORTS)
    counts = counts_for(c, mod_id)
    note = ('      <p class="sheet-note"><a href="/mods/%s.html">The mod\'s page</a></p>\n'
            '      <p class="sheet-note">%s</p>\n' % (esc(mod_id), esc(counts_line(counts))))
    body = notice(h)
    body += '<section class="acct-section" aria-labelledby="s-reports">\n<h2 id="s-reports">Reports from players</h2>\n'
    if rows:
        body += '<ul class="reports-list">\n%s</ul>\n' % "".join(report_item(r) for r in rows)
        if sum(counts.values()) > len(rows):
            body += '<p class="form-hint">The newest %d are shown.</p>\n' % len(rows)
    else:
        body += "<p>No reports from players yet.</p>\n"
    body += "</section>\n"

    body += '<section class="acct-section" id="write" aria-labelledby="s-write">\n<h2 id="s-write">Your report</h2>\n'
    if not h.account:
        back = "/reports/%s#write" % mod_id
        body += ('<p><a href="/account/signin?next=%s">Sign in</a> or <a href="/account/signup?next=%s">create an account</a> '
                 'to write a report.</p>\n' % (esc(quote(back)), esc(quote(back))))
        body += "</section>\n"
        return h.page("%s: reports" % info["name"], sheet(info["name"], note, body), code)

    csrf = hidden("csrf", h.csrf())
    mine = c.execute("""SELECT r.*, a.name, a.role FROM reports r JOIN accounts a ON a.id = r.account
                        WHERE r.account = ? AND r.mod = ? ORDER BY r.updated DESC""", (h.account["id"], mod_id)).fetchall()
    if mine:
        items = ""
        for r in mine:
            actions = ('<div class="report-actions"><a href="/reports/%s?edit=%d#write">Change<span class="visually-hidden"> the report for %s</span></a> '
                       '<form class="inline-form" method="post" action="/reports/%s/delete">%s%s%s<button class="link-button" type="submit">'
                       'Delete<span class="visually-hidden"> the report for %s</span></button></form></div>\n'
                       % (esc(mod_id), r["n"], esc(build_label(r["channel"], r["build"])), esc(mod_id), csrf, hidden("n", r["n"]),
                          hidden("back", "/reports/" + mod_id), esc(build_label(r["channel"], r["build"]))))
            items += report_item(r, actions=actions)
        body += '<ul class="reports-list mine">\n%s</ul>\n' % items

    values = values or {}
    edit = None
    edit_n = str(values.get("n") or h.query.get("edit") or "")
    if edit_n.isdigit():
        edit = next((r for r in mine if r["n"] == int(edit_n)), None)
    current = builds()
    result = values.get("result") or (edit["result"] if edit else "")
    radios = "".join('<label><input type="radio" name="result" value="%s"%s%s> %s</label>\n' % (
        k, " required" if k == "works" else "", " checked" if result == k else "", esc(label))
        for k, label in (("works", "It works"), ("partly", "Partly"), ("broken", "It doesn't work")))
    if edit:
        build_html = '<p class="report-field">Build: %s</p>\n%s\n' % (esc(build_label(edit["channel"], edit["build"])), hidden("n", edit["n"]))
    elif current:
        chosen = values.get("channel") or current[0]["value"]
        options = "".join('<option value="%s"%s>%s</option>' % (b["value"], " selected" if b["value"] == chosen else "", esc(b["label"]))
                          for b in current)
        build_html = ('<p class="report-field"><label for="report-build">Kiln build</label>'
                      '<select id="report-build" name="channel">%s</select></p>\n' % options)
    else:
        build_html = ""
    version = values.get("version") if "version" in values else (edit["version"] if edit else info["version"])
    text = values.get("text") if "text" in values else (edit["text"] if edit else "")
    if not current and not edit:
        body += "<p>Reports cannot be written at the moment: the catalogue names no Kiln build.</p>\n</section>\n"
        return h.page("%s: reports" % info["name"], sheet(info["name"], note, body), code)
    body += error_block(error)
    body += """<form class="report-form" method="post" action="/reports/%s">
%s%s
<fieldset class="report-choice">
<legend>Did it run for you in Horizon?</legend>
%s</fieldset>
%s<p class="report-field"><label for="report-version">Mod version</label><input id="report-version" name="version" maxlength="64" autocomplete="off" spellcheck="false" value="%s"></p>
<p class="report-field"><label for="report-text">What you saw, optional</label><textarea id="report-text" name="text" maxlength="400" rows="4">%s</textarea></p>
<p class="report-note">Shown at once with your name and the date. One report per build: another for the same build replaces it.</p>
<p class="report-actions"><button type="submit" class="report-send">%s</button>%s</p>
</form>
""" % (esc(mod_id), csrf, hidden("back", "/reports/" + mod_id), radios, build_html, esc(version), esc(text),
       "Save the change" if edit else "Send the report",
       ' <a href="/reports/%s#write">Write a new one instead</a>' % esc(mod_id) if edit else "")
    body += "</section>\n"
    h.page("%s: reports" % info["name"], sheet(info["name"], note, body), code, error=bool(error))


def reports_page_get(h, mod_id):
    reports_page(h, mod_id)


def report_back(h, mod_id):
    back = h.form.get("back", "")
    allowed_backs = ("/reports/" + mod_id, "/mods/%s.html" % mod_id, "/account/reports")
    return back if back in allowed_backs else "/reports/" + mod_id


def report_reply(h, mod_id, done):
    if h.wants_json():
        return h.send_json(200, {"ok": True, "summary": summary_for(h.c, mod_id), "mine": mine_json(h.c, h.account["id"], mod_id)})
    back = report_back(h, mod_id)
    if back.startswith("/mods/"):
        return h.redirect(back + "#reports")
    h.redirect(back + "?done=" + done + ("#write" if back.startswith("/reports/") else ""))


def report_error(h, mod_id, code, message, values=None):
    if h.wants_json():
        return h.send_json(code, {"error": message})
    reports_page(h, mod_id, message, values, code)


def report_post(h, mod_id):
    if not mod_info(mod_id):
        return h.not_found()
    if not h.account:
        back = "/reports/%s#write" % mod_id
        if h.wants_json():
            return h.send_json(401, {"error": "Sign in to write a report.", "signin": "/account/signin?next=" + quote(back)})
        return h.redirect("/account/signin?next=" + quote(back))
    f = h.form
    values = {k: f.get(k, "") for k in ("result", "channel", "version", "text", "n")}
    if not h.csrf_ok():
        return report_error(h, mod_id, 403, "The form expired. Send it again.", values)
    result = f.get("result", "")
    if result not in RESULTS:
        return report_error(h, mod_id, 400, "Choose whether it works, partly works or does not work.", values)
    version = clean_line(f.get("version"), VERSION_MAX)
    text = clean_text(f.get("text"), TEXT_MAX)
    account = h.account["id"]
    if not allowed(h.c, "report", str(account)):
        return report_error(h, mod_id, 429, "That is enough reports for one day. Try again tomorrow.", values)
    t = now()
    n = f.get("n", "")
    if n.isdigit():
        row = h.c.execute("SELECT * FROM reports WHERE n = ? AND account = ? AND mod = ?", (int(n), account, mod_id)).fetchone()
        if not row:
            return report_error(h, mod_id, 404, "That report is gone. Write a new one.", values)
        h.c.execute("UPDATE reports SET result = ?, version = ?, text = ?, updated = ? WHERE n = ?", (result, version, text, t, row["n"]))
    else:
        chosen = next((b for b in builds() if b["value"] == f.get("channel")), None)
        if not chosen:
            return report_error(h, mod_id, 400, "Choose the Kiln build you ran.", values)
        h.c.execute("""INSERT INTO reports (mod, account, channel, build, version, result, text, created, updated)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(account, mod, build) DO UPDATE SET version = excluded.version, result = excluded.result,
                       text = excluded.text, updated = excluded.updated""",
                    (mod_id, account, chosen["value"], chosen["build"], version, result, text, t, t))
    record(h.c, "report", str(account))
    report_reply(h, mod_id, "saved")


def report_delete_post(h, mod_id):
    if not h.account:
        if h.wants_json():
            return h.send_json(401, {"error": "Sign in first."})
        return h.redirect("/account/signin?next=" + quote("/reports/" + mod_id))
    if not h.csrf_ok():
        return report_error(h, mod_id, 403, "The form expired. Send it again.")
    n = h.form.get("n", "")
    if n.isdigit():
        h.c.execute("DELETE FROM reports WHERE n = ? AND account = ? AND mod = ?", (int(n), h.account["id"], mod_id))
    report_reply(h, mod_id, "deleted")


def my_reports_get(h):
    if not h.need_account("/account/reports"):
        return
    rows = h.c.execute("""SELECT r.*, a.name, a.role FROM reports r JOIN accounts a ON a.id = r.account
                          WHERE r.account = ? ORDER BY r.updated DESC""", (h.account["id"],)).fetchall()
    csrf = hidden("csrf", h.csrf())
    body = notice(h)
    if not rows:
        body += '<p>None yet. Find a mod in the <a href="/mods.html">list of mods</a>; its page has the form.</p>\n'
    else:
        items = ""
        for r in rows:
            what = "%s, %s" % (mod_name(r["mod"]), build_label(r["channel"], r["build"]))
            actions = ('<div class="report-actions"><a href="/reports/%s?edit=%d#write">Change<span class="visually-hidden"> the report on %s</span></a> '
                       '<form class="inline-form" method="post" action="/reports/%s/delete">%s%s%s<button class="link-button" type="submit">'
                       'Delete<span class="visually-hidden"> the report on %s</span></button></form></div>\n'
                       % (esc(r["mod"]), r["n"], esc(what), esc(r["mod"]), csrf, hidden("n", r["n"]), hidden("back", "/account/reports"), esc(what)))
            items += report_item(r, with_mod=True, actions=actions)
        body += '<ul class="reports-list">\n%s</ul>\n' % items
    note = '      <p class="sheet-note">Everything you wrote, newest first. <a href="/account/">Back to the account</a>.</p>\n'
    h.page("Your reports", sheet("Your reports", note, body))


# Moderation, for accounts with the team role

def admin_get(h, error="", code=200):
    if not h.need_account("/account/admin"):
        return
    if h.account["role"] != "team":
        return h.not_found()
    c = h.c
    name = clean_line(h.query.get("name"), 40)
    mod = h.query.get("mod", "") if ID.match(h.query.get("mod", "")) else ""
    csrf = hidden("csrf", h.csrf())
    body = notice(h) + error_block(error)
    body += """<form class="toolbar admin-search" method="get" action="/account/admin">
<p class="form-field"><label for="a-name">Name contains</label><input id="a-name" name="name" maxlength="40" value="%s"></p>
<p class="form-field"><label for="a-mod">Mod id</label><input id="a-mod" name="mod" maxlength="81" value="%s"></p>
<p class="form-actions"><button class="btn-quiet" type="submit">Show</button> <a href="/account/admin">Everything</a></p>
</form>
""" % (esc(name), esc(mod))

    where, args = ["1"], []
    if name:
        where.append("a.name LIKE ? ESCAPE '\\'")
        args.append("%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if mod:
        where.append("r.mod = ?")
        args.append(mod)
    rows = c.execute("""SELECT r.*, a.name, a.role, a.banned FROM reports r JOIN accounts a ON a.id = r.account
                        WHERE %s ORDER BY r.updated DESC LIMIT 50""" % " AND ".join(where), args).fetchall()
    body += '<section class="acct-section" aria-labelledby="s-a-reports">\n<h2 id="s-a-reports">Reports</h2>\n'
    if rows:
        trs = ""
        for r in rows:
            trs += ('<tr><td>%s</td><td><a href="/mods/%s.html">%s</a></td><td>%s%s</td><td><span class="mark %s">%s</span></td>'
                    '<td>%s</td><td class="admin-text">%s</td><td><form class="inline-form" method="post" action="/account/admin">%s%s%s'
                    '<button class="link-button" type="submit">Delete<span class="visually-hidden"> report %d</span></button></form></td></tr>\n'
                    % (esc(iso_date(r["updated"])), esc(r["mod"]), esc(r["mod"]), esc(r["name"]), " (banned)" if r["banned"] else "",
                       MARKS.get(r["result"], "mark-unknown"), esc(RESULTS.get(r["result"], r["result"])),
                       esc(build_label(r["channel"], r["build"])), esc(r["text"]), csrf, hidden("action", "delete-report"),
                       hidden("target", r["n"]), r["n"]))
        body += ('<div class="table-wrap" role="region" aria-label="Reports" tabindex="0"><table class="acct-table">\n'
                 '<thead><tr><th scope="col">Date</th><th scope="col">Mod</th><th scope="col">Name</th><th scope="col">Result</th>'
                 '<th scope="col">Build</th><th scope="col">Text</th><th scope="col"><span class="visually-hidden">Action</span></th></tr></thead>\n'
                 '<tbody>\n%s</tbody></table></div>\n' % trs)
    else:
        body += "<p>No reports match.</p>\n"
    body += "</section>\n"

    where, args = ["active = 1"], []
    if name:
        where.append("name LIKE ? ESCAPE '\\'")
        args.append("%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    accounts = c.execute("SELECT * FROM accounts WHERE %s ORDER BY created DESC LIMIT 30" % " AND ".join(where), args).fetchall()
    body += '<section class="acct-section" aria-labelledby="s-a-accounts">\n<h2 id="s-a-accounts">Accounts</h2>\n'
    if accounts:
        trs = ""
        for a in accounts:
            ways = (["e-mail"] if a["email"] else []) + [PROVIDERS[p]["label"] for p in methods_of(c, a["id"]) if p in PROVIDERS]
            k = c.execute("SELECT COUNT(*) FROM reports WHERE account = ?", (a["id"],)).fetchone()[0]
            status = "Kiln team" if a["role"] == "team" else ("banned" if a["banned"] else "")
            action = ""
            if a["role"] != "team":
                verb = "unban" if a["banned"] else "ban"
                action = ('<form class="inline-form" method="post" action="/account/admin">%s%s%s<button class="link-button" type="submit">'
                          '%s<span class="visually-hidden"> %s</span></button></form>' % (csrf, hidden("action", verb), hidden("target", a["name"]),
                                                                                       verb.capitalize(), esc(a["name"])))
            trs += ('<tr><td><a href="/account/admin?name=%s">%s</a></td><td>%s</td><td>%s</td><td>%d</td><td>%s</td><td>%s</td></tr>\n'
                    % (esc(quote(a["name"])), esc(a["name"]), esc(iso_date(a["created"])), esc(", ".join(ways)), k, esc(status), action))
        body += ('<div class="table-wrap" role="region" aria-label="Accounts" tabindex="0"><table class="acct-table">\n'
                 '<thead><tr><th scope="col">Name</th><th scope="col">Joined</th><th scope="col">Signs in with</th><th scope="col">Reports</th>'
                 '<th scope="col">Status</th><th scope="col"><span class="visually-hidden">Action</span></th></tr></thead>\n'
                 '<tbody>\n%s</tbody></table></div>\n' % trs)
    else:
        body += "<p>No accounts match.</p>\n"
    body += "</section>\n"

    entries = c.execute("SELECT * FROM log ORDER BY at DESC LIMIT 30").fetchall()
    body += '<section class="acct-section" aria-labelledby="s-a-log">\n<h2 id="s-a-log">What the team did</h2>\n'
    if entries:
        body += '<ul class="admin-log">\n%s</ul>\n' % "".join(
            "<li>%s: %s %s %s</li>\n" % (esc(iso_date(e["at"])), esc(e["actor"]), esc(e["action"]), esc(e["target"])) for e in entries)
    else:
        body += "<p>Nothing yet.</p>\n"
    body += "</section>\n"
    note = ('      <p class="sheet-note">Delete a report that is not about running the mod in Horizon; ban an account that keeps '
            'sending such reports. A banned account cannot sign in, and its reports are hidden until it is unbanned.</p>\n')
    h.page("Moderation", sheet("Moderation", note, body), code, error=bool(error))


def admin_post(h):
    if not h.need_account("/account/admin"):
        return
    if h.account["role"] != "team":
        return h.not_found()
    if not h.csrf_ok():
        return admin_get(h, "The form expired. Send it again.", 400)
    action, target = h.form.get("action", ""), h.form.get("target", "")
    actor = h.account["name"]
    c = h.c
    if action == "delete-report" and target.isdigit():
        row = c.execute("SELECT r.n, r.mod, a.name FROM reports r JOIN accounts a ON a.id = r.account WHERE r.n = ?", (int(target),)).fetchone()
        if row:
            c.execute("DELETE FROM reports WHERE n = ?", (row["n"],))
            add_log(c, actor, "deleted", "report %d on %s by %s" % (row["n"], row["mod"], row["name"]))
    elif action in ("ban", "unban"):
        a = c.execute("SELECT * FROM accounts WHERE name = ?", (target,)).fetchone()
        if not a or a["role"] == "team":
            return admin_get(h, "That account cannot be changed here.", 400)
        c.execute("UPDATE accounts SET banned = ? WHERE id = ?", (1 if action == "ban" else 0, a["id"]))
        if action == "ban":
            c.execute("DELETE FROM sessions WHERE account = ?", (a["id"],))
        add_log(c, actor, "banned" if action == "ban" else "unbanned", "account %s" % a["name"])
    else:
        return admin_get(h, "Unknown action.", 400)
    h.redirect("/account/admin?done=done")


# Sign-in with another service

def auth_start(h, p):
    if p not in providers_on():
        return h.not_found()
    if not take(h.c, "auth", h.who()):
        return h.fail(429, "Too many sign-ins from here. Try again in an hour.")
    conf = PROVIDERS[p]
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    pending = {"p": p, "state": state, "verifier": verifier, "next": safe_next(h.query.get("next")), "at": now(),
               "link": h.account["id"] if h.account else 0}
    h.set_pending(pending, None if h.account else PENDING_TTL)
    params = {"response_type": "code", "client_id": provider_keys(p)[0], "redirect_uri": redirect_uri(p), "state": state}
    if conf["scope"]:
        params["scope"] = conf["scope"]
    if conf["pkce"]:
        params["code_challenge"] = b64e(hashlib.sha256(verifier.encode()).digest())
        params["code_challenge_method"] = "S256"
    h.redirect(conf["authorize"] + "?" + urllib.parse.urlencode(params))


def auth_callback(h, p):
    if p not in PROVIDERS:
        return h.not_found()
    pending = h.pending()
    good = (pending.get("p") == p and now() - int(pending.get("at") or 0) <= PENDING_TTL
            and hmac.compare_digest(str(pending.get("state", "")), h.query.get("state", "")))
    linking = bool(good and pending.get("link") and h.account and pending["link"] == h.account["id"])
    fail_to = "/account/?done=" if linking else "/account/signin?done="
    if h.session and pending:
        h.set_pending(None)
    if not good or not h.query.get("code") or p not in providers_on():
        return h.redirect(fail_to + "failed")
    try:
        subject, suggested = oauth_identity(p, h.query["code"], pending.get("verifier", ""))
    except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
        log("auth: %s failed (%s)" % (p, type(error).__name__))
        return h.redirect(fail_to + "failed")
    row = h.c.execute("SELECT * FROM identities WHERE provider = ? AND subject = ?", (p, subject)).fetchone()
    if linking:
        if row and row["account"] != h.account["id"]:
            return h.redirect("/account/?done=taken")
        if not row:
            h.c.execute("DELETE FROM identities WHERE account = ? AND provider = ?", (h.account["id"], p))
            h.c.execute("INSERT INTO identities (provider, subject, account, created) VALUES (?, ?, ?, ?)", (p, subject, h.account["id"], now()))
        return h.redirect("/account/?done=linked")
    if row:
        account = h.c.execute("SELECT * FROM accounts WHERE id = ?", (row["account"],)).fetchone()
        if account["banned"]:
            return h.fail(403, "This account is banned. If you think that is a mistake, write to %s." % CONTACT)
        h.sign_in(account["id"])
        return h.redirect(safe_next(pending.get("next")))
    h.set_pending({"new": {"p": p, "sub": subject, "name": suggested[:40]}, "next": pending.get("next"), "at": now()}, WELCOME_TTL)
    h.redirect("/account/welcome")


def welcome_pending(h):
    p = h.pending()
    new = p.get("new") or {}
    if not new.get("sub") or new.get("p") not in PROVIDERS or now() - int(p.get("at") or 0) > WELCOME_TTL:
        return None, p
    return new, p


def welcome_page(h, new, error="", name=None, code=200):
    label = PROVIDERS[new["p"]]["label"]
    if name is None:
        name = new.get("name", "")
        if name_problem(h.c, name):
            name = ""
    body = error_block(error) + """<form class="form" method="post" action="/account/welcome">
%s
<p class="form-field"><label for="f-name">Name</label><input id="f-name" name="name" required minlength="3" maxlength="24" pattern="[A-Za-z0-9][A-Za-z0-9_\\-]{2,23}" autocomplete="nickname" spellcheck="false" aria-describedby="f-name-hint" value="%s"><span class="form-hint" id="f-name-hint">Shown with your reports. 3 to 24 letters, digits, - or _.</span></p>
<p class="form-actions"><button class="btn-fire" type="submit">Create the account</button> <a href="/account/signin">Cancel</a></p>
</form>
<p class="form-hint">From %s we keep only the id of your account there. <a href="/privacy.html">Your data</a>.</p>
""" % (hidden("csrf", h.csrf()), esc(name), esc(label))
    note = ('      <p class="sheet-note">You signed in with %s for the first time. Choose the name your reports show; '
            'it is public.</p>\n' % esc(label))
    h.page("Choose a name", sheet("Choose a name", note, body), code, error=bool(error))


def welcome_get(h):
    new, _ = welcome_pending(h)
    if not new:
        return h.redirect("/account/signin")
    welcome_page(h, new)


def welcome_post(h):
    new, pending = welcome_pending(h)
    if not new or not h.csrf_ok():
        return h.redirect("/account/signin?done=failed")
    name = clean_line(h.form.get("name"), 40)
    problem = name_problem(h.c, name)
    if problem:
        return welcome_page(h, new, problem, name, 400)
    if h.c.execute("SELECT 1 FROM identities WHERE provider = ? AND subject = ?", (new["p"], new["sub"])).fetchone():
        return h.redirect("/account/signin?done=failed")
    if not take(h.c, "signup", h.who()):
        return welcome_page(h, new, "Enough accounts were made from here today. Try again tomorrow.", name, 429)
    cur = h.c.execute("INSERT INTO accounts (name, created, active) VALUES (?, ?, 1)", (name, now()))
    h.c.execute("INSERT INTO identities (provider, subject, account, created) VALUES (?, ?, ?, ?)", (new["p"], new["sub"], cur.lastrowid, now()))
    h.sign_in(cur.lastrowid)
    nxt = safe_next(pending.get("next"))
    h.redirect(nxt if nxt != "/account/" else "/account/?done=welcome")


ROUTES = [(m, re.compile(p), f) for m, p, f in (
    ("GET", r"/account/", profile_get),
    ("GET", r"/account/signin", signin_get),
    ("POST", r"/account/signin", signin_post),
    ("GET", r"/account/signup", signup_get),
    ("POST", r"/account/signup", signup_post),
    ("POST", r"/account/signout", signout_post),
    ("GET", r"/account/confirm", confirm_get),
    ("GET", r"/account/reset", reset_get),
    ("POST", r"/account/reset", reset_post),
    ("GET", r"/account/reset/new", reset_new_get),
    ("POST", r"/account/reset/new", reset_new_post),
    ("POST", r"/account/password", password_post),
    ("POST", r"/account/email", email_post),
    ("POST", r"/account/unlink", unlink_post),
    ("POST", r"/account/delete", delete_post),
    ("GET", r"/account/reports", my_reports_get),
    ("GET", r"/account/admin", admin_get),
    ("POST", r"/account/admin", admin_post),
    ("GET", r"/account/me\.json", me_get),
    ("GET", r"/account/auth/([a-z]+)", auth_start),
    ("GET", r"/account/auth/([a-z]+)/callback", auth_callback),
    ("GET", r"/account/welcome", welcome_get),
    ("POST", r"/account/welcome", welcome_post),
    ("GET", r"/reports/summary\.json", summary_json_get),
    ("GET", r"/reports/([a-z0-9][a-z0-9-]{0,80})\.json", report_json_get),
    ("GET", r"/reports/([a-z0-9][a-z0-9-]{0,80})", reports_page_get),
    ("POST", r"/reports/([a-z0-9][a-z0-9-]{0,80})", report_post),
    ("POST", r"/reports/([a-z0-9][a-z0-9-]{0,80})/delete", report_delete_post),
)]


# Running

def cleaner():
    while True:
        try:
            with database() as c:
                cleanup(c)
        except sqlite3.Error as error:
            log("cleanup failed: %s" % type(error).__name__)
        time.sleep(3600)


def serve():
    init_db()
    host, _, port = LISTEN.rpartition(":")
    threading.Thread(target=mail_worker, daemon=True).start()
    threading.Thread(target=cleaner, daemon=True).start()
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    server.daemon_threads = True
    log("kiln-accounts on %s:%s; mail %s; sign-in services: %s" % (
        host, port, "ready" if mail_ready() else "not set up", ", ".join(providers_on()) or "none"))
    server.serve_forever()


def cli(args):
    init_db()
    cmd, rest = (args[0], args[1:]) if args else ("help", [])
    with database() as c:
        def account_named(name):
            row = c.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
            if not row:
                raise SystemExit("no account named %s" % name)
            return row

        if cmd == "check":
            print("data:      %s" % DATA)
            print("site:      %s" % SITE_ROOT)
            print("origin:    %s" % ORIGIN)
            print("mail:      %s" % ("log to %s/outbox.txt" % DATA if MAIL_MODE == "log" else
                                     "SMTP %s:%d as %s" % (SMTP_HOST, SMTP_PORT, SMTP_USER) if mail_ready() else
                                     "not set up (SMTP_USER and SMTP_PASSWORD)"))
            for p in PROVIDERS:
                cid, secret = provider_keys(p)
                print("%-10s %s" % (p + ":", "ready" if cid and secret else "not set up (%s_CLIENT_ID and %s_CLIENT_SECRET)" % (p.upper(), p.upper())))
            print("builds:    %s" % (", ".join(b["label"] for b in builds()) or "none found in mods.json"))
            n_acc = c.execute("SELECT COUNT(*) FROM accounts WHERE active = 1").fetchone()[0]
            n_rep = c.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
            print("accounts:  %d; reports: %d" % (n_acc, n_rep))
            return 0
        if cmd == "accounts":
            text = rest[0] if rest else ""
            for a in c.execute("SELECT * FROM accounts WHERE name LIKE ? ORDER BY created DESC LIMIT 200", ("%" + text + "%",)):
                ways = (["e-mail"] if a["email"] else []) + methods_of(c, a["id"])
                k = c.execute("SELECT COUNT(*) FROM reports WHERE account = ?", (a["id"],)).fetchone()[0]
                flags = [x for x in ("team" if a["role"] == "team" else "", "banned" if a["banned"] else "",
                                     "unconfirmed" if not a["active"] else "") if x]
                print("%-24s %s  %-28s %3d reports  %s" % (a["name"], iso_date(a["created"]), ", ".join(ways), k, " ".join(flags)))
            return 0
        if cmd == "reports":
            where, values = ["1"], []
            if "--mod" in rest:
                where.append("r.mod = ?")
                values.append(rest[rest.index("--mod") + 1])
            if "--name" in rest:
                where.append("a.name = ?")
                values.append(rest[rest.index("--name") + 1])
            for r in c.execute("""SELECT r.*, a.name FROM reports r JOIN accounts a ON a.id = r.account WHERE %s
                                  ORDER BY r.updated DESC LIMIT 200""" % " AND ".join(where), values):
                print("%5d  %s  %-7s %-24s %-16s %-20s %s" % (r["n"], iso_date(r["updated"]), r["result"], r["mod"][:24], r["name"][:16],
                                                            build_label(r["channel"], r["build"])[:20], r["text"].replace("\n", " ")[:60]))
            return 0
        if cmd == "delete-report" and rest:
            for n in rest:
                row = c.execute("SELECT r.n, r.mod, a.name FROM reports r JOIN accounts a ON a.id = r.account WHERE r.n = ?", (int(n),)).fetchone()
                if row:
                    c.execute("DELETE FROM reports WHERE n = ?", (row["n"],))
                    add_log(c, "ssh", "deleted", "report %d on %s by %s" % (row["n"], row["mod"], row["name"]))
                    print("deleted report %d" % row["n"])
            return 0
        if cmd in ("ban", "unban") and len(rest) == 1:
            a = account_named(rest[0])
            c.execute("UPDATE accounts SET banned = ? WHERE id = ?", (1 if cmd == "ban" else 0, a["id"]))
            if cmd == "ban":
                c.execute("DELETE FROM sessions WHERE account = ?", (a["id"],))
            add_log(c, "ssh", cmd + "ned", "account %s" % a["name"])
            print("%sned %s" % (cmd, a["name"]))
            return 0
        if cmd == "role" and len(rest) == 2 and rest[1] in ("team", "none"):
            a = account_named(rest[0])
            c.execute("UPDATE accounts SET role = ? WHERE id = ?", ("team" if rest[1] == "team" else "", a["id"]))
            add_log(c, "ssh", "gave the team role to" if rest[1] == "team" else "took the team role from", "account %s" % a["name"])
            print("%s: role %s" % (a["name"], rest[1]))
            return 0
        if cmd == "rename" and len(rest) == 2:
            a = account_named(rest[0])
            if not NAME.match(rest[1]):
                raise SystemExit("%s: not a valid name" % rest[1])
            if c.execute("SELECT 1 FROM accounts WHERE name = ? AND id != ?", (rest[1], a["id"])).fetchone():
                raise SystemExit("%s: taken" % rest[1])
            c.execute("UPDATE accounts SET name = ? WHERE id = ?", (rest[1], a["id"]))
            add_log(c, "ssh", "renamed", "account %s to %s" % (a["name"], rest[1]))
            print("renamed %s to %s" % (a["name"], rest[1]))
            return 0
        if cmd == "delete-account" and len(rest) == 1:
            a = account_named(rest[0])
            c.execute("DELETE FROM accounts WHERE id = ?", (a["id"],))
            add_log(c, "ssh", "deleted", "account %s" % a["name"])
            print("deleted %s and its reports" % a["name"])
            return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        sys.exit(cli(sys.argv[1:]))
