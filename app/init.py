#!/usr/bin/env python3
"""Initialisation on every container start (idempotent)."""
import datetime as dt
import ipaddress
import json
import os
import re
import secrets
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from common import (COLL_ROOT, COLLECTION, CONF, KEY_FILE, CERT_FILE, TLS_CERT, TLS_CHAIN,
                    TLS_FULLCHAIN, TLS_KEY,
                    RADICALE_CONF, RIGHTS, STATE_DIR, STORAGE, SYNC_SECRET, SYNC_USER, HTPASSWD,
                    check_name, ensure_addressbook, load_users, read_htpasswd, save_users,
                    set_htpasswd, state_file, write_private)


def log(msg):
    print(f"[init] {msg}", flush=True)


def generate_self_signed(host):
    names = [host, host.split(".")[0], "localhost"]
    san = [x509.DNSName(n) for n in dict.fromkeys(names)]
    san.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=825))  # macOS maximum for TLS server certificates
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True,
                                         content_commitment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    write_private(TLS_KEY, key.private_bytes(serialization.Encoding.PEM,
                                             serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()).decode())
    TLS_CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log(f"Self-signed TLS certificate for {host} created (valid 825 days)")


def ensure_tls():
    """Uses tls-cert.pem + tls-key.pem (+ optional tls-chain.pem) from data/config.
    Only if neither cert nor key exists, a self-signed certificate is generated.
    Writes tls-fullchain.pem (leaf + intermediates) which Radicale and the enrollment
    endpoint serve. Problems that would break clients stop the container."""
    host = os.environ.get("TLS_HOSTNAME", "").strip()
    if not TLS_CERT.exists() and not TLS_KEY.exists():
        if not host:
            log("ERROR: no tls-cert.pem/tls-key.pem in config/ and TLS_HOSTNAME not set "
                "(e.g. nas.example.lan) - cannot create a self-signed certificate")
            sys.exit(1)
        generate_self_signed(host)
    elif not (TLS_CERT.exists() and TLS_KEY.exists()):
        log(f"ERROR: {TLS_CERT.name} and {TLS_KEY.name} must both exist (only one found)")
        sys.exit(1)

    try:
        certs = x509.load_pem_x509_certificates(TLS_CERT.read_bytes())
        if TLS_CHAIN.exists():
            certs += x509.load_pem_x509_certificates(TLS_CHAIN.read_bytes())
        key = serialization.load_pem_private_key(TLS_KEY.read_bytes(), password=None)
    except (ValueError, TypeError) as e:
        log(f"ERROR: cannot read TLS certificate/key (PEM, unencrypted key expected): {e}")
        sys.exit(1)

    # leaf first; drop duplicates (tls-cert.pem may already contain the chain)
    seen, chain = set(), []
    for c in certs:
        fp = c.fingerprint(hashes.SHA256())
        if fp not in seen:
            seen.add(fp)
            chain.append(c)
    leaf = chain[0]

    pub = lambda k: k.public_bytes(serialization.Encoding.DER,  # noqa: E731
                                   serialization.PublicFormat.SubjectPublicKeyInfo)
    if pub(leaf.public_key()) != pub(key.public_key()):
        log(f"ERROR: {TLS_KEY.name} does not belong to the first certificate in {TLS_CERT.name}")
        sys.exit(1)

    now = dt.datetime.now(dt.timezone.utc)
    days = (leaf.not_valid_after_utc - now).days
    if leaf.not_valid_before_utc > now:
        log("ERROR: TLS certificate is not valid yet")
        sys.exit(1)
    if days < 0:
        log(f"ERROR: TLS certificate expired on {leaf.not_valid_after_utc:%Y-%m-%d}")
        sys.exit(1)
    if days < 30:
        log(f"WARNING: TLS certificate expires in {days} days")
    lifetime = (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days
    if lifetime > 825:
        log(f"WARNING: certificate lifetime is {lifetime} days - macOS rejects TLS server "
            f"certificates valid for more than 825 days")

    try:
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns = names.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        dns = []
        log("WARNING: certificate has no Subject Alternative Name - macOS will reject it")
    if host and dns and not any(d == host or (d.startswith("*.") and host.split(".", 1)[-1] == d[2:])
                                for d in dns):
        log(f"WARNING: TLS_HOSTNAME {host} is not covered by the certificate ({', '.join(dns)})")

    for child, parent in zip(chain, chain[1:]):
        try:
            child.verify_directly_issued_by(parent)
        except Exception:  # noqa: BLE001
            log(f"WARNING: chain order: '{child.subject.rfc4514_string()}' is not issued by "
                f"'{parent.subject.rfc4514_string()}' - put the issuing CA first in {TLS_CHAIN.name}")
    if len(chain) == 1 and leaf.issuer != leaf.subject:
        log(f"Note: no intermediate certificates - fine if clients trust the issuing CA directly, "
            f"otherwise add it as {TLS_CHAIN.name}")

    TLS_FULLCHAIN.write_bytes(b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain))
    kind = "self-signed" if leaf.issuer == leaf.subject else f"issued by {leaf.issuer.rfc4514_string()}"
    log(f"TLS: {leaf.subject.rfc4514_string()}, {kind}, valid until "
        f"{leaf.not_valid_after_utc:%Y-%m-%d}, {len(chain) - 1} intermediate(s)")


