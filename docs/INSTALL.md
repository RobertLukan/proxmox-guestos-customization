# Installing GuestOS (+ optional PDM fork)

This document is the **production-oriented** install guide. Lab IPs and
self-signed shortcuts live in [PDM_INTEGRATION.md](PDM_INTEGRATION.md)
(lab notes) and [TLS_PRODUCTION.md](TLS_PRODUCTION.md).

## What “the product” is

| Piece | Role | How you install it |
|-------|------|--------------------|
| **GuestOS** | Sidecar that clones Windows templates and runs Sysprep customize | Docker Compose on a Linux host that can reach Proxmox APIs |
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
| **GuestOS runtime** | Proxmox API user (clone, config, start/stop, guest agent) — **not** Proxmox root on every node unless you choose that |
| **PDM service** | Read `guestos.cfg` (`www-data`); outbound HTTPS to GuestOS |
| **Day-to-day operators** | PDM login **or** GuestOS UI login — **no** SSH, **no** API tokens, **no** Proxmox passwords |

Secrets stay on servers (`GuestOS .env`, PDM `guestos.cfg`). They are never
shipped in the PDM UI wasm and are never needed by end users.

---

## 1. Prerequisites

### Hosts

- **GuestOS host:** Linux with Docker Engine + Compose v2 (amd64 recommended for
  Proxmox utility VMs). Open **TCP 443** to operators and to the PDM host.
- **Proxmox:** API reachable from the GuestOS **worker** (usually `:8006`).
- **PDM host** *(optional):* existing Proxmox Datacenter Manager install where
  you will replace upstream packages with the GuestOS fork builds.

### Windows golden image

- Converted to a Proxmox **template**.
- QEMU **Guest Agent** installed and working.
- `ostype` is a Windows type (`win10`, `win11`, …).
- Prefer a clean image (few extra local users).

Validated: Windows Server 2019, Windows 11. See [AD_VALIDATION.md](AD_VALIDATION.md)
if you need domain join.

### Credentials to prepare

Generate once and keep offline / in your secret store:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'  # SECRET_KEY
python3 -c 'import secrets; print(secrets.token_hex(32))'  # GUESTOS_LAUNCH_SECRET
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'  # GUESTOS_API_TOKEN
```

Create a dedicated Proxmox API user/token with rights to clone templates,
configure NICs, start/stop VMs, and use the guest agent. Prefer a token over
embedding `root@pam` passwords.

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

(Air-gapped: copy a release tarball or pre-built amd64 images; see
`docker-compose.offline.yml`.)

### 2.2 Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Minimum production-oriented settings:

| Variable | Production expectation |
|----------|------------------------|
| `SECRET_KEY` | Required; random |
| `PROXMOX_HOST` / `USER` / `PASSWORD` (or API token fields you use) | Real cluster API |
| `PROXMOX_VERIFY_SSL` | **`True`** with a trusted PVE cert (or install your CA into the containers) |
| `PRIMARY_BRIDGE` | Bridge clones should land on (e.g. `vmbr0`) |
| `BEHIND_REVERSE_PROXY` | **`True`** (Compose Caddy, or your own TLS proxy) |
| `GUESTOS_TLS_HOST` | Public DNS name operators type in the browser |
| `GUESTOS_API_TOKEN` | Long random; shared only with PDM `guestos.cfg` |
| `GUESTOS_LAUNCH_SECRET` | Long random; **must match** PDM `launch-secret` |
| `GUESTOS_CORS_ORIGINS` | Restrict to PDM origin(s), or leave unset if browsers only use PDM proxy |
| `DOMAIN_PROFILES_JSON` | Real AD profiles if you join domains (no placeholders) |
| `PVE_REMOTES_JSON` | Optional named remotes for multi-cluster / PDM `remote_id` |

Do **not** enable WinRM (`GUESTOS_ENABLE_WINRM`) for new production designs.

### 2.3 TLS certificates

**Production:** put a cert trusted by browsers **and** the PDM host under
`deploy/caddy/certs/` (or terminate TLS on your own reverse proxy and point it
at `127.0.0.1:5001`). See [TLS_PRODUCTION.md](TLS_PRODUCTION.md).

**Lab only:** `./deploy/caddy/gen-selfsigned.sh "$GUESTOS_TLS_HOST"`.

### 2.4 Start Compose

```bash
docker compose up -d --build
curl -fsS "https://${GUESTOS_TLS_HOST}/api/health"
curl -fsS "https://${GUESTOS_TLS_HOST}/api/version"
```

Services: **Caddy** (`:443`), **web** (loopback `:5001`), **worker**, **Redis**.
SQLite persists in the `app-instance` volume.

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
| Upgrade GuestOS | `git pull` (or new release tarball) → `docker compose up -d --build` |
| Upgrade PDM fork | Rebuild `.deb`s → install → keep `apt-mark hold` |
| Rotate secrets | Change `.env` + matching `guestos.cfg` → restart Compose + PDM service |
| After major workflow changes | Run lab/prod smoke once (API script and/or one PDM Customize) — not a daily cron |
| Backup | Compose volume `app-instance` (SQLite task history) + `.env` + `guestos.cfg` + TLS material |

---

## 6. Production checklist (short)

- [ ] Trusted TLS on GuestOS; `PROXMOX_VERIFY_SSL=true` (and remotes)
- [ ] Default GuestOS UI password changed
- [ ] Dedicated Proxmox API principal (least privilege)
- [ ] `GUESTOS_API_TOKEN` / `GUESTOS_LAUNCH_SECRET` only on GuestOS + PDM cfg
- [ ] PDM packages held; `verify-tls: true`
- [ ] Real `DOMAIN_PROFILES_JSON` if joining AD
- [ ] One successful Customize (or API workflow) on a disposable template clone

More detail: [TLS_PRODUCTION.md](TLS_PRODUCTION.md), [PDM_INTEGRATION.md](PDM_INTEGRATION.md),
PDM fork [README.GUESTOS.md](https://github.com/RobertLukan/proxmox-datacenter-manager-guestos/blob/guestos-sysprep/README.GUESTOS.md).
