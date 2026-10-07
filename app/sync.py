#!/usr/bin/env python3
"""One-way sync: M365 default contacts folder (Graph delta) -> Radicale (CardDAV).

M365 is the source of truth. Primary key is the immutable ID (Prefer: IdType="ImmutableId").
"""
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import signal
import sys
import time
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

import httpx
import msal
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from common import (CERT_FILE, COLLECTION, KEY_FILE, SYNC_SECRET, SYNC_USER, TLS_CERT, TRIGGER,
                    env_bool, env_int, load_users, state_file)

GRAPH = "https://graph.microsoft.com/v1.0"
PREFER = 'IdType="ImmutableId", odata.maxpagesize=200'
FIELDS = [
    "displayName", "givenName", "middleName", "surname", "title", "generation", "nickName",
    "companyName", "department", "jobTitle", "emailAddresses", "businessPhones", "homePhones",
    "mobilePhone", "businessAddress", "homeAddress", "otherAddress", "birthday",
    "personalNotes", "businessHomePage", "categories", "lastModifiedDateTime",
]
log = logging.getLogger("sync")


class DeltaGone(Exception):
    """Delta token invalid/expired -> full resync required."""


def retry_after(resp, attempt):
    try:
        return max(1, int(resp.headers.get("Retry-After", "")))
    except ValueError:
        return min(2 ** attempt, 60)


# --------------------------------------------------------------------------- Graph
class Graph:
    """App-only Graph client (certificate auth), shared by all mailboxes."""

    def __init__(self, tenant, client_id):
        cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
        self.cert_not_after = cert.not_valid_after_utc
        self.app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant}",
            client_credential={
                "private_key": KEY_FILE.read_text(),
                "thumbprint": cert.fingerprint(hashes.SHA1()).hex().upper(),
            },
        )
        self.http = httpx.Client(timeout=60)

    def _token(self):
        res = self.app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in res:
            raise RuntimeError(f"Token error: {res.get('error')}: {res.get('error_description')}")
        return res["access_token"]

    def get(self, url, params=None, allow_404=False):
        for attempt in range(1, 7):
            r = self.http.get(url, params=params, headers={
                "Authorization": f"Bearer {self._token()}", "Prefer": PREFER})
            if r.status_code == 410 or (r.status_code == 400 and "syncstate" in r.text.lower()):
                raise DeltaGone()
            if r.status_code == 404 and allow_404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                wait = retry_after(r, attempt)
                log.warning("Graph HTTP %s, waiting %ss (attempt %s)", r.status_code, wait, attempt)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError("Graph: too many failed attempts")

    def check_access(self, mailbox):
        """Returns (ok, message) - used by the CLI when a user is added."""
        r = self.http.get(f"{GRAPH}/users/{quote(mailbox)}/contacts",
                          params={"$top": "1", "$select": "id"},
                          headers={"Authorization": f"Bearer {self._token()}"})
        if r.status_code == 200:
            return True, "Graph access to the contacts of this mailbox works"
        if r.status_code == 403:
            return False, ("HTTP 403: the mailbox is not (yet) in the app's RBAC scope. Add it "
                           "to the scope group / run 2-Grant-MailboxAccess.ps1; propagation can "
                           "take up to 2 hours. The sync retries automatically.")
        if r.status_code == 404:
            return False, "HTTP 404: mailbox not found - check the address"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"


class Mailbox:
    """Graph operations for one mailbox."""

    def __init__(self, graph: Graph, mailbox: str):
        self.get = graph.get
        self.base = f"{GRAPH}/users/{quote(mailbox)}"

    def default_folder_id(self):
        r = self.get(f"{self.base}/contacts", params={"$top": "1", "$select": "parentFolderId"})
        items = r.json().get("value", [])
        if not items:
            raise RuntimeError("No contacts in the default folder - cannot determine its folder ID")
        return items[0]["parentFolderId"]

    def delta_pages(self, url, params=None):
        while True:
            data = self.get(url, params=params).json()
            params = None
            yield data.get("value", []), data.get("@odata.deltaLink")
            if "@odata.nextLink" not in data:
                return
            url = data["@odata.nextLink"]

    def photo(self, cid):
        r = self.get(f"{self.base}/contacts/{quote(cid, safe='')}/photo/$value", allow_404=True)
        if r is None or not r.content:
            return None
        ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip().lower()
        return ("PNG" if "png" in ctype else "JPEG"), r.content


