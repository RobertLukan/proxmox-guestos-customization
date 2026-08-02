# Installing GuestOS (+ optional PDM fork)

This document is the **production-oriented** install guide. Lab IPs and
self-signed shortcuts live in [PDM_INTEGRATION.md](PDM_INTEGRATION.md)
(lab notes) and [TLS_PRODUCTION.md](TLS_PRODUCTION.md).

## What “the product” is

| Piece | Role | How you install it |
|-------|------|--------------------|
| **GuestOS** | Sidecar that clones Windows templates and runs Sysprep customize | Docker Compose (or Podman Compose) on a Linux host that can reach Proxmox APIs |
| **PDM GuestOS fork** *(optional)* | Thin PDM UI + server proxy: **Customize** button + **GuestOS** task tab | Debian `.deb` packages built from the fork, installed on the PDM host |

You can run **GuestOS alone** (browser UI + machine API). PDM is optional glue
for operators who already live in Datacenter Manager.

```text
Operators ──HTTPS──► PDM (optional)
                       │  guestos.cfg (base-url, api-token, launch-secret)
                       ▼
                   GuestOS :443 (Caddy → web)
                       │
                       ▼
                   Proxmox API :8006  (clone / guest-agent / power)
```

## Lab agent access vs production (important)

During lab bring-up, an automation agent may have had **broad SSH / rebuild
permissions** on `guestos-lab` and `pdm-lab`. That is a **dev convenience**, not
the production security model.

In production:

