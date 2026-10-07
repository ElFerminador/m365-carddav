"""Shared paths, configuration and htpasswd helpers."""
import os
import re
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
SYNC_SECRET = CONF / "sync.secret"
KEY_FILE = CONF / "graph-key.pem"
CERT_FILE = CONF / "graph-cert.pem"
CER_FILE = CONF / "graph-cert.cer"
TLS_CERT = CONF / "tls-cert.pem"
TLS_KEY = CONF / "tls-key.pem"

SYNC_USER = "sync"
CARDDAV_USER = os.environ.get("CARDDAV_USER", "").strip()
COLLECTION = os.environ.get("COLLECTION", "m365").strip()

_NAME_RE = re.compile(r"^[a-z0-9._-]+$")


def validate_names():
    for label, value in (("CARDDAV_USER", CARDDAV_USER), ("COLLECTION", COLLECTION)):
        if not value:
            sys.exit(f"{label} is not set")
        if not _NAME_RE.match(value):
            sys.exit(f"{label}='{value}' is invalid (allowed: a-z 0-9 . _ -)")
    if CARDDAV_USER == SYNC_USER:
        sys.exit(f"CARDDAV_USER must not be '{SYNC_USER}'")


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


def read_htpasswd() -> dict:
    users = {}
    if HTPASSWD.exists():
        for line in HTPASSWD.read_text().splitlines():
            if ":" in line:
                user, hashed = line.split(":", 1)
                users[user] = hashed
    return users


def set_htpasswd(user: str, password: str):
    users = read_htpasswd()
    users[user] = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    write_private(HTPASSWD, "".join(f"{u}:{h}\n" for u, h in users.items()))
