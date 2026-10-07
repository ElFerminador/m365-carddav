# Changelog

## 1.4.0
- Optional admin web UI (`ADMIN_UI=true`) at `https://<host>:5233/admin`: user list with sync
  status, add user (Graph access check + invitation), new invitation, resync, delete.
  Initial admin password generated into a 0600 file (never logged), `resetadminpw` to renew.
  Global login throttle, `__Host-` session cookie, CSRF tokens, no JavaScript, audit log.
- Invitation links now use `/invite/<token>` (`/enroll/<token>` still works).
- Stricter validation of mailbox addresses; CLI and web UI share the same user operations.

## 1.3.0
- Optional self-service passwords: `ENROLLMENT=true` starts a minimal HTTPS endpoint on port 5233;
  `user invite <name>` (and `user add`) print a one-time link plus a ready-to-send message.
  The admin never sees the user's password. Tokens: 256 bit, single use, expiring, stored hashed.
- Enrollment page and invitation text in English or German (`LANGUAGE`).
- `CARDDAV_URL` / `ENROLLMENT_URL` for setups behind NAT or a proxy.
- Own TLS certificates: optional `tls-chain.pem` for intermediate CAs; certificate, key, chain
  order, host name and expiry are validated on start; expiry is also checked during operation.

## 1.2.0
- Multiple users per container: `users.json` mapping, per-user sync state, errors isolated per user.
- New CLI `user` (`list`, `add`, `del`, `passwd`, `sync`, `resync`), changes apply without restart;
  `user add` checks Graph access and triggers an immediate sync.
- Static Radicale rights with `{user}` placeholders: each user reads only their own address book.
- `2-Grant-MailboxAccess.ps1 -Group` scopes the app to the direct members of a group.
- `CARDDAV_USER` + `MAILBOX` remain as optional single-user shortcut; 1.x state is migrated.

## 1.1.0
- Built-in HTTPS (self-signed certificate for `TLS_HOSTNAME`, or bring your own).

## 1.0.0
- Initial version: Graph delta sync of the default contacts folder into Radicale.