def bootstrap_from_env(users: dict):
    """Single-user shortcut / upgrade path from 1.x: MAILBOX + CARDDAV_USER as env vars."""
    user = os.environ.get("CARDDAV_USER", "").strip()
    mailbox = os.environ.get("MAILBOX", "").strip()
    if not (user or mailbox):
        return
    if not (user and mailbox):
        log("ERROR: CARDDAV_USER and MAILBOX must be set together (or both omitted)")
        sys.exit(1)
    err = check_name("user", user)
    if err:
        log(f"ERROR: CARDDAV_USER: {err}")
        sys.exit(1)
    if users.get(user, {}).get("mailbox") != mailbox:
        users[user] = {"mailbox": mailbox}
        save_users(users)
        log(f"User mapping from environment: {user} -> {mailbox}")
    # 1.x kept a single state file
    legacy = STATE_DIR / "state.json"
    if legacy.exists() and not state_file(user).exists():
        legacy.rename(state_file(user))
        log(f"Migrated sync state to {state_file(user).name}")


def main():
    err = check_name("COLLECTION", COLLECTION)
    if err:
        log(f"ERROR: {err}")
        sys.exit(1)
    for d in (CONF, STATE_DIR, COLL_ROOT / SYNC_USER):
        d.mkdir(parents=True, exist_ok=True)

    ensure_tls()

    # Radicale configuration (rewritten on every start)
    RADICALE_CONF.write_text(f"""[server]
hosts = 0.0.0.0:5232
ssl = True
certificate = {TLS_FULLCHAIN}
key = {TLS_KEY}

[auth]
type = htpasswd
htpasswd_filename = {HTPASSWD}
htpasswd_encryption = bcrypt

[rights]
type = from_file
file = {RIGHTS}

[storage]
filesystem_folder = {STORAGE}

[web]
type = none

[logging]
level = info
""")

    # Static rights, valid for any number of users (first matching section wins):
    #  - every user may read only their own principal and address book
    #  - the internal sync user may write every address book, nothing else
    c, s = re.escape(COLLECTION), re.escape(SYNC_USER)
    RIGHTS.write_text(f"""# Generated automatically - changes are overwritten on start
[root]
user: .+
collection:
permissions: R

[sync-principals]
user: {s}
collection: [^/]+
permissions: R

[sync-addressbooks]
user: {s}
collection: [^/]+/{c}
permissions: rw

[own-principal]
user: .+
collection: {{user}}
permissions: R

[own-addressbook]
user: .+
collection: {{user}}/{c}
permissions: r
""")

    # Internal password for the sync user
    if not (SYNC_SECRET.exists() and SYNC_USER in read_htpasswd()):
        pw = secrets.token_urlsafe(32)
        write_private(SYNC_SECRET, pw)
        set_htpasswd(SYNC_USER, pw)
        log("Internal password for sync user created")

    users = load_users()
    bootstrap_from_env(users)
    passwords = read_htpasswd()
    for user in users:
        if ensure_addressbook(user):
            log(f"Address book /{user}/{COLLECTION}/ created")
        if user not in passwords:
            log(f"WARNING: no password set for '{user}' -> "
                f"docker exec -it m365-carddav user passwd {user}")
    if not users:
        log("No users configured yet -> docker exec -it m365-carddav user add <name> <mailbox>")

    if not (KEY_FILE.exists() and CERT_FILE.exists()):
        log("ERROR: no Graph certificate found. Run 'gen-cert' first (see README).")
        sys.exit(1)


if __name__ == "__main__":
    main()
