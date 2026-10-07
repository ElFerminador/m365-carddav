# m365-carddav

**Microsoft 365 contacts on macOS after the EWS shutdown.**

Since October 2026 Microsoft is switching off Exchange Web Services (EWS) in Exchange Online
(final shutdown: 1 April 2027). The macOS Contacts app still talks to Exchange via EWS, so your
M365 contacts disappear from your Mac. Apple has announced Microsoft Graph support for a
future macOS 27 update, without a date.

`m365-carddav` closes the gap: a single Docker container that

1. reads the **default contacts folder** of one M365 mailbox via Microsoft Graph (delta queries,
   every 15 minutes), and
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
  Exchange Online *RBAC for Applications*, scoped to exactly one mailbox.
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
- One mailbox per container.
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

### 5. Grant access to the mailbox

```powershell
./scripts/2-Grant-MailboxAccess.ps1 `
    -AppId <CLIENT_ID> -ServicePrincipalId <ServicePrincipalId> `
    -Mailbox user@contoso.com -AdminUpn admin@contoso.onmicrosoft.com
```

The output must show `Application Contacts.Read` with `InScope = True`.

> **Do not** add `Contacts.Read` as an API permission in Entra and do not grant admin consent.
> Entra consent and Exchange RBAC are additive: consent would open *every* mailbox in the tenant.

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
  -e MAILBOX=user@contoso.com \
  -e CARDDAV_USER=alice \
  -e TLS_HOSTNAME=nas.example.lan \
  -v /volume1/docker/m365-carddav/data:/data \
  m365-carddav:latest
```

Tip: save this as `run.sh` with `docker rm -f m365-carddav 2>/dev/null` as the first line; an
update is then just `docker build …` + `./run.sh`.

Set the CardDAV password for `CARDDAV_USER` (min. 12 characters, takes effect immediately):

```sh
docker exec -it m365-carddav set-password
```

Watch the first sync:

```sh
docker logs -f m365-carddav
```

Expected: `Full sync: N written, 0 deleted, …`. If the RBAC assignment has not propagated yet,
you will see HTTP 403; the container retries every 15 minutes on its own.

Test from a client (`-k` because the certificate is self-signed):

```sh
curl -sk -o /dev/null -w "%{http_code}\n" -u alice -X PROPFIND -H "Depth: 0" \
  https://nas.example.lan:5232/alice/
```

`207` means server, password and path are fine.

### 7. Add the account on macOS

System Settings → Internet Accounts → Add Account → Add Other Account → **CardDAV Account**:

| Field | Value |
|---|---|
| Account Type | **Manual** |
| User Name | `alice` |
| Password | from step 6 |
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

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TENANT_ID` | yes | | Entra tenant ID |
| `CLIENT_ID` | yes | | App (client) ID from step 4 |
| `MAILBOX` | yes | | Mailbox whose default contacts folder is synced |
| `CARDDAV_USER` | yes | | CardDAV login name for your clients (`a-z 0-9 . _ -`) |
| `TLS_HOSTNAME` | yes* | | Hostname for the self-signed certificate (*not needed if you provide your own) |
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
  The `deltaLink` is stored in `data/state/state.json`.
- **Full reconciliation** once a day (and when Graph answers `410 Gone`): all contacts are
  fetched, unchanged ones (same `lastModifiedDateTime`) are skipped, orphans on the CardDAV
  side are deleted.
- **Rights:** Radicale `rights` file – the internal user `sync` may write the address book,
  `CARDDAV_USER` may only read it. Both passwords are bcrypt hashes in `data/config/htpasswd`.
- **Processes:** `tini` → `entrypoint.sh` runs Radicale and the sync loop; if either dies,
  the container exits and Docker restarts it.

## Files in `data/`

| Path | Content | Secret? |
|---|---|---|
| `config/graph-key.pem` | Private key for Graph authentication | **yes** |
| `config/graph-cert.pem`, `graph-cert.cer` | Public Graph certificate | no |
| `config/tls-key.pem`, `tls-cert.pem` | HTTPS certificate of the CardDAV server | key: **yes** |
| `config/htpasswd`, `sync.secret` | CardDAV password hashes / internal sync password | **yes** |
| `config/radicale.conf`, `rights` | Regenerated on every start | no |
| `state/state.json` | Delta link and per-contact hashes | no |
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

**Renew the TLS certificate** (valid 825 days): delete `data/config/tls-cert.pem` and
`tls-key.pem`, restart the container, trust the new certificate on each Mac once.

**Use your own TLS certificate:** place `tls-cert.pem` (incl. chain) and `tls-key.pem` in
`data/config/`, owned by the container user, and restart. No trust prompt on the Macs.

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
| Log: HTTP 403 from Graph | RBAC assignment not propagated yet – wait; check with `Test-ServicePrincipalAuthorization`. |
| Log: `No contacts in the default folder` | Add one contact in Outlook, restart the container. |
| Radicale log: `Bad request syntax ('\x16\x03\x01…')` | A client speaks TLS to a plain-HTTP server. Fixed since TLS is built in; make sure you use `https://`. |
| macOS: *Unable to verify account name or password*, nothing in the container log | Use account type **Manual** with the full URL (step 7). |
| `curl` returns 401 | Wrong password or `set-password` not run. |

## License

MIT – see [LICENSE](LICENSE). Not affiliated with Microsoft or Apple.
