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

Copy `graph-cert.cer` to the machine where you run PowerShell, then:

```powershell
./scripts/1-Register-EntraApp.ps1 -CertPath ./graph-cert.cer
```

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
./scripts/2-Grant-MailboxAccess.ps1 `
    -AppId <CLIENT_ID> -ServicePrincipalId <ServicePrincipalId> `
    -Mailbox user@contoso.com -AdminUpn admin@contoso.onmicrosoft.com
```

**Several users:** create a group (Microsoft 365 group, mail-enabled security group or
distribution list), add the users as **direct** members (nested groups are ignored), then:

```powershell
./scripts/2-Grant-MailboxAccess.ps1 `
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

For several users, also enable the one-time links (see [Passwords](#passwords)):
add `-p 5233:5233 -e ENROLLMENT=true -e LANGUAGE=de` (or `en`).

Tip: save this as `run.sh` with `docker rm -f m365-carddav 2>/dev/null` as the first line; an
update is then just `docker build …` + `./run.sh`.

### 7. Add users

```sh
docker exec -it m365-carddav user add alice alice@contoso.com
```

This checks that the app can read the mailbox's contacts (warns on HTTP 403 if the RBAC
assignment has not propagated yet – it can take up to 2 hours, the sync keeps retrying) and
starts the first sync immediately. With `ENROLLMENT=true` it then prints a one-time link and a
ready-to-send message for the user; without it, it asks you for the CardDAV password.

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

macOS warns that it cannot verify the server identity → *Show Certificate* → set *When using
this certificate* to **Always Trust** → enter your Mac password. The address book **"M365"**
appears in Contacts shortly after.

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
| `user invite <name> [--hours N]` | New one-time link for the user to choose a password (needs `ENROLLMENT=true`) |
| `user passwd <name>` | Set the CardDAV password yourself |
| `user del <name>` | Delete the user, its password and its address book |
| `user sync` | Run a sync round now |
| `user resync <name>` | Throw away the sync state of one user and do a full resync |

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

| | `ENROLLMENT` off (default) | `ENROLLMENT=true` |
|---|---|---|
| Who chooses the password | the admin (`user passwd`) | the user, via a one-time link |
| Admin ever sees it | yes | **no** |
| Extra listener | none | port 5233 (HTTPS) |
| Reset / forgotten password | `user passwd` | `user invite <name>` – the old password keeps working until the new one is set |

The enrollment endpoint is deliberately minimal:

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
in front of it, which this project intentionally does not do (see below).

If clients reach the container under a different name or port (NAT, reverse proxy), set
`CARDDAV_URL` and `ENROLLMENT_URL` so the links and instructions are correct.

### Why a CLI and not a web admin UI?

A web UI looks more convenient, but for this project it would be the weakest point of the whole
setup:

- **The admin role is worth more than every single account.** Whoever can map a CardDAV login
  to a mailbox can read the contacts of *every* mailbox in the RBAC scope – just add yourself a
  user pointing to the CEO's mailbox. A web login for that power is a new, high-value target on
  the network, reachable by anyone who can reach the CardDAV port.
- **Doing it right is a lot of security-critical code:** login and session handling, CSRF
  protection, brute-force throttling, password storage for admins, audit logging, a separate
  port or path, TLS for it, plus updates whenever a dependency has a vulnerability. Every
  mistake there is a data leak; none of it adds functionality.
- **The CLI has no attack surface of its own.** `docker exec` requires root (or docker group
  membership) on the host. Anyone who has that can read `data/` anyway, so the CLI grants
  nothing that was not already available – it inherits the host's access control, SSH keys,
  MFA, logging.
- **The target group already has it.** Whoever runs this container for several users is an
  administrator with shell access to the Docker host. Adding a user is one command and happens
  rarely.
- **Leaving users out of the loop is a feature.** Granting access to a mailbox is a decision for
  the Exchange admin (group membership); mapping it to a login is a decision for the host admin.
  Self-service would merge both into one web form.

The optional enrollment page is not an admin UI: it cannot map logins to mailboxes, cannot see
other users and becomes useless once its single link is used.

If a UI is needed later, it can be built on top of the same CLI/`users.json` – but it should
then live behind existing authentication (e.g. an identity-aware proxy with Entra ID SSO),
not with its own password form.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TENANT_ID` | yes | | Entra tenant ID |
| `CLIENT_ID` | yes | | App (client) ID from step 4 |
| `CARDDAV_USER` + `MAILBOX` | | | Optional single-user shortcut, creates the mapping on start (see step 7) |
| `TLS_HOSTNAME` | yes* | | Hostname for the self-signed certificate (*not needed if you provide your own) |
| `ENROLLMENT` | | `false` | Enable one-time password links on port 5233 |
| `ENROLLMENT_HOURS` | | `72` | Validity of invitation links |
| `LANGUAGE` | | `en` | Language of the enrollment page and invitation text (`en`, `de`) |
| `CARDDAV_URL` | | `https://<TLS_HOSTNAME>:5232` | Base URL clients use (shown in instructions) |
| `ENROLLMENT_URL` | | `https://<TLS_HOSTNAME>:5233` | Base URL of the invitation links |
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
warnings, neither in Contacts nor on the enrollment page): put these PEM files into
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
./scripts/Remove-MailboxAccess.ps1 -AppId <CLIENT_ID> -AdminUpn admin@contoso.onmicrosoft.com
./scripts/Remove-EntraApp.ps1 -AppId <CLIENT_ID>
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
| Invitation link does not load | Port 5233 not published (`-p 5233:5233`) or `ENROLLMENT` not `true`. |

## License

MIT – see [LICENSE](LICENSE). Not affiliated with Microsoft or Apple.
