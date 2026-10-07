# m365-carddav

**Microsoft 365 contacts on macOS after the EWS shutdown.**

Since October 2026 Microsoft is switching off Exchange Web Services (EWS) in Exchange Online
(final shutdown: 1 April 2027). The macOS Contacts app still talks to Exchange via EWS, so your
M365 contacts disappear from your Mac. Apple has announced Microsoft Graph support for a
future macOS 27 update, without a date.

`m365-carddav` closes the gap: a single Docker container that

1. reads the **default contacts folder** of one or more M365 mailboxes via Microsoft Graph
   (delta queries, every 15 minutes), and
2. serves it through a built-in **CardDAV server** ([Radicale](https://radicale.org)), which
   macOS (and iOS, Thunderbird, …) can subscribe to natively.

```
M365 mailbox ──Graph delta──▶ [ m365-carddav container ] ──CardDAV/HTTPS──▶ macOS Contacts
 (source of truth)             sync loop + Radicale                         (read-only)
```

When Apple ships native Graph support, you delete the container, the folder and the Entra app,
and nothing is left behind.

## Features

- **One-way, M365 is the master.** Contacts are read-only on the Mac; edit them in Outlook/OWA.
- **Least privilege.** The Entra app gets *no* tenant-wide permission. Access is granted with
  Exchange Online *RBAC for Applications*, scoped to one mailbox or to the members of one group.
- **Multiple users** in one container, managed with a small CLI (`user add / del / list`),
  no restart needed. Every user only sees their own address book.
- **Self-service password via one-time link** (optional): the admin never sees or chooses a
  user's CardDAV password.
- **Admin web UI** (optional) for users, invitations and sync status – or the CLI only.
- **Certificate authentication** (no client secret, no refresh token that breaks on MFA or
  password changes).
- **Stable keys.** Uses Graph immutable IDs as vCard `UID`; a changed address simply overwrites
  the whole vCard.
- **Delta sync** every 15 minutes, nightly full reconciliation, automatic resync on expired
  delta tokens.
- Contact photos, multiple e-mail addresses, phone types, addresses, birthday, notes, categories,
  company-only contacts.
- Runs as **non-root** (`--user`), `--cap-drop ALL`, HTTPS with an auto-generated certificate.
- Offline: macOS keeps a full local copy of CardDAV accounts.

## Limitations

- Only the **default contacts folder** (no subfolders, no shared/public folders).
- One CardDAV user per mailbox; shared mailboxes and delegated access are not covered.
- The default contacts folder must contain at least one contact when the container starts
  for the first time (used to determine its folder ID).
- Changes made on the Mac are rejected by the server (by design).

## Requirements

- A host that runs Docker 24/7 (a Synology/QNAP NAS, a small Linux box, …).
- Network access from your Macs to that host (LAN, VPN, Tailscale, …).
- Microsoft 365 / Exchange Online, and:
  - an account that can create app registrations (Application Administrator or Global Admin),
  - an account with the **Exchange Administrator** role (or Organization Management).
- PowerShell 7 with the modules `Microsoft.Graph.Applications` and `ExchangeOnlineManagement`.

---

## Step-by-step setup

The examples use `/volume1/docker/m365-carddav` as the base directory (Synology style) and
UID/GID `1027:100`. Adapt both to your host. Commands assume a root shell on the Docker host.

### 1. Get the code and build the image

```sh
mkdir -p /volume1/docker/m365-carddav
cd /volume1/docker/m365-carddav
git clone https://github.com/ElFerminador/m365-carddav.git build
docker build -t m365-carddav:latest build
```

(No git on the host? Download the repository as ZIP and copy it to `.../m365-carddav/build`.)

### 2. Create the data directory

```sh
mkdir -p /volume1/docker/m365-carddav/data
chown -R 1027:100 /volume1/docker/m365-carddav/data
```

The container runs as `--user 1027:100` from the very first second, so it cannot fix
ownership itself.

> **Synology:** a `+` in `ls -l` means ACLs are active. If the next step fails with
> *Permission denied*, give the user read/write on `docker/m365-carddav/data` in File Station
> (Properties → Permission).

### 3. Create the Graph certificate

```sh
docker run --rm --user 1027:100 \
  -v /volume1/docker/m365-carddav/data:/data \
  m365-carddav:latest gen-cert
```

This writes `data/config/graph-key.pem` (private, never leaves the host) and
`data/config/graph-cert.cer` (public, uploaded to Entra in the next step). Valid for 2 years.

### 4. Register the Entra app

The PowerShell scripts live in the `scripts` folder of this repository. On the machine where
you run PowerShell 7, get a copy of the repository (clone or ZIP), copy `graph-cert.cer` from
the Docker host to that machine, then change into the folder and run step 1:

```powershell
cd <path-to-repository>/scripts
./1-Register-EntraApp.ps1 -CertPath <path-to>/graph-cert.cer
```

All script commands below assume you are in this folder. (The `./` is required: PowerShell
does not run scripts from the current directory without it.)

Note the three values printed: `TENANT_ID`, `CLIENT_ID` and `ServicePrincipalId`.

> **Error *"Could not load file or assembly 'Microsoft.Graph.Authentication …' Assembly with
> same name is already loaded"*:** your Graph modules have different versions. Check with
> `'Microsoft.Graph.Authentication','Microsoft.Graph.Applications' | % { Get-InstalledModule $_ -AllVersions }`
> and install the missing one in the same version, e.g.
> `Install-Module Microsoft.Graph.Applications -RequiredVersion <version of Authentication>`.
> Then restart `pwsh`.

### 5. Grant access to the mailbox(es)

**One mailbox:**

```powershell
./2-Grant-MailboxAccess.ps1 `
    -AppId <CLIENT_ID> -ServicePrincipalId <ServicePrincipalId> `
    -Mailbox user@contoso.com -AdminUpn admin@contoso.onmicrosoft.com
```

**Several users:** create a group (Microsoft 365 group, mail-enabled security group or
distribution list), add the users as **direct** members (nested groups are ignored), then:

```powershell
./2-Grant-MailboxAccess.ps1 `
    -AppId <CLIENT_ID> -ServicePrincipalId <ServicePrincipalId> `
    -Group carddav-users@contoso.com -AdminUpn admin@contoso.onmicrosoft.com
```

From then on, granting or revoking a user is a plain group membership change. Running the
script with `-Group` on an existing single-mailbox setup switches the scope to the group.

The output must show `InScope = True` for each mailbox.

> **Do not** add `Contacts.Read` as an API permission in Entra and do not grant admin consent.
> Entra consent and Exchange RBAC are additive: consent would open *every* mailbox in the tenant.
> No "impersonation" is involved either – `ApplicationImpersonation` was an EWS concept.

> **Error *"The term 'New-ServicePrincipal' is not recognized"*:** the signed-in account has no
> Exchange admin rights. Exchange Online only exposes cmdlets the account may run. Use `-AdminUpn`.

### 6. Start the container

**Single user** – you set the CardDAV password yourself:

```sh
docker run -d \
  --name m365-carddav \
  --restart unless-stopped \
  --user 1027:100 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -p 5232:5232 \
  -e TZ=Europe/Zurich \
  -e TENANT_ID=<TENANT_ID> \
  -e CLIENT_ID=<CLIENT_ID> \
  -e TLS_HOSTNAME=nas.example.lan \
  -v /volume1/docker/m365-carddav/data:/data \
  m365-carddav:latest
```

**Multiple users** – managed in a web admin UI, users choose their own password via a one-time
link (see [Admin web UI](#admin-web-ui) and [Passwords](#passwords)); same as above plus port
5233, `ADMIN_UI` and `LANGUAGE` (`de` or `en`, for the invitation page and text):

```sh
docker run -d \
  --name m365-carddav \
  --restart unless-stopped \
  --user 1027:100 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -p 5232:5232 \
  -p 5233:5233 \
  -e TZ=Europe/Zurich \
  -e TENANT_ID=<TENANT_ID> \
  -e CLIENT_ID=<CLIENT_ID> \
  -e TLS_HOSTNAME=nas.example.lan \
  -e ADMIN_UI=true \
  -e LANGUAGE=de \
  -v /volume1/docker/m365-carddav/data:/data \
  m365-carddav:latest
```

Prefer the command line over a web UI? Use `-e ENROLLMENT=true` instead of `-e ADMIN_UI=true`:
invitation links work the same, users are managed with `docker exec … user …` only.

Tip: save this as `run.sh` with `docker rm -f m365-carddav 2>/dev/null` as the first line; an
update is then just `docker build …` + `./run.sh`.

### 7. Add users

**Admin web UI** (with `ADMIN_UI=true`): open `https://nas.example.lan:5233/admin`. The initial
admin password was generated on the first start and is *not* written to the log; read it once:

```sh
docker exec m365-carddav cat /data/config/admin-initial-password.txt
```

The file is deleted after the first successful login – store the password in your password
manager. Lost it? `docker exec -it m365-carddav resetadminpw` prints a new one and signs out all
admin sessions.

In the UI, *Add user* takes the CardDAV user name and the mailbox, checks Graph access and shows
the one-time link plus a ready-to-send message. The table shows password/invitation state, last
sync and errors per user; *Invite*, *Resync* and *Delete* are per row.

**Command line** (always available):

```sh
docker exec -it m365-carddav user add alice alice@contoso.com
```

This checks that the app can read the mailbox's contacts (warns on HTTP 403 if the RBAC
assignment has not propagated yet – it can take up to 2 hours, the sync keeps retrying) and
starts the first sync immediately. With `ADMIN_UI=true` or `ENROLLMENT=true` it then prints a
one-time link and a ready-to-send message for the user; otherwise it asks you for the CardDAV
password.

```sh
docker exec m365-carddav user list        # users, last sync, errors
docker logs -f m365-carddav               # expected: [alice] Full sync: N written, …
```

Test from a client (`-k` because the certificate is self-signed):

```sh
curl -sk -o /dev/null -w "%{http_code}\n" -u alice -X PROPFIND -H "Depth: 0" \
  https://nas.example.lan:5232/alice/
```

`207` means server, password and path are fine.

> **Single-user shortcut:** instead of `user add` you can pass `-e CARDDAV_USER=alice
> -e MAILBOX=alice@contoso.com` to `docker run`; the mapping is created on start and you set
> the password with `docker exec -it m365-carddav user passwd alice`. Installations of 1.x
> keep working this way unchanged (their sync state is migrated automatically).

### 8. Add the account on macOS

System Settings → Internet Accounts → Add Account → Add Other Account → **CardDAV Account**:

| Field | Value |
|---|---|
| Account Type | **Manual** |
| User Name | `alice` |
| Password | from step 7 |
| Server Address | `https://nas.example.lan:5232/alice/` |

If macOS warns that it cannot verify the server identity (self-signed certificate) →
*Show Certificate* → set *When using this certificate* to **Always Trust** → enter your Mac
password.

**Rename the account:** macOS names the new account after the server URL, and that is what
Contacts shows as group header. In System Settings → Internet Accounts, select the new account
and change its **Description** to e.g. `M365`. (There is no terminal command for this – macOS
offers no CLI or AppleScript access to Internet Accounts.)

> **Use "Manual" with the full URL.** With *Advanced* (separate host/path/port fields) and
> with configuration profiles (`.mobileconfig`), macOS 26 shows *"Unable to verify account
> name or password"* without ever opening a connection to the server – verified with tcpdump.
> Only *Manual* works reliably.

If the dialog fails without asking about the certificate, trust it from the terminal first:

```sh
openssl s_client -connect nas.example.lan:5232 -servername nas.example.lan </dev/null 2>/dev/null \
  | openssl x509 > /tmp/m365-carddav.pem
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain /tmp/m365-carddav.pem
```

---

## Managing users

All commands run inside the container and take effect without a restart:

| Command | Effect |
|---|---|
| `user list` | Users, mailboxes, password set?, last sync, last error |
| `user add <name> <mailbox>` | Add a user (checks Graph access, asks for the password, syncs now) |
| `user invite <name> [--hours N]` | New one-time link for the user to choose a password (needs `ADMIN_UI` or `ENROLLMENT`) |
| `user passwd <name>` | Set the CardDAV password yourself |
| `user del <name>` | Delete the user, its password and its address book |
| `user sync` | Run a sync round now |
| `user resync <name>` | Throw away the sync state of one user and do a full resync |
| `resetadminpw` | New password for the admin web UI (signs out all admin sessions) |

Use `docker exec -it m365-carddav user …` (`-it` is needed for password prompts and
confirmations). `user del` does not touch Exchange: remove the mailbox from the scope group as
well if the app should lose access to it.

Rights are enforced by Radicale itself: every user may only read `/<name>/m365/`; only the
internal `sync` user may write, and it cannot read anything else.

## Passwords

The CardDAV password is **not** the user's Microsoft 365 password. It exists only in the
container (bcrypt hash) and is used solely by the Contacts app to log in to the CardDAV server.
The container reads the mailbox with its own certificate, so M365 password changes, MFA or
Conditional Access have no effect on it. To cut a user off, run `user del` and remove the
mailbox from the scope group.

There are two ways to set it:

| | default | `ENROLLMENT=true` or `ADMIN_UI=true` |
|---|---|---|
| Who chooses the password | the admin (`user passwd`) | the user, via a one-time link |
| Admin ever sees it | yes | **no** |
| Extra listener | none | port 5233 (HTTPS) |
| Reset / forgotten password | `user passwd` | `user invite <name>` – the old password keeps working until the new one is set |

The invitation page (`/invite/<token>`) is deliberately minimal:

- It can do exactly one thing: set the password of the one user an invitation was issued for.
  There is no login, no session, no listing, no other route.
- Tokens have 256 bits, are valid once and for `ENROLLMENT_HOURS` (default 72); a new
  invitation replaces the previous one. Only their SHA-256 is stored (`config/invites.json`).
- Tokens never appear in the log; responses carry `no-store`, `no-referrer`, a strict CSP and
  `X-Frame-Options: DENY`; invalid tokens are answered with a delay.
- The link is as sensitive as a password until it is used: send it to the user directly
  (e-mail to the mailbox in question, Teams chat), not to a shared channel.

It does **not** prove the user's identity – whoever holds the link can set the password, just
as whoever intercepts a password could use it. For identity proof you would put Entra ID sign-in
in front of it, which this project does not do.

If clients reach the container under a different name or port (NAT, reverse proxy), set
`CARDDAV_URL` and `ENROLLMENT_URL` (base URL of port 5233) so links and instructions are
correct.

## Admin web UI

Enabled with `ADMIN_UI=true`, served at `https://<host>:5233/admin` – the same port as the
invitation links. It can list users with their sync status, add users (with Graph access check
and invitation), issue new invitations, trigger a (full) sync and delete users. It **cannot**
show or set a user's password and cannot change anything in Exchange.

### Why this needs care

The admin role is worth more than every single account: whoever can map a CardDAV login to a
mailbox can read the contacts of *every* mailbox in the RBAC scope. And because users must reach
port 5233 to open their invitation, the admin login is reachable from the same network. The UI is
therefore built defensively:

| Measure | Details |
|---|---|
| Password | 24 random characters (144 bit), generated by the container, stored as bcrypt hash in `config/admin.passwd`. Never logged; the initial one is written to a 0600 file that is deleted after the first login. |
| Brute force | Global throttle: after 5 failed logins the login is locked for 1 minute, doubling up to 15 minutes; every failure is logged. Global on purpose – behind Docker's port mapping all clients appear with the same source IP. |
| Sessions | 256-bit random ID in a `__Host-` cookie (`Secure`, `HttpOnly`, `SameSite=Strict`), in memory only (a container restart signs everybody out); 30 min idle / 12 h absolute timeout; invalidated by `resetadminpw`. |
| CSRF | Per-session token on every form plus `Origin` check. |
| Output | Everything HTML-escaped, no JavaScript at all, strict CSP, `X-Frame-Options: DENY`, `no-store`, `no-referrer`. |
| Audit | Logins, failed logins and every change are logged (`docker logs`), without passwords or tokens. |
| Recovery | `docker exec -it m365-carddav resetadminpw` – requires root/docker rights on the host. |

Residual risk you should be aware of:

- Anyone who can reach port 5233 can *try* to log in. Do not expose 5233 (or 5232) to the
  internet; LAN/VPN only.
- The throttle can be abused to lock the admin out for up to 15 minutes. The command line still
  works then.
- No second factor. If you want one, set `ENROLLMENT=true` instead of `ADMIN_UI=true` and manage
  users with the CLI, or put port 5233 behind an identity-aware proxy (e.g. Entra application
  proxy, Cloudflare Access, Tailscale ACLs) – the UI works unchanged behind it.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TENANT_ID` | yes | | Entra tenant ID |
| `CLIENT_ID` | yes | | App (client) ID from step 4 |
| `CARDDAV_USER` + `MAILBOX` | | | Optional single-user shortcut, creates the mapping on start (see step 7) |
| `TLS_HOSTNAME` | yes* | | Hostname for the self-signed certificate (*not needed if you provide your own) |
| `ADMIN_UI` | | `false` | Admin web UI on port 5233 (`/admin`); implies invitation links |
| `ENROLLMENT` | | `false` | Invitation links on port 5233 without the admin UI |
| `ENROLLMENT_HOURS` | | `72` | Validity of invitation links |
| `LANGUAGE` | | `en` | Language of the invitation page and text (`en`, `de`); the admin UI is English |
| `CARDDAV_URL` | | `https://<TLS_HOSTNAME>:5232` | Base URL clients use (shown in instructions) |
| `ENROLLMENT_URL` | | `https://<TLS_HOSTNAME>:5233` | Base URL of port 5233 (invitation links, admin UI) |
| `SYNC_INTERVAL` | | `900` | Seconds between delta syncs |
| `FULL_SYNC_HOUR` | | `3` | Hour (container local time) of the daily full reconciliation |
| `SYNC_PHOTOS` | | `true` | Sync contact photos |
| `COLLECTION` | | `m365` | Address book path segment |
| `TZ` | | UTC | Time zone, e.g. `Europe/Zurich` |

## How it works

- **Primary key:** every Graph request sends `Prefer: IdType="ImmutableId"`. The immutable ID
  becomes the vCard `UID`; the resource name is `sha256(id).vcf` (IDs contain `/+=`).
- **Delta sync:** `GET /users/{mailbox}/contactFolders/{default}/contacts/delta`. New or
  changed contacts are rebuilt as vCard 3.0 and `PUT`; `@removed` entries are `DELETE`d.
  The `deltaLink` is stored per user in `data/state/<user>.json`.
- **Full reconciliation** once a day (and when Graph answers `410 Gone`): all contacts are
  fetched, unchanged ones (same `lastModifiedDateTime`) are skipped, orphans on the CardDAV
  side are deleted.
- **Users:** `data/config/users.json` maps CardDAV logins to mailboxes; it is re-read before
  every sync round. `user add` touches a trigger file that wakes the sync loop immediately.
- **Rights:** static Radicale `rights` file using `{user}` placeholders – every user may read
  only their own address book, the internal user `sync` may write all address books and read
  nothing else. All passwords are bcrypt hashes in `data/config/htpasswd`.
- **Isolation:** an error in one mailbox (e.g. HTTP 403) is logged and shown in `user list`,
  the other users keep syncing.
- **Processes:** `tini` → `entrypoint.sh` runs Radicale and the sync loop; if either dies,
  the container exits and Docker restarts it.

## Files in `data/`

| Path | Content | Secret? |
|---|---|---|
| `config/graph-key.pem` | Private key for Graph authentication | **yes** |
| `config/graph-cert.pem`, `graph-cert.cer` | Public Graph certificate | no |
| `config/tls-key.pem`, `tls-cert.pem`, `tls-chain.pem` | HTTPS certificate of the CardDAV server (own or self-signed) | key: **yes** |
| `config/tls-fullchain.pem` | Certificate + chain, regenerated on every start | no |
| `config/htpasswd`, `sync.secret` | CardDAV password hashes / internal sync password | **yes** |
| `config/users.json` | CardDAV user → mailbox mapping | no |
| `config/invites.json` | SHA-256 of open invitation tokens + expiry | no |
| `config/admin.passwd` | bcrypt hash of the admin UI password | **yes** |
| `config/admin-initial-password.txt` | Initial admin password, deleted after the first login | **yes** |
| `config/radicale.conf`, `rights` | Regenerated on every start | no |
| `state/<user>.json` | Delta link, per-contact hashes, last sync status | no |
| `radicale/collections/` | The vCards | personal data |

Nothing in `data/` needs a separate backup: everything can be recreated.

## Maintenance

**Update**

```sh
cd /volume1/docker/m365-carddav/build && git pull
docker build -t m365-carddav:latest .
./run.sh
```

Contacts, sync state and passwords survive in `data/`.

**Renew the Graph certificate** (the log warns 30 days before expiry):

```sh
docker run --rm --user 1027:100 -v /volume1/docker/m365-carddav/data:/data m365-carddav:latest gen-cert --force
```

Upload the new `graph-cert.cer` in Entra (App registrations → m365-carddav → Certificates &
secrets), remove the old one, then `docker restart m365-carddav`.

### TLS certificate

Without further action the container creates a **self-signed** certificate for `TLS_HOSTNAME`
(valid 825 days); every Mac has to trust it once.

**Your own certificate** (e.g. from an internal CA that all your Macs already trust – no
warnings, neither in Contacts nor on the invitation page and admin UI): put these PEM files into
`data/config/` and restart the container:

| File | Content | Required |
|---|---|---|
| `tls-cert.pem` | Server certificate (may also contain the chain behind it) | yes |
| `tls-key.pem` | Unencrypted private key of that certificate | yes |
| `tls-chain.pem` | Intermediate CA certificate(s), **issuing CA first** | only if the server certificate was issued by an intermediate CA that your clients do not have installed |

The root CA itself does not belong into the chain – clients must already trust it.

```sh
cp nas.crt  /volume1/docker/m365-carddav/data/config/tls-cert.pem
cp nas.key  /volume1/docker/m365-carddav/data/config/tls-key.pem
cp issuing-ca.crt /volume1/docker/m365-carddav/data/config/tls-chain.pem   # only if needed
chown 1027:100 /volume1/docker/m365-carddav/data/config/tls-*.pem
chmod 600 /volume1/docker/m365-carddav/data/config/tls-key.pem
docker restart m365-carddav
docker logs m365-carddav 2>&1 | grep "\[init\]"
```

On start the container checks the files and logs e.g.
`TLS: CN=nas.example.lan, issued by CN=Example Issuing CA, valid until 2028-10-06, 1 intermediate(s)`.
It refuses to start if the key does not match the certificate or the certificate has expired,
and warns if `TLS_HOSTNAME` is not in the Subject Alternative Names, the chain is in the wrong
order, or the certificate expires within 30 days (also checked during operation).

Requirements of macOS for TLS server certificates – also for private CAs: DNS name in the
**Subject Alternative Name**, Extended Key Usage **serverAuth**, RSA ≥ 2048 bit (or ECDSA),
SHA-2 signature and a validity of **at most 825 days**. The stricter 398-day limit only applies
to certificates from public CAs, so 2-year certificates from an internal CA are fine.

To return to a self-signed certificate, delete `tls-cert.pem`, `tls-key.pem` and
`tls-chain.pem` and restart. Renewing works the same way as installing: replace the files,
restart.

## Uninstall

```sh
docker rm -f m365-carddav
docker rmi m365-carddav:latest
rm -rf /volume1/docker/m365-carddav
```

```powershell
cd <path-to-repository>/scripts
./Remove-MailboxAccess.ps1 -AppId <CLIENT_ID> -AdminUpn admin@contoso.onmicrosoft.com
./Remove-EntraApp.ps1 -AppId <CLIENT_ID>
```

Remove the CardDAV account on every Mac.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `pull access denied for m365-carddav` | Image not built yet (step 1). |
| `Permission denied` on `/data` | `chown` (step 2) or Synology ACLs. |
| Log / `user list`: HTTP 403 from Graph | Mailbox not in the RBAC scope (not a *direct* group member?) or not propagated yet – wait; check with `Test-ServicePrincipalAuthorization`. |
| Log: `No contacts in the default folder` | Add one contact in Outlook, restart the container. |
| Radicale log: `Bad request syntax ('\x16\x03\x01…')` | A client speaks TLS to a plain-HTTP server. Fixed since TLS is built in; make sure you use `https://`. |
| macOS: *Unable to verify account name or password*, nothing in the container log | Use account type **Manual** with the full URL (step 8). |
| `curl` returns 401 | Wrong password or no password set (`user passwd` / `user invite`). |
| Invitation link: *Link not valid* | Used, expired or replaced by a newer invitation – `user invite <name>`. |
| Container does not start: `tls-key.pem does not belong …` | Key and certificate do not match, or the leaf is not the first certificate in `tls-cert.pem`. |
| Mac: certificate not trusted although the CA is installed | Intermediate missing → add `tls-chain.pem`. Check with `openssl s_client -connect host:5232 -showcerts`. |
| Invitation link / admin UI does not load | Port 5233 not published (`-p 5233:5233`), or neither `ADMIN_UI` nor `ENROLLMENT` is `true`. |
| Admin UI: *Too many failed attempts* | Wait (max. 15 min) or use the CLI. Forgotten password: `docker exec -it m365-carddav resetadminpw`. |
| Admin UI: *Form expired* | Session timed out (30 min idle) or the admin password was reset – reload and sign in. |

## License

MIT – see [LICENSE](LICENSE). Not affiliated with Microsoft or Apple.
