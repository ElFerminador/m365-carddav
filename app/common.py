"""Shared paths, configuration, user mappings and htpasswd helpers."""
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path

import bcrypt

DATA = Path(os.environ.get("DATA_DIR", "/data"))
CONF = DATA / "config"
STATE_DIR = DATA / "state"
STORAGE = DATA / "radicale" / "collections"
COLL_ROOT = STORAGE / "collection-root"

HTPASSWD = CONF / "htpasswd"
RIGHTS = CONF / "rights"
RADICALE_CONF = CONF / "radicale.conf"
USERS_FILE = CONF / "users.json"
INVITES_FILE = CONF / "invites.json"
SYNC_SECRET = CONF / "sync.secret"
KEY_FILE = CONF / "graph-key.pem"
CERT_FILE = CONF / "graph-cert.pem"
CER_FILE = CONF / "graph-cert.cer"
TLS_CERT = CONF / "tls-cert.pem"
TLS_KEY = CONF / "tls-key.pem"
TLS_CHAIN = CONF / "tls-chain.pem"          # optional: intermediate CA(s), issuing CA first
TLS_FULLCHAIN = CONF / "tls-fullchain.pem"  # generated on start: cert + chain, used by the servers
TRIGGER = STATE_DIR / "sync.now"

SYNC_USER = "sync"
COLLECTION = os.environ.get("COLLECTION", "m365").strip()

_NAME_RE = re.compile(r"^[a-z0-9._-]+$")


def check_name(label: str, value: str) -> str:
    """Returns an error message, or '' if the name is valid."""
    if not value:
        return f"{label} is empty"
    if not _NAME_RE.match(value):
        return f"{label} '{value}' is invalid (allowed: a-z 0-9 . _ -)"
    if label == "user" and value == SYNC_USER:
        return f"'{SYNC_USER}' is reserved for the internal sync user"
    return ""


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        sys.exit(f"{name} must be an integer")


def env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


def write_private(path: Path, content: str):
    """Write atomically with mode 0600."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- users
def load_users() -> dict:
    """{carddav_user: {"mailbox": "user@contoso.com"}}"""
    try:
        return json.loads(USERS_FILE.read_text())
    except FileNotFoundError:
        return {}


def save_users(users: dict):
    write_private(USERS_FILE, json.dumps(users, indent=2, sort_keys=True) + "\n")


def ensure_addressbook(user: str) -> bool:
    """Creates the address book in the user's principal. Returns True if newly created."""
    book = COLL_ROOT / user / COLLECTION
    book.mkdir(parents=True, exist_ok=True)
    props = book / ".Radicale.props"
    if props.exists():
        return False
    props.write_text(json.dumps({
        "tag": "VADDRESSBOOK",
        "D:displayname": "M365",
        "CR:addressbook-description": "Read-only mirror of the M365 contacts",
    }))
    return True


def state_file(user: str) -> Path:
    return STATE_DIR / f"{user}.json"


def trigger_sync():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TRIGGER.touch()


# --------------------------------------------------------------------------- htpasswd
def read_htpasswd() -> dict:
    users = {}
    if HTPASSWD.exists():
        for line in HTPASSWD.read_text().splitlines():
            if ":" in line:
                user, hashed = line.split(":", 1)
                users[user] = hashed
    return users


def _write_htpasswd(users: dict):
    write_private(HTPASSWD, "".join(f"{u}:{h}\n" for u, h in users.items()))


def set_htpasswd(user: str, password: str):
    users = read_htpasswd()
    users[user] = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    _write_htpasswd(users)


def del_htpasswd(user: str):
    users = read_htpasswd()
    if users.pop(user, None) is not None:
        _write_htpasswd(users)


# --------------------------------------------------------------------------- URLs
def _host():
    return os.environ.get("TLS_HOSTNAME", "").strip() or "<host>"


def carddav_url(user: str) -> str:
    base = os.environ.get("CARDDAV_URL", "").strip().rstrip("/") or f"https://{_host()}:5232"
    return f"{base}/{user}/"


def enrollment_url(token: str) -> str:
    base = os.environ.get("ENROLLMENT_URL", "").strip().rstrip("/") or f"https://{_host()}:5233"
    return f"{base}/enroll/{token}"


# --------------------------------------------------------------------------- invitations
# Only the SHA-256 of a token is stored; the token itself is shown once by `user invite`.
def _load_invites() -> dict:
    try:
        invites = json.loads(INVITES_FILE.read_text())
    except FileNotFoundError:
        return {}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return {h: i for h, i in invites.items() if i["expires"] > now}


def _save_invites(invites: dict):
    write_private(INVITES_FILE, json.dumps(invites, indent=2) + "\n")


def create_invite(user: str, hours: int):
    """Returns (token, expiry). Replaces any open invitation of this user."""
    invites = {h: i for h, i in _load_invites().items() if i["user"] != user}
    token = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)
    invites[hashlib.sha256(token.encode()).hexdigest()] = {
        "user": user, "expires": expires.isoformat(timespec="seconds")}
    _save_invites(invites)
    return token, expires