| Who | Needs |
|-----|--------|
| **Deployer** (once / after upgrades) | Root or sudo on the GuestOS VM; on PDM host only if installing the fork packages + writing `guestos.cfg` |
| **GuestOS runtime** | Proxmox API user (clone, config, start/stop, guest agent) — **not** Proxmox root on every node unless you choose that. Details: [Proxmox privileges](#proxmox-privileges) |
| **PDM service** | Read `guestos.cfg` (`www-data`); outbound HTTPS to GuestOS |
| **Day-to-day operators** | PDM login **or** GuestOS UI login — **no** SSH, **no** API tokens, **no** Proxmox passwords |

Secrets stay on servers (`GuestOS .env`, PDM `guestos.cfg`). They are never
shipped in the PDM UI wasm and are never needed by end users.

---

## 1. Prerequisites

### Hosts

- **GuestOS host:** Linux with Docker Engine + Compose v2 (amd64 recommended for
  Proxmox utility VMs), **or** Podman with a Compose-compatible CLI
  (`podman compose` / equivalent). Open **TCP 443** to operators and to the PDM host.
- **Proxmox:** API reachable from the GuestOS **worker** (usually `:8006`).
- **PDM host** *(optional):* existing Proxmox Datacenter Manager install where
  you will replace upstream packages with the GuestOS fork builds.

### Packages / tools (GuestOS host)

Install these **before** `git clone` / Compose. The quick-start assumes they
are already on `PATH`:

| Tool | Why |
|------|-----|
| **`git`** | Clone the repository (or skip if you unpack a release tarball instead) |
| **`curl`** | Health / version checks after bring-up (`/api/health`, `/api/version`) |
| **Docker Engine + Compose v2** *or* **Podman** + a **Compose v2-compatible** provider | Run `web` / `worker` / `redis` / `caddy` |
| **`python3`** *(optional on host)* | Generating secrets below; not required inside Compose containers |

**Compose must be v2** (the `docker compose` / `podman compose` plugin style).
GuestOS’s `docker-compose.yml` uses Compose Spec features such as
`depends_on: … condition: service_healthy`. Do **not** use the old Python
package **`docker-compose` 1.x** (`docker-compose==1.29.x`): on Python 3.12+ it
fails with `ModuleNotFoundError: No module named 'distutils'`, and it is not a
supported deploy path for this project.

Example on Debian/Ubuntu-style hosts (adjust for your distro):

```bash
sudo apt update
sudo apt install -y git curl ca-certificates

# Option A — Docker Engine + Compose v2 plugin
# sudo apt install -y docker.io docker-compose-v2
# Confirm: docker compose version   # should print "Docker Compose version v2.…"

# Option B — Podman (do NOT rely on the broken Compose v1 shim alone)
# sudo apt install -y podman podman-docker docker-compose-v2
#   # or: sudo apt install -y podman podman-compose
# Prefer:  podman compose version   # or: podman-compose --version
# Avoid:   a `docker compose` that shells out to /usr/bin/docker-compose 1.29.x
```

Confirm before continuing:

```bash
git --version
curl --version | head -1
docker compose version 2>/dev/null || podman compose version 2>/dev/null || podman-compose --version
```

If `docker compose` prints *“Executing external compose provider …
/usr/bin/docker-compose”* and then crashes on `distutils`, you still have
**Compose v1**. Fix by installing **`docker-compose-v2`** or **`podman-compose`**,
then either:

```bash
# Prefer calling Podman directly
podman compose up -d --build
# or
podman-compose up -d --build
```

or ensure `docker compose version` reports **v2.x** (not `docker-compose` 1.29).
Optional: `touch /etc/containers/nodocker` to silence the podman-docker banner.

### Windows golden image

Full checklist: **[WINDOWS_TEMPLATE.md](WINDOWS_TEMPLATE.md)** (guest agent,
ostype, convert-to-template, Server disk tags, what Sysprep does on the
clone).

Short form:

- Converted to a Proxmox **template**.
- QEMU **Guest Agent** installed, enabled in Options, and working.
- `ostype` is a Windows type (`win10`, `win11`, …).
- Prefer a clean image (few extra local users).

Validated: Windows Server 2019 (Standard Evaluation), Windows Server 2022
(Standard VL), Windows 11 — full edition matrix and community asks:
[VALIDATED_MATRIX.md](VALIDATED_MATRIX.md). See
[AD_VALIDATION.md](AD_VALIDATION.md) for domain join.

### Credentials to prepare

Generate once and keep offline / in your secret store:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'  # SECRET_KEY
python3 -c 'import secrets; print(secrets.token_hex(32))'  # GUESTOS_LAUNCH_SECRET
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'  # GUESTOS_API_TOKEN
```

GuestOS authenticates to PVE with `PROXMOX_USER` + `PROXMOX_PASSWORD` (see
`.env.example`). Prefer a dedicated least-privilege user over `root@pam`.
See **Proxmox privileges** below.

### Proxmox privileges

GuestOS is an automation sidecar. Privilege planning has three layers:

| Layer | Who | Needs |
|-------|-----|--------|
| **GuestOS Linux host** | Deployer / runtime | Docker Compose; outbound reachability to Proxmox API `:8006` (and AD DNS if joining domains). No day-to-day SSH into every PVE node. |
| **Proxmox API user** | Stored only in GuestOS `.env` / `PVE_REMOTES_JSON` | Rights to clone templates, configure and power the **new** VM, and drive the QEMU guest agent (see below). |
| **Operators** | Browser / PDM | GuestOS UI login or PDM login only — **no** PVE password, **no** API tokens. |

Optional **AD join** credentials (`DOMAIN_PROFILES_JSON`) are separate from PVE
ACLs: that account only needs rights to join computers to the domain (and OU if
you set `domain_ou`).

#### What the API user does

| Area | GuestOS actions |
|------|-----------------|
| Inventory / read | List VMs/templates, read QEMU config, list node bridges, storage used% |
| Allocate / clone | Cluster `nextid`, clone template → new VM |
| Configure clone | Cores/RAM, NICs/VLAN/bridge, tags, rename (`failed-…`), optional disk attach/resize |
| Power | Start / stop |
| Guest agent | File write (unattend / `setup.ps1`), guest command exec, fsinfo / network checks |
| Optional cleanup | Delete VM (lab smoke / `--cleanup` only — not used for normal failed jobs) |

#### Recommended privileges (PVE 8.x-style names)

Create a role (e.g. `GuestOS`) and assign it to `guestos@pve` on a **limited
path**: a pool that holds your Windows golden templates and where clones land
(tighter), or `/` for a simple lab.

On that path, typically include:

- `VM.Audit` / `VM.Monitor` — read config and status  
- `VM.Clone` — clone from template  
- `VM.Allocate` — create (and delete) VMs  
- `VM.Config.Options`, `VM.Config.CPU`, `VM.Config.Memory`, `VM.Config.Network`,
  `VM.Config.Disk` — name, tags, hardware, NICs, disks  
- `VM.PowerMgmt` — start / stop  
- **Guest agent** (required for Sysprep customize):  
  `VM.GuestAgent.Audit`, `VM.GuestAgent.FileRead`, `VM.GuestAgent.FileWrite`,
  plus the privilege your PVE build exposes for **guest command execution**
  (without exec/file-write, jobs fail after clone)  
- `Datastore.Audit` — storage % safeguards  
- `Datastore.AllocateSpace` — if you use Configure disks (`manage_disks` on
  Server templates)  
- Enough node/network visibility to list bridges (often `Sys.Audit` on the node,
  or equivalent SDN/bridge ACL if you use those)

Exact labels can vary slightly by Proxmox version; map them in
**Datacenter → Permissions → Roles**. If clone works but Sysprep never starts,
the usual gap is **guest agent file-write / exec**.

#### What you do *not* need

- Full Datacenter Administrator / `root@pam` for normal operation  
- Host SSH to hypervisors for day-to-day customize jobs  
- Giving operators the Proxmox API password (keep it on the GuestOS host only)

---

## 2. Install GuestOS (required)

### 2.1 Place the code

```bash
# Example path — any durable directory is fine
sudo mkdir -p /opt/proxmox-guestos-customization
sudo chown "$USER":"$USER" /opt/proxmox-guestos-customization
git clone https://github.com/RobertLukan/proxmox-guestos-customization.git \
  /opt/proxmox-guestos-customization
cd /opt/proxmox-guestos-customization
```

(Air-gapped: copy a release tarball **plus** pre-pulled **`linux/amd64`** images via
`docker pull --platform linux/amd64` → `docker save` / `docker load` — see README
**Platform notes**. Use `docker-compose.yml` + `docker-compose.ghcr.yml` with
`--no-build` and `DOCKER_DEFAULT_PLATFORM=linux/amd64`.)

### 2.2 Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Minimum production-oriented settings:

| Variable | Production expectation |
|----------|------------------------|
| `SECRET_KEY` | Required; random |
| `PROXMOX_HOST` / `USER` / `PASSWORD` | Real cluster API (user + password) |
| `PROXMOX_VERIFY_SSL` | **`True`** with a trusted PVE cert (or install your CA into the containers) |
| `PRIMARY_BRIDGE` | Bridge **or SDN VNet name** clones should land on (defaults to `vmbr0` if unset) |
| `BEHIND_REVERSE_PROXY` | **`True`** (Compose Caddy, or your own TLS proxy) |
| `GUESTOS_TLS_HOST` | Public DNS name operators type in the browser |
| `GUESTOS_API_TOKEN` | Long random; shared only with PDM `guestos.cfg` |
| `GUESTOS_LAUNCH_SECRET` | Long random; **must match** PDM `launch-secret` |
| `GUESTOS_CORS_ORIGINS` | Empty (default) disables CORS; set PDM origin(s) or `*` only for lab direct browser calls |
| `DOMAIN_PROFILES_JSON` | Real AD profiles if you join domains (no placeholders) |
| `PVE_REMOTES_JSON` | Optional named remotes for multi-cluster / PDM `remote_id` |

### 2.3 TLS certificates

**Production:** put a cert trusted by browsers **and** the PDM host under
`deploy/caddy/certs/` (or terminate TLS on your own reverse proxy and point it
at `127.0.0.1:5001`). See [TLS_PRODUCTION.md](TLS_PRODUCTION.md).

**Lab only:** `./deploy/caddy/gen-selfsigned.sh "$GUESTOS_TLS_HOST"`.

### 2.4 Start Compose

Compose **v2** required (see [Packages / tools](#packages--tools-guestos-host)).

**Preferred — pull from GHCR** (image published on each `v*` release):

```bash
export GUESTOS_VERSION=2.6.7   # pin; both 2.6.7 and v2.6.7 tags exist on GHCR
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up -d --no-build
# Podman: podman compose -f docker-compose.yml -f docker-compose.ghcr.yml …
```

Image: `ghcr.io/robertlukan/proxmox-guestos-customization` —
[package page](https://github.com/RobertLukan/proxmox-guestos-customization/pkgs/container/proxmox-guestos-customization).
If pulls fail with 403/404 on a public repo, open the package → **Package settings** →
set visibility to **Public** (needed once after the first publish).

**Alternative — build locally:**

```bash
docker compose up -d --build
# Podman (if docker compose still points at Compose v1):
# podman compose up -d --build
#   or: podman-compose up -d --build
```

Then verify:

```bash
curl -fsS "https://${GUESTOS_TLS_HOST}/api/health"
curl -fsS "https://${GUESTOS_TLS_HOST}/api/version"
# Lab self-signed (gen-selfsigned.sh): skip TLS verify with -k / --insecure
# curl -fsSk "https://${GUESTOS_TLS_HOST}/api/health"
# curl -fsSk "https://${GUESTOS_TLS_HOST}/api/version"
```

Services: **Caddy** (`:443`), **web** (loopback `:5001`), **worker** (clone
queue), **verify-worker** (verify queue), **Redis**. SQLite persists in the
`app-instance` volume.

Optional after first login: create **Customization Specs** (UI **Specs** or
`/api/specs`) for reusable non-secret defaults; apply via `spec_id` on single
or bulk start. See [BULK_PROVISIONING.md](BULK_PROVISIONING.md) and
[openapi.yaml](openapi.yaml).

### 2.5 First login (standalone UI)

1. Open `https://<GUESTOS_TLS_HOST>/`.
2. Log in with the default password **`changeme`**.
3. **Change Password** immediately.
4. Optionally run one **Clone + Sysprep** from a Windows template to validate.

Machine API (no browser):

```bash
export GUESTOS_URL=https://guestos.example.com
export GUESTOS_API_TOKEN=…   # from .env
python3 scripts/pdm_api_smoke.py --base-url "$GUESTOS_URL" --token "$GUESTOS_API_TOKEN"
```

Full template→Sysprep→cleanup is **destructive**; run only after major changes
(see [PDM_INTEGRATION.md](PDM_INTEGRATION.md) — Automated smoke tests).

---

## 3. Install PDM GuestOS fork (optional)

Skip this section if operators will only use the GuestOS web UI / API.

### 3.1 Build packages (on a Debian / PDM-capable build host)

Source: [proxmox-datacenter-manager-guestos](https://github.com/RobertLukan/proxmox-datacenter-manager-guestos)
branch `guestos-sysprep`. Needs Proxmox UI/server build dependencies (devel
repos, `proxmox-wasm-builder`, Rust crates, etc.) — typically the **PDM host
itself** or a matching Debian build VM.

```bash
git clone … && cd proxmox-datacenter-manager-guestos
git checkout guestos-sysprep
git submodule update --init --recursive   # ui/pwt-assets

make deb          # server package (includes GuestOS proxy)
cd ui && make deb # UI package (Customize + GuestOS tab)
```

Versioning today is hand-built (`1.1.3+guestos.N` UI; bump server to
`1.1.7+guestos.N` when you change the proxy). There is no public APT feed yet —
copy the `.deb` files to the PDM host.

### 3.2 Install on the PDM host

```bash
# Stop briefly if required by your packaging; then:
sudo apt install ./proxmox-datacenter-manager_*.deb \
                 ./proxmox-datacenter-manager-ui_*.deb

# Prevent unattended upgrade from replacing the fork with upstream:
sudo apt-mark hold proxmox-datacenter-manager proxmox-datacenter-manager-ui
```

### 3.3 Configure `guestos.cfg`

```bash
sudo install -o www-data -g www-data -m 640 /dev/null \
  /etc/proxmox-datacenter-manager/guestos.cfg
sudoeditor /etc/proxmox-datacenter-manager/guestos.cfg
```

Format is Proxmox simple-config (`key: value`). Example:
[guestos.cfg.example](https://github.com/RobertLukan/proxmox-datacenter-manager-guestos/blob/guestos-sysprep/docs/guestos.cfg.example)

```text
base-url: https://guestos.example.com
api-token: <same as GUESTOS_API_TOKEN>
launch-secret: <same as GUESTOS_LAUNCH_SECRET>
launch-ttl: 300
verify-tls: true
```

```bash
sudo systemctl restart proxmox-datacenter-manager.service
```

### 3.4 Operator check

1. Log into PDM with a normal admin account (no GuestOS secrets).
2. Open remote → **Windows template** → **Customize (GuestOS)** → GuestOS wizard
   opens already authenticated.
3. Open the **GuestOS** tab → task list loads (PDM proxies GuestOS with the
   server-side token).

---

## 4. Network / firewall sketch

| From | To | Port | Why |
|------|-----|------|-----|
| Operators / PDM | GuestOS | **443/tcp** | UI, `/launch`, API |
| GuestOS worker | Each PVE API | **8006/tcp** (typical) | Clone, config, guest agent |
| PDM | GuestOS | **443/tcp** | Launch signing + task proxy |
| GuestOS host localhost | GuestOS web | **5001/tcp** | Debug only; not public |

Do **not** expose Redis or `:5001` on a public interface.

---

## 5. Day-2 operations

| Task | How |
|------|-----|
| Upgrade GuestOS | Prefer `export GUESTOS_VERSION=…` → `docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull && … up -d --no-build`. Or `git pull` → `docker compose up -d --build`. |
| Upgrade PDM fork | Rebuild `.deb`s → install → keep `apt-mark hold` |
| Rotate secrets | Change `.env` + matching `guestos.cfg` → restart Compose + PDM service |
| After major workflow changes | Run lab/prod smoke once (API script and/or one PDM Customize) — not a daily cron |
| Backup | Compose volume `app-instance` (SQLite task history) + `.env` + `guestos.cfg` + TLS material |

---

## 6. Production checklist (short)

- [ ] GuestOS host has `git`, `curl`, and Docker/Podman + Compose v2
- [ ] (If using GHCR pull) package visibility is Public; `GUESTOS_VERSION` pinned
- [ ] Trusted TLS on GuestOS; `PROXMOX_VERIFY_SSL=true` (and remotes)
- [ ] Default GuestOS UI password changed
- [ ] Dedicated Proxmox API principal (least privilege) — see [Proxmox privileges](#proxmox-privileges)
- [ ] `GUESTOS_API_TOKEN` / `GUESTOS_LAUNCH_SECRET` only on GuestOS + PDM cfg
- [ ] PDM packages held; `verify-tls: true`
- [ ] Real `DOMAIN_PROFILES_JSON` if joining AD
- [ ] One successful Customize (or API workflow) on a disposable template clone

More detail: [WINDOWS_TEMPLATE.md](WINDOWS_TEMPLATE.md), [BULK_PROVISIONING.md](BULK_PROVISIONING.md), [TLS_PRODUCTION.md](TLS_PRODUCTION.md),
[PDM_INTEGRATION.md](PDM_INTEGRATION.md),
PDM fork [README.GUESTOS.md](https://github.com/RobertLukan/proxmox-datacenter-manager-guestos/blob/guestos-sysprep/README.GUESTOS.md).
