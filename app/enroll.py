#!/usr/bin/env python3
"""Minimal one-time enrollment endpoint (only started with ENROLLMENT=true).

The ONLY thing it can do: set the CardDAV password of the one user an invitation was
issued for, once, before the invitation expires. No other routes, no sessions, no admin.
"""
import hashlib
import html
import http.server
import logging
import os
import re
import ssl
import sys
import threading
import time
from urllib.parse import parse_qs

from common import (TLS_FULLCHAIN, TLS_KEY, carddav_url, consume_invite, find_invite, load_users,
                    set_htpasswd, t)

log = logging.getLogger("enroll")
PORT = 5233
MIN_LEN = 12
TOKEN_RE = re.compile(r"^/enroll/([A-Za-z0-9_-]{20,100})$")
_lock = threading.Lock()

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
main{{max-width:34rem;margin:2rem auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:1.5rem}}
h1{{font-size:1.35rem;margin:0 0 .75rem}} p{{margin:.5rem 0}} .mut{{color:var(--mut);font-size:.9rem}}
label{{display:block;margin:1rem 0 .25rem;font-weight:600}}
input{{width:100%;box-sizing:border-box;padding:.6rem .7rem;font:inherit;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg)}}
button{{margin-top:1.25rem;width:100%;padding:.7rem;font:inherit;font-weight:600;border:0;border-radius:8px;background:var(--ac);color:#fff;cursor:pointer}}
.err{{color:var(--err);font-weight:600}} .ok{{color:var(--ok);font-weight:600}}
code{{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:.1rem .35rem;word-break:break-all}}
ol{{padding-left:1.2rem}} li{{margin:.35rem 0}}
</style></head><body><main><div class="card">{body}</div></main></body></html>"""


def instructions(user):
    return (f"<h2 style='font-size:1.05rem;margin-top:1.5rem'>{t('mac_title')}</h2><ol>"
            f"<li>{t('mac_1')}</li><li>{t('mac_2')}</li>"
            f"<li>{t('mac_3')}<br>{t('user_name')}: <code>{html.escape(user)}</code><br>"
            f"{t('server')}: <code>{html.escape(carddav_url(user))}</code></li>"
            f"<li>{t('mac_4')}</li></ol>")


def form(user, mailbox, error=""):
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    return (f"<h1>{t('title')}</h1>"
            f"<p>{t('intro')}</p>"
            f"<p class='mut'>{t('user_name')}: <code>{html.escape(user)}</code><br>"
            f"{t('mailbox')}: {html.escape(mailbox)}</p>{err}"
            f"<form method='post' autocomplete='off'>"
            f"<input type='text' name='username' value='{html.escape(user)}' autocomplete='username' hidden>"
            f"<label for='p1'>{t('new_pw')}</label>"
            f"<input id='p1' name='p1' type='password' minlength='{MIN_LEN}' required autocomplete='new-password'>"
            f"<label for='p2'>{t('repeat_pw')}</label>"
            f"<input id='p2' name='p2' type='password' minlength='{MIN_LEN}' required autocomplete='new-password'>"
            f"<p class='mut'>{t('pw_hint').format(n=MIN_LEN)}</p>"
            f"<button type='submit'>{t('save')}</button></form>")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "enroll"
    sys_version = ""
    timeout = 20  # slow or idle clients are dropped

    def log_message(self, fmt, *args):  # never log the token
        log.info("%s %s -> %s", self.client_address[0], self.command, args[1] if len(args) > 1 else "")

    def send(self, code, body):
        data = PAGE.format(lang=t("lang"), title=t("title"), body=body).encode()
        self.send_response(code)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def invalid(self):
        time.sleep(1)  # slows down guessing (tokens have 256 bit anyway)
        self.send(404, f"<h1>{t('invalid_title')}</h1><p>{t('invalid')}</p>")

    def lookup(self):
        m = TOKEN_RE.match(self.path.split("?", 1)[0])
        if not m:
            return None, None, None
        token_hash = hashlib.sha256(m.group(1).encode()).hexdigest()
        invite = find_invite(token_hash)
        if not invite:
            return None, None, None
        user = invite["user"]
        mailbox = load_users().get(user, {}).get("mailbox")
        if not mailbox:
            return None, None, None
        return token_hash, user, mailbox

    def do_GET(self):
        _, user, mailbox = self.lookup()
        if not user:
            return self.invalid()
        self.send(200, form(user, mailbox))

    def do_POST(self):
        token_hash, user, mailbox = self.lookup()
        if not user:
            return self.invalid()
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            return self.send(413, f"<p class='err'>{t('too_large')}</p>")
        fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        p1, p2 = fields.get("p1", [""])[0], fields.get("p2", [""])[0]
        if len(p1) < MIN_LEN:
            return self.send(400, form(user, mailbox, t("err_short").format(n=MIN_LEN)))
        if p1 != p2:
            return self.send(400, form(user, mailbox, t("err_mismatch")))
        with _lock:
            if not consume_invite(token_hash):  # used concurrently / expired meanwhile
                return self.invalid()
            set_htpasswd(user, p1)
        log.info("Password set for '%s' via invitation", user)
        self.send(200, f"<h1>{t('done_title')}</h1><p class='ok'>{t('done')}</p>"
                       f"<p class='mut'>{t('done_once')}</p>{instructions(user)}")

    def do_HEAD(self):
        self.send_response(405)
        self.end_headers()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_PROPFIND = do_HEAD


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        log.warning("%s: %s", client_address[0], sys.exc_info()[1])


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s [enroll] %(message)s")
    srv = Server(("0.0.0.0", PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(TLS_FULLCHAIN, TLS_KEY)
    # Handshake happens in the worker thread, so a stalled client cannot block accept()
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    log.info("Enrollment endpoint listening on port %d (HTTPS)", PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
