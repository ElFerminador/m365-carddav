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

from common import (CARDDAV_USER, COLL_ROOT, COLLECTION, CONF, KEY_FILE, CERT_FILE, TLS_CERT, TLS_KEY,
                    RADICALE_CONF, RIGHTS, STATE_DIR, STORAGE, SYNC_SECRET, SYNC_USER, HTPASSWD,
                    read_htpasswd, set_htpasswd, validate_names, write_private)


def log(msg):
    print(f"[init] {msg}", flush=True)


def ensure_tls():
    """Create a self-signed server certificate if none exists.
    Own certificate: put tls-cert.pem (incl. chain) + tls-key.pem into data/config."""
    if TLS_CERT.exists() and TLS_KEY.exists():
        return
    host = os.environ.get("TLS_HOSTNAME", "").strip()
    if not host:
        log("ERROR: TLS_HOSTNAME is not set (e.g. nas.example.lan)")
        sys.exit(1)
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


def main():
    validate_names()
    for d in (CONF, STATE_DIR, COLL_ROOT / SYNC_USER):
        d.mkdir(parents=True, exist_ok=True)

    ensure_tls()

    # Radicale configuration (rewritten on every start)
    RADICALE_CONF.write_text(f"""[server]
hosts = 0.0.0.0:5232
ssl = True
certificate = {TLS_CERT}
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

    # Rights: CARDDAV_USER is read-only, sync may only write to the address book
    u, c, s = re.escape(CARDDAV_USER), re.escape(COLLECTION), re.escape(SYNC_USER)
    RIGHTS.write_text(f"""# Generated automatically - changes are overwritten on start
[root]
user: .+
collection:
permissions: R

[reader-principal]
user: {u}
collection: {u}
permissions: R

[reader-addressbook]
user: {u}
collection: {u}/{c}
permissions: r

[sync-own-principal]
user: {s}
collection: {s}
permissions: R

[sync-reader-principal]
user: {s}
collection: {u}
permissions: R

[sync-addressbook]
user: {s}
collection: {u}/{c}
permissions: rw
""")

    # Address book lives in the reader's principal so that clients find it via discovery
    book = COLL_ROOT / CARDDAV_USER / COLLECTION
    book.mkdir(parents=True, exist_ok=True)
    props = book / ".Radicale.props"
    if not props.exists():
        props.write_text(json.dumps({
            "tag": "VADDRESSBOOK",
            "D:displayname": "M365",
            "CR:addressbook-description": "Read-only mirror of the M365 contacts",
        }))
        log(f"Address book /{CARDDAV_USER}/{COLLECTION}/ created")

    # Internal password for the sync user
    if not (SYNC_SECRET.exists() and SYNC_USER in read_htpasswd()):
        pw = secrets.token_urlsafe(32)
        write_private(SYNC_SECRET, pw)
        set_htpasswd(SYNC_USER, pw)
        log("Internal password for sync user created")

    if CARDDAV_USER not in read_htpasswd():
        log(f"WARNING: no password set for '{CARDDAV_USER}' yet -> "
            f"docker exec -it m365-carddav set-password")

    if not (KEY_FILE.exists() and CERT_FILE.exists()):
        log("ERROR: no Graph certificate found. Run 'gen-cert' first (see README).")
        sys.exit(1)


if __name__ == "__main__":
    main()