def find_invite(token_hash: str):
    return _load_invites().get(token_hash)


def consume_invite(token_hash: str) -> bool:
    invites = _load_invites()
    if invites.pop(token_hash, None) is None:
        return False
    _save_invites(invites)
    return True


def delete_invites(user: str):
    invites = _load_invites()
    keep = {h: i for h, i in invites.items() if i["user"] != user}
    if keep != invites:
        _save_invites(keep)


def pending_invite(user: str):
    """Expiry (ISO string) of an open invitation, or None."""
    for i in _load_invites().values():
        if i["user"] == user:
            return i["expires"]
    return None


# --------------------------------------------------------------------------- texts
TEXTS = {
    "en": {
        "lang": "en",
        "title": "Set up your M365 contacts",
        "intro": "Choose a password for the address book that mirrors your Microsoft 365 "
                 "contacts. It is <b>not</b> your Microsoft 365 password and is only used "
                 "by the Contacts app.",
        "user_name": "User name", "mailbox": "Mailbox", "server": "Server address",
        "new_pw": "New password", "repeat_pw": "Repeat password",
        "pw_hint": "At least {n} characters. Ideally let your password manager generate it.",
        "save": "Save password",
        "err_short": "The password must have at least {n} characters.",
        "err_mismatch": "The passwords do not match.",
        "too_large": "Request too large.",
        "invalid_title": "Link not valid",
        "invalid": "This link is invalid, expired or has already been used. "
                   "Ask your administrator for a new one.",
        "done_title": "Done",
        "done": "Your password has been saved.",
        "done_once": "This link is now invalid. Keep the password in your password manager.",
        "mac_title": "Add the account on your Mac",
        "mac_1": "System Settings → Internet Accounts → Add Account → Add Other Account → "
                 "<b>CardDAV Account</b>",
        "mac_2": "Account Type: <b>Manual</b> (not «Advanced»)",
        "mac_3": "Enter user name, the password you just set, and as server address:",
        "mac_4": "If macOS cannot verify the server identity: «Show Certificate» → "
                 "«Always Trust». The address book «M365» appears in Contacts shortly after.",
        "mail": "Hello\n\nYour Microsoft 365 contacts can now be used in the macOS Contacts app.\n"
                "Please open this link once and choose a password (it is NOT your Microsoft "
                "365 password):\n\n{url}\n\nThe link can be used once and is valid until "
                "{expires}. The page then shows how to add the account on your Mac.\n",
    },
    "de": {
        "lang": "de",
        "title": "M365-Kontakte einrichten",
        "intro": "Wähle ein Passwort für das Adressbuch, das deine Microsoft-365-Kontakte "
                 "spiegelt. Es ist <b>nicht</b> dein Microsoft-365-Passwort und wird nur "
                 "von der Kontakte-App verwendet.",
        "user_name": "Benutzername", "mailbox": "Postfach", "server": "Serveradresse",
        "new_pw": "Neues Passwort", "repeat_pw": "Passwort wiederholen",
        "pw_hint": "Mindestens {n} Zeichen. Am besten vom Passwortmanager erzeugen lassen.",
        "save": "Passwort speichern",
        "err_short": "Das Passwort muss mindestens {n} Zeichen haben.",
        "err_mismatch": "Die Passwörter stimmen nicht überein.",
        "too_large": "Anfrage zu gross.",
        "invalid_title": "Link ungültig",
        "invalid": "Dieser Link ist ungültig, abgelaufen oder wurde bereits verwendet. "
                   "Bitte beim Administrator einen neuen anfordern.",
        "done_title": "Erledigt",
        "done": "Dein Passwort wurde gespeichert.",
        "done_once": "Dieser Link ist jetzt ungültig. Bewahre das Passwort im Passwortmanager auf.",
        "mac_title": "Konto auf dem Mac einrichten",
        "mac_1": "Systemeinstellungen → Internetaccounts → Account hinzufügen → Anderen "
                 "Account hinzufügen → <b>CardDAV-Account</b>",
        "mac_2": "Accounttyp: <b>Manuell</b> (nicht «Erweitert»)",
        "mac_3": "Benutzername, das soeben gesetzte Passwort und als Serveradresse eintragen:",
        "mac_4": "Falls macOS die Identität des Servers nicht überprüfen kann: «Zertifikat "
                 "einblenden» → «Immer vertrauen». Das Adressbuch «M365» erscheint kurz "
                 "danach in Kontakte.",
        "mail": "Hallo\n\nDeine Microsoft-365-Kontakte können ab sofort in der Kontakte-App "
                "auf dem Mac verwendet werden.\nBitte öffne einmalig diesen Link und wähle ein "
                "Passwort (NICHT dein Microsoft-365-Passwort):\n\n{url}\n\nDer Link ist "
                "einmal verwendbar und gültig bis {expires}. Die Seite zeigt danach, wie du "
                "das Konto auf dem Mac einrichtest.\n",
    },
}


def t(key: str) -> str:
    lang = os.environ.get("LANGUAGE", "en").strip().lower()[:2]
    return TEXTS.get(lang, TEXTS["en"])[key]