# --------------------------------------------------------------------------- vCard
def esc(value) -> str:
    return (str(value or "").replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
            .replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;"))


def fold(line: str) -> str:
    """RFC-compliant folding at 75 octets without splitting UTF-8 characters."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > 75:
            out.append(cur)
            cur, size = " " + ch, 1 + n
        else:
            cur, size = cur + ch, size + n
    out.append(cur)
    return "\r\n".join(out)


def adr(a):
    a = a or {}
    if not any(a.get(k) for k in ("street", "city", "state", "postalCode", "countryOrRegion")):
        return None
    return ";".join(["", "", esc(a.get("street")), esc(a.get("city")), esc(a.get("state")),
                     esc(a.get("postalCode")), esc(a.get("countryOrRegion"))])


def build_vcard(c: dict, photo=None) -> str:
    given, surname, company = c.get("givenName"), c.get("surname"), c.get("companyName")
    is_company = not given and not surname and bool(company)
    emails = [e.get("address") for e in c.get("emailAddresses") or [] if e.get("address")]
    fn = (c.get("displayName") or " ".join(x for x in (given, surname) if x)
          or company or (emails[0] if emails else "Unnamed"))

    L = ["BEGIN:VCARD", "VERSION:3.0", "PRODID:-//m365-carddav//EN",
         f"UID:{esc(c['id'])}", f"FN:{esc(fn)}",
         "N:" + ";".join(esc(c.get(k)) for k in ("surname", "givenName", "middleName",
                                                  "title", "generation"))]
    if c.get("nickName"):
        L.append(f"NICKNAME:{esc(c['nickName'])}")
    if company or c.get("department"):
        L.append(f"ORG:{esc(company)};{esc(c.get('department'))}")
    if is_company:
        L.append("X-ABShowAs:COMPANY")
    if c.get("jobTitle"):
        L.append(f"TITLE:{esc(c['jobTitle'])}")
    for i, e in enumerate(emails):
        L.append(f"EMAIL;TYPE=INTERNET{',PREF' if i == 0 else ''}:{esc(e)}")
    for p in c.get("businessPhones") or []:
        L.append(f"TEL;TYPE=WORK,VOICE:{esc(p)}")
    for p in c.get("homePhones") or []:
        L.append(f"TEL;TYPE=HOME,VOICE:{esc(p)}")
    if c.get("mobilePhone"):
        L.append(f"TEL;TYPE=CELL,VOICE:{esc(c['mobilePhone'])}")
    for key, typ in (("businessAddress", "WORK"), ("homeAddress", "HOME"), ("otherAddress", "OTHER")):
        value = adr(c.get(key))
        if value:
            L.append(f"ADR;TYPE={typ}:{value}")
    if c.get("birthday"):
        L.append(f"BDAY:{c['birthday'][:10]}")
    if c.get("businessHomePage"):
        L.append(f"URL:{esc(c['businessHomePage'])}")
    if c.get("categories"):
        L.append("CATEGORIES:" + ",".join(esc(x) for x in c["categories"]))
    if c.get("personalNotes"):
        L.append(f"NOTE:{esc(c['personalNotes'])}")
    if photo:
        typ, raw = photo
        L.append(f"PHOTO;ENCODING=b;TYPE={typ}:{base64.b64encode(raw).decode()}")
    if c.get("lastModifiedDateTime"):
        L.append(f"REV:{c['lastModifiedDateTime']}")
    L.append("END:VCARD")
    return "\r\n".join(fold(x) for x in L) + "\r\n"


def resource_name(cid: str) -> str:
    return hashlib.sha256(cid.encode()).hexdigest() + ".vcf"


# --------------------------------------------------------------------------- CardDAV
class Dav:
    ROOT = "https://127.0.0.1:5232/"

    def __init__(self, user):
        self.base = f"{self.ROOT}{user}/{COLLECTION}/"
        # Loopback inside the same container: certificate is not verified
        self.http = httpx.Client(auth=(SYNC_USER, SYNC_SECRET.read_text().strip()), timeout=30,
                                 verify=False)

    def wait_ready(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.http.request("PROPFIND", self.ROOT, headers={"Depth": "0"}).status_code == 207:
                    return
            except httpx.TransportError:
                pass
            time.sleep(1)
        raise RuntimeError("Radicale not reachable")

    def put(self, name, card):
        r = self.http.put(self.base + name, content=card.encode(),
                          headers={"Content-Type": "text/vcard; charset=utf-8"})
        r.raise_for_status()

    def delete(self, name):
        r = self.http.delete(self.base + name)
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def list(self) -> set:
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getetag/>'
                '</d:prop></d:propfind>')
        r = self.http.request("PROPFIND", self.base, content=body,
                              headers={"Depth": "1", "Content-Type": "application/xml"})
        r.raise_for_status()
        names = set()
        for href in ET.fromstring(r.content).iter("{DAV:}href"):
            name = unquote(href.text or "").rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".vcf"):
                names.add(name)
        return names


# --------------------------------------------------------------------------- State
def load_state(user) -> dict:
    try:
        return json.loads(state_file(user).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(user, state: dict):
    path = state_file(user)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


# --------------------------------------------------------------------------- Sync
def sync_once(user, state, graph, dav, mailbox, full, photos):
    if state.get("mailbox") != mailbox:
        state.clear()
        state.update(mailbox=mailbox, contacts={})
    contacts = state.setdefault("contacts", {})
    if not state.get("folder_id"):
        state["folder_id"] = graph.default_folder_id()

    if full or not state.get("delta_link"):
        full = True
        url = f"{graph.base}/contactFolders/{quote(state['folder_id'], safe='')}/contacts/delta"
        params = {"$select": ",".join(FIELDS)}
    else:
        url, params = state["delta_link"], None

    existing = dav.list() if full else set()
    seen_ids, written, deleted, unchanged = set(), 0, 0, 0
    delta_link = None

    for items, link in graph.delta_pages(url, params):
        delta_link = link or delta_link
        for c in items:
            cid = c["id"]
            name = resource_name(cid)
            if "@removed" in c:
                dav.delete(name)
                contacts.pop(cid, None)
                deleted += 1
                continue
            seen_ids.add(cid)
            prev = contacts.get(cid)
            mod = c.get("lastModifiedDateTime")
            if full and prev and prev.get("mod") == mod and name in existing:
                unchanged += 1
                continue
            card = build_vcard(c, graph.photo(cid) if photos else None)
            digest = hashlib.sha256(card.encode()).hexdigest()
            if not prev or prev.get("hash") != digest or (full and name not in existing):
                dav.put(name, card)
                written += 1
            else:
                unchanged += 1
            contacts[cid] = {"mod": mod, "hash": digest}

    if full:
        expected = {resource_name(cid) for cid in seen_ids}
        for orphan in existing - expected:
            dav.delete(orphan)
            deleted += 1
        for cid in list(contacts):
            if cid not in seen_ids:
                del contacts[cid]
        state["last_full"] = dt.date.today().isoformat()

    if not delta_link:
        raise RuntimeError("Graph returned no deltaLink")
    state["delta_link"] = delta_link
    result = (f"{'Full' if full else 'Delta'} sync: {written} written, {deleted} deleted, "
              f"{unchanged} unchanged, {len(contacts)} contacts total")
    state.update(last_sync=dt.datetime.now().isoformat(timespec="seconds"), last_result=result)
    state.pop("last_error", None)
    save_state(user, state)
    log.info("[%s] %s", user, result)


def sync_user(user, mailbox, graph, photos, full_hour):
    state = load_state(user)
    dav = Dav(user)
    mbx = Mailbox(graph, mailbox)
    for _ in range(2):  # second round only after an expired delta token
        now = dt.datetime.now()
        full = not state.get("delta_link") or (
            now.hour >= full_hour and state.get("last_full") != now.date().isoformat())
        try:
            sync_once(user, state, mbx, dav, mailbox, full, photos)
            return
        except DeltaGone:
            log.warning("[%s] Delta token invalid - full resync", user)
            state.pop("delta_link", None)
            continue
        except httpx.HTTPStatusError as e:
            err = f"HTTP {e.response.status_code} at {e.request.url.path}: {e.response.text[:300]}"
        except Exception as e:  # noqa: BLE001 - one user must never stop the others
            log.exception("[%s] Sync failed", user)
            err = f"{type(e).__name__}: {e}"
        log.error("[%s] %s", user, err)
        state.update(last_error=err, last_error_at=dt.datetime.now().isoformat(timespec="seconds"))
        save_state(user, state)
        return


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s [sync] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = {k: os.environ.get(k, "").strip() for k in ("TENANT_ID", "CLIENT_ID")}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        log.error("Missing environment variables: %s", ", ".join(missing))
        sys.exit(2)
    interval = env_int("SYNC_INTERVAL", 900)
    full_hour = env_int("FULL_SYNC_HOUR", 3)
    photos = env_bool("SYNC_PHOTOS", True)

    stop = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.append(True))

    graph = Graph(cfg["TENANT_ID"], cfg["CLIENT_ID"])
    Dav("-").wait_ready()
    log.info("Start: interval %ss, full sync from %02d:00, photos %s",
             interval, full_hour, "yes" if photos else "no")

    while not stop:
        TRIGGER.unlink(missing_ok=True)
        days_left = (graph.cert_not_after - dt.datetime.now(dt.timezone.utc)).days
        if days_left < 30:
            log.warning("Graph certificate expires in %d days - renew it (see README)", days_left)
        try:
            tls_days = (x509.load_pem_x509_certificate(TLS_CERT.read_bytes()).not_valid_after_utc
                        - dt.datetime.now(dt.timezone.utc)).days
            if tls_days < 30:
                log.warning("TLS certificate expires in %d days - replace tls-cert.pem and "
                            "restart the container", tls_days)
        except (OSError, ValueError):
            pass
        users = load_users()  # re-read every round: users added/removed via CLI need no restart
        if not users:
            log.info("No users configured (docker exec -it m365-carddav user add <name> <mailbox>)")
        for user, cfg_user in users.items():
            if stop:
                break
            sync_user(user, cfg_user["mailbox"], graph, photos, full_hour)
        for _ in range(interval):
            if stop or TRIGGER.exists():
                break
            time.sleep(1)
    log.info("Stopped")


if __name__ == "__main__":
    main()
