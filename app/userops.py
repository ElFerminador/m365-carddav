"""User operations shared by the CLI (`user`) and the admin web UI."""
import datetime as dt
import json
import os
import re
import shutil

from common import (CERT_FILE, COLL_ROOT, KEY_FILE, check_name, create_invite, del_htpasswd,
                    delete_invites, enrollment_url, ensure_addressbook, load_users,
                    pending_invite, read_htpasswd, save_users, state_file, t, trigger_sync)


MAILBOX_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def local(ts: str) -> str:
    return dt.datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M")


def validate_new_user(name: str, mailbox: str):
    """Returns (name, mailbox) normalised, raises ValueError with a readable message."""
    name, mailbox = name.strip().lower(), mailbox.strip()
    err = check_name("user", name)
    if err:
        raise ValueError(err)
    if len(mailbox) > 254 or not MAILBOX_RE.match(mailbox):
        raise ValueError("Mailbox must be an e-mail address.")
    if name in load_users():
        raise ValueError(f"User '{name}' already exists.")
    return name, mailbox


def add_user(name: str, mailbox: str):
    name, mailbox = validate_new_user(name, mailbox)
    users = load_users()
    users[name] = {"mailbox": mailbox}
    save_users(users)
    ensure_addressbook(name)
    trigger_sync()
    return name, mailbox


def delete_user(name: str):
    users = load_users()
    if name not in users:
        raise ValueError(f"Unknown user '{name}'.")
    del users[name]
    save_users(users)
    del_htpasswd(name)
    delete_invites(name)
    shutil.rmtree(COLL_ROOT / name, ignore_errors=True)
    state_file(name).unlink(missing_ok=True)


def resync_user(name: str):
    if name not in load_users():
        raise ValueError(f"Unknown user '{name}'.")
    state_file(name).unlink(missing_ok=True)
    trigger_sync()


def new_invite(name: str, hours: int):
    """Returns dict with url, expires (local string) and the ready-to-send text."""
    if name not in load_users():
        raise ValueError(f"Unknown user '{name}'.")
    token, expires = create_invite(name, hours)
    exp = local(expires.isoformat())
    url = enrollment_url(token)
    return {"url": url, "expires": exp, "text": t("mail").format(url=url, expires=exp)}


def check_access(mailbox: str):
    """(ok, message); ok is None if the check could not run."""
    tenant, client = os.environ.get("TENANT_ID", ""), os.environ.get("CLIENT_ID", "")
    if not (tenant and client and KEY_FILE.exists() and CERT_FILE.exists()):
        return None, "Access check skipped (TENANT_ID/CLIENT_ID or Graph certificate missing)."
    from sync import Graph  # lazy: network libs + certificate
    try:
        return Graph(tenant, client).check_access(mailbox)
    except Exception as e:  # noqa: BLE001
        return None, f"Access check failed: {e}"


def user_rows():
    users, pw = load_users(), read_htpasswd()
    rows = []
    for name in sorted(users):
        try:
            st = json.loads(state_file(name).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            st = {}
        error = bool(st.get("last_error") and st.get("last_error_at", "") >= st.get("last_sync", ""))
        inv = pending_invite(name)
        rows.append({
            "name": name,
            "mailbox": users[name]["mailbox"],
            "password": name in pw,
            "invite_until": local(inv) if inv else "",
            "last_sync": (st.get("last_sync") or "").replace("T", " "),
            "status": st["last_error"] if error else st.get("last_result", "never synced"),
            "error": error,
        })
    return rows
