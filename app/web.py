#!/usr/bin/env python3
"""HTTPS endpoint on port 5233 with two independent parts:

  /invite/<token>   one-time password links for users       (ENROLLMENT=true or ADMIN_UI=true)
  /admin            user administration, password-protected  (ADMIN_UI=true)

Everything else answers 404. No JavaScript, no external resources.
"""
import hashlib
import html
import http.server
import logging
import re
import secrets
import ssl
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from common import (ADMIN_INITIAL, COLLECTION, TLS_FULLCHAIN, TLS_KEY, admin_fingerprint,
                    carddav_url, consume_invite, env_bool, env_int, find_invite, load_users,
                    set_htpasswd, t, trigger_sync, verify_admin_password)
import userops

log = logging.getLogger("web")
PORT = 5233
MIN_LEN = 12
INVITE_RE = re.compile(r"^/(?:invite|enroll)/([A-Za-z0-9_-]{20,100})$")
COOKIE = "__Host-m365carddav"
IDLE_TIMEOUT = 30 * 60
ABSOLUTE_TIMEOUT = 12 * 3600
ADMIN_UI = env_bool("ADMIN_UI", False)
INVITES = ADMIN_UI or env_bool("ENROLLMENT", False)
HOURS = env_int("ENROLLMENT_HOURS", 72)
_invite_lock = threading.Lock()

HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    "Strict-Transport-Security": "max-age=31536000",
}

PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{title}</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f6f4;--card:#fff;--fg:#1d1d1f;--mut:#6e6e73;--ac:#0a66c2;--err:#b3261e;--ok:#1e7a3c;--bd:#d2d2d7}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111;--card:#1c1c1e;--fg:#f2f2f7;--mut:#a1a1a6;--ac:#4ea1ff;--err:#ff6b60;--ok:#4cd07d;--bd:#3a3a3c}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:{width};margin:2rem auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:1.5rem;margin-bottom:1rem}}
h1{{font-size:1.35rem;margin:0 0 .75rem}} h2{{font-size:1.05rem;margin:0 0 .75rem}} p{{margin:.5rem 0}}
.mut{{color:var(--mut);font-size:.9rem}}
label{{display:block;margin:1rem 0 .25rem;font-weight:600}}
input,textarea{{width:100%;box-sizing:border-box;padding:.6rem .7rem;font:inherit;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg)}}
textarea{{font:14px/1.4 ui-monospace,Menlo,monospace;min-height:5rem}}
button{{padding:.6rem 1rem;font:inherit;font-weight:600;border:0;border-radius:8px;background:var(--ac);color:#fff;cursor:pointer}}
button.wide{{margin-top:1.25rem;width:100%}} button.sec{{background:transparent;color:var(--ac);border:1px solid var(--bd)}}
button.small{{padding:.25rem .6rem;font-size:.85rem;font-weight:500}} button.danger{{background:var(--err)}}
.err{{color:var(--err);font-weight:600}} .ok{{color:var(--ok);font-weight:600}}
code{{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:.1rem .35rem;word-break:break-all}}
ol{{padding-left:1.2rem}} li{{margin:.35rem 0}}
table{{width:100%;border-collapse:collapse;font-size:.92rem}} th,td{{text-align:left;padding:.5rem .4rem;border-bottom:1px solid var(--bd);vertical-align:top}}
th{{color:var(--mut);font-weight:600}} td.act{{white-space:nowrap}} td.act form{{display:inline}}
.row{{display:flex;gap:.75rem;flex-wrap:wrap;align-items:flex-end}} .row>div{{flex:1;min-width:12rem}}
.top{{display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap}} .top form{{display:inline}}
.scroll{{overflow-x:auto}}
</style></head><body><main>{body}</main></body></html>"""

e = html.escape


# --------------------------------------------------------------------------- admin sessions
class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def create(self):
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.data[sid] = {"created": now, "seen": now, "csrf": secrets.token_urlsafe(32),
                              "fp": admin_fingerprint(), "flash": ""}
        return sid

    def get(self, sid):
        if not sid:
            return None
        now = time.time()
        with self.lock:
            s = self.data.get(sid)
            if not s:
                return None
            if (now - s["seen"] > IDLE_TIMEOUT or now - s["created"] > ABSOLUTE_TIMEOUT
                    or s["fp"] != admin_fingerprint()):
                del self.data[sid]
                return None
            s["seen"] = now
            return s

    def drop(self, sid):
        with self.lock:
            self.data.pop(sid, None)


class Throttle:
    """Global login throttle (all clients may share one source IP behind Docker NAT)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fails = 0
        self.until = 0.0

    def wait(self):
        with self.lock:
            return max(0, int(self.until - time.time()))

    def failed(self):
        with self.lock:
            self.fails += 1
            if self.fails >= 5:
                self.until = time.time() + min(900, 60 * 2 ** (self.fails - 5))

    def ok(self):
        with self.lock:
            self.fails, self.until = 0, 0.0


SESSIONS, THROTTLE = Sessions(), Throttle()


# --------------------------------------------------------------------------- invite pages
def instructions(user):
    return (f"<h2 style='margin-top:1.5rem'>{t('mac_title')}</h2><ol>"
            f"<li>{t('mac_1')}</li><li>{t('mac_2')}</li>"
            f"<li>{t('mac_3')}<br>{t('user_name')}: <code>{e(user)}</code><br>"
            f"{t('server')}: <code>{e(carddav_url(user))}</code></li>"
            f"<li>{t('mac_4')}</li></ol>")


def invite_form(user, mailbox, error=""):
    err = f"<p class='err'>{e(error)}</p>" if error else ""
    return (f"<div class='card'><h1>{t('title')}</h1><p>{t('intro')}</p>"
            f"<p class='mut'>{t('user_name')}: <code>{e(user)}</code><br>"
            f"{t('mailbox')}: {e(mailbox)}</p>{err}"
            f"<form method='post' autocomplete='off'>"
            f"<input type='text' name='username' value='{e(user)}' autocomplete='username' hidden>"
            f"<label for='p1'>{t('new_pw')}</label>"
            f"<input id='p1' name='p1' type='password' minlength='{MIN_LEN}' required autocomplete='new-password'>"
            f"<label for='p2'>{t('repeat_pw')}</label>"
            f"<input id='p2' name='p2' type='password' minlength='{MIN_LEN}' required autocomplete='new-password'>"
            f"<p class='mut'>{t('pw_hint').format(n=MIN_LEN)}</p>"
            f"<button class='wide' type='submit'>{t('save')}</button></form></div>")


# --------------------------------------------------------------------------- admin pages
def csrf_field(s):
    return f"<input type='hidden' name='csrf' value='{e(s['csrf'])}'>"


def login_page(error=""):
    err = f"<p class='err'>{e(error)}</p>" if error else ""
    first = ("<p class='mut'>First login: <code>docker exec m365-carddav cat "
             f"{e(str(ADMIN_INITIAL))}</code></p>" if ADMIN_INITIAL.exists() else "")
    return (f"<div class='card'><h1>m365-carddav · Admin</h1>{err}"
            f"<form method='post' action='/admin/login'>"
            f"<label for='u'>User name</label>"
            f"<input id='u' name='username' value='admin' readonly autocomplete='username'>"
            f"<label for='p'>Password</label>"
            f"<input id='p' name='password' type='password' required autofocus autocomplete='current-password'>"
            f"<button class='wide' type='submit'>Sign in</button></form>{first}"
            f"<p class='mut'>Forgot it? <code>docker exec -it m365-carddav resetadminpw</code></p></div>")


def dashboard(s):
    flash, s["flash"] = s.get("flash", ""), ""
    rows = userops.user_rows()
    c = csrf_field(s)
    trs = ""
    for r in rows:
        pw = ("<span class='ok'>set</span>" if r["password"] else "<span class='err'>missing</span>")
        if r["invite_until"]:
            pw += f"<br><span class='mut'>invitation until {e(r['invite_until'])}</span>"
        status = f"<span class='err'>{e(r['status'][:160])}</span>" if r["error"] else e(r["status"])
        n = e(r["name"])
        trs += (f"<tr><td><b>{n}</b></td><td>{e(r['mailbox'])}</td><td>{pw}</td>"
                f"<td>{e(r['last_sync'] or '–')}</td><td>{status}</td><td class='act'>"
                f"<form method='post' action='/admin/invite'>{c}<input type='hidden' name='name' value='{n}'>"
                f"<button class='small sec' title='Issue a new one-time password link'>Invite</button></form> "
                f"<form method='post' action='/admin/resync'>{c}<input type='hidden' name='name' value='{n}'>"
                f"<button class='small sec' title='Discard sync state and resync fully'>Resync</button></form> "
                f"<form method='post' action='/admin/delete-confirm'>{c}<input type='hidden' name='name' value='{n}'>"
                f"<button class='small danger'>Delete</button></form></td></tr>")
    if not trs:
        trs = "<tr><td colspan='6' class='mut'>No users yet.</td></tr>"
    msg = f"<p class='ok'>{e(flash)}</p>" if flash else ""
    return (f"<div class='top'><h1 style='margin:0'>m365-carddav · Users</h1><div>"
            f"<form method='post' action='/admin/sync'>{c}<button class='sec'>Sync now</button></form> "
            f"<form method='post' action='/admin/logout'>{c}<button class='sec'>Sign out</button></form>"
            f"</div></div>{msg}"
            f"<div class='card' style='margin-top:1rem'><div class='scroll'><table><tr><th>User</th><th>Mailbox</th>"
            f"<th>Password</th><th>Last sync</th><th>Status</th><th></th></tr>{trs}</table></div>"
            f"<p class='mut'>Status refreshes on reload. Address book path: /&lt;user&gt;/{e(COLLECTION)}/</p></div>"
            f"<div class='card'><h2>Add user</h2><form method='post' action='/admin/add'>{c}<div class='row'>"
            f"<div><label for='n'>CardDAV user name</label>"
            f"<input id='n' name='name' required pattern='[a-z0-9._-]+' placeholder='alice' autocomplete='off'></div>"
            f"<div><label for='m'>M365 mailbox</label>"
            f"<input id='m' name='mailbox' type='email' required placeholder='alice@contoso.com' autocomplete='off'></div>"
            f"<div style='flex:0'><button>Add</button></div></div></form>"
            f"<p class='mut'>The mailbox must be in the app's RBAC scope (scope group). "
            f"The user then receives a one-time link to choose their own password.</p></div>")


def invite_result(name, inv, check=None):
    chk = ""
    if check:
        ok, msg = check
        cls = {True: "ok", False: "err", None: "mut"}[ok]
        chk = f"<p class='{cls}'>{e(msg)}</p>"
    return (f"<div class='card'><h1>Invitation for {e(name)}</h1>{chk}"
            f"<p>One-time link, valid until {e(inv['expires'])}. <b>Shown only now</b> – "
            f"send it directly to the user (e-mail to their mailbox, Teams chat).</p>"
            f"<label>Link</label><textarea readonly rows='2'>{e(inv['url'])}</textarea>"
            f"<label>Text to send</label><textarea readonly rows='9'>{e(inv['text'])}</textarea>"
            f"<p style='margin-top:1.25rem'><a href='/admin'>← Back to users</a></p></div>")


def delete_confirm(s, name, mailbox):
    return (f"<div class='card'><h1>Delete {e(name)}?</h1>"
            f"<p>Removes the CardDAV login <b>{e(name)}</b>, its password and the address book "
            f"mirroring <b>{e(mailbox)}</b>. Macs using this account stop syncing.</p>"
            f"<p class='mut'>Exchange access is not changed – remove the mailbox from the scope group "
            f"as well if the app should lose access to it.</p>"
            f"<form method='post' action='/admin/delete'>{csrf_field(s)}"
            f"<input type='hidden' name='name' value='{e(name)}'>"
            f"<button class='danger'>Delete</button> <a href='/admin' style='margin-left:1rem'>Cancel</a></form></div>")


# --------------------------------------------------------------------------- handler
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "m365-carddav"
    sys_version = ""
    timeout = 20

    def log_message(self, fmt, *args):  # paths may contain tokens - never log them
        path = urlsplit(self.path).path
        shown = "/invite/…" if INVITE_RE.match(path) else path[:40]
        log.info("%s %s %s -> %s", self.client_address[0], self.command, shown,
                 args[1] if len(args) > 1 else "")

    # -- output
    def send(self, code, body, width="34rem", lang=None, extra=None):
        data = PAGE.format(lang=lang or t("lang"), title="m365-carddav", width=width,
                           body=body).encode()
        self.send_response(code)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or []):
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location, extra=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def not_found(self):
        self.send(404, "<div class='card'><h1>Not found</h1></div>", lang="en")

    def admin(self, code, body):
        self.send(code, body, width="64rem", lang="en")

    # -- input
    def form(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8192:
            return None
        return {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}

    def same_origin(self):
        origin = self.headers.get("Origin")
        return origin is None or origin == f"https://{self.headers.get('Host', '')}"

    def session(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v, SESSIONS.get(v)
        return None, None

    # -- routing
    def do_GET(self):
        path = urlsplit(self.path).path
        if INVITES and INVITE_RE.match(path):
            return self.invite_get(path)
        if ADMIN_UI and path in ("/admin", "/admin/"):
            _, s = self.session()
            return self.admin(200, dashboard(s) if s else login_page())
        return self.not_found()

    def do_POST(self):
        path = urlsplit(self.path).path
        if INVITES and INVITE_RE.match(path):
            return self.invite_post(path)
        if ADMIN_UI and path.startswith("/admin/"):
            return self.admin_post(path)
        return self.not_found()

    def do_HEAD(self):
        self.send_response(405)
        self.end_headers()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_PROPFIND = do_HEAD

    # -- invitations
    def invite_lookup(self, path):
        token_hash = hashlib.sha256(INVITE_RE.match(path).group(1).encode()).hexdigest()
        invite = find_invite(token_hash)
        mailbox = load_users().get(invite["user"], {}).get("mailbox") if invite else None
        if not mailbox:
            time.sleep(1)  # slows down guessing (tokens have 256 bit anyway)
            self.send(404, f"<div class='card'><h1>{t('invalid_title')}</h1><p>{t('invalid')}</p></div>")
            return None, None, None
        return token_hash, invite["user"], mailbox

    def invite_get(self, path):
        _, user, mailbox = self.invite_lookup(path)
        if user:
            self.send(200, invite_form(user, mailbox))

    def invite_post(self, path):
        token_hash, user, mailbox = self.invite_lookup(path)
        if not user:
            return
        f = self.form()
        if f is None or not self.same_origin():
            return self.send(400, f"<div class='card'><p class='err'>{t('too_large')}</p></div>")
        p1, p2 = f.get("p1", ""), f.get("p2", "")
        if len(p1) < MIN_LEN:
            return self.send(400, invite_form(user, mailbox, t("err_short").format(n=MIN_LEN)))
        if p1 != p2:
            return self.send(400, invite_form(user, mailbox, t("err_mismatch")))
        with _invite_lock:
            if not consume_invite(token_hash):
                return self.send(404, f"<div class='card'><h1>{t('invalid_title')}</h1><p>{t('invalid')}</p></div>")
            set_htpasswd(user, p1)
        log.info("Password set for '%s' via invitation", user)
        self.send(200, f"<div class='card'><h1>{t('done_title')}</h1><p class='ok'>{t('done')}</p>"
                       f"<p class='mut'>{t('done_once')}</p>{instructions(user)}</div>")

    # -- admin
    def admin_post(self, path):
        f = self.form()
        if f is None or not self.same_origin():
            return self.admin(400, "<div class='card'><p class='err'>Bad request.</p></div>")
        if path == "/admin/login":
            return self.login(f)
        sid, s = self.session()
        if not s:
            return self.redirect("/admin")
        if not secrets.compare_digest(f.get("csrf", ""), s["csrf"]):
            log.warning("Admin: CSRF token mismatch on %s", path)
            return self.admin(403, "<div class='card'><p class='err'>Form expired – "
                                   "<a href='/admin'>reload</a>.</p></div>")
        name = f.get("name", "")
        try:
            if path == "/admin/logout":
                SESSIONS.drop(sid)
                log.info("Admin: signed out")
                return self.redirect("/admin", [("Set-Cookie", f"{COOKIE}=; Path=/; Secure; HttpOnly; "
                                                               f"SameSite=Strict; Max-Age=0")])
            if path == "/admin/sync":
                trigger_sync()
                s["flash"] = "Sync round triggered."
                return self.redirect("/admin")
            if path == "/admin/add":
                name, mailbox = userops.validate_new_user(name, f.get("mailbox", ""))
                check = userops.check_access(mailbox)
                userops.add_user(name, mailbox)
                inv = userops.new_invite(name, HOURS)
                log.info("Admin: added user '%s' -> %s, invitation issued", name, mailbox)
                return self.admin(200, invite_result(name, inv, check))
            if path == "/admin/invite":
                inv = userops.new_invite(name, HOURS)
                log.info("Admin: invitation issued for '%s'", name)
                return self.admin(200, invite_result(name, inv))
            if path == "/admin/resync":
                userops.resync_user(name)
                log.info("Admin: full resync of '%s' triggered", name)
                s["flash"] = f"Full resync of {name} triggered."
                return self.redirect("/admin")
            if path == "/admin/delete-confirm":
                mailbox = load_users().get(name, {}).get("mailbox")
                if not mailbox:
                    raise ValueError(f"Unknown user '{name}'.")
                return self.admin(200, delete_confirm(s, name, mailbox))
            if path == "/admin/delete":
                userops.delete_user(name)
                log.info("Admin: deleted user '%s'", name)
                s["flash"] = f"User {name} deleted."
                return self.redirect("/admin")
        except ValueError as err:
            return self.admin(400, f"<div class='card'><p class='err'>{e(str(err))}</p>"
                                   f"<p><a href='/admin'>← Back</a></p></div>")
        return self.not_found()

    def login(self, f):
        wait = THROTTLE.wait()
        if wait:
            return self.admin(429, login_page(f"Too many failed attempts – try again in {wait} s."))
        if not verify_admin_password(f.get("password", "")):
            THROTTLE.failed()
            log.warning("Admin: failed login from %s", self.client_address[0])
            time.sleep(1)
            return self.admin(401, login_page("Wrong password."))
        THROTTLE.ok()
        ADMIN_INITIAL.unlink(missing_ok=True)
        sid = SESSIONS.create()
        log.info("Admin: signed in from %s", self.client_address[0])
        self.redirect("/admin", [("Set-Cookie", f"{COOKIE}={sid}; Path=/; Secure; HttpOnly; "
                                                f"SameSite=Strict")])


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        log.warning("%s: %s", client_address[0], sys.exc_info()[1])


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s [web] %(message)s")
    srv = Server(("0.0.0.0", PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(TLS_FULLCHAIN, TLS_KEY)
    # Handshake happens in the worker thread, so a stalled client cannot block accept()
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    log.info("Listening on port %d (HTTPS): invitations %s, admin UI %s", PORT,
             "on" if INVITES else "off", "on" if ADMIN_UI else "off")
    srv.serve_forever()


if __name__ == "__main__":
    main()
