# Proxmox GuestOS Utility

**Current release: [2.4.0](VERSION)** — community project for **Sysprep guest OS customization** of Windows VMs in Proxmox VE (VMware-style: golden image template → clone → customize), including **bulk Win11 desktop provisioning** with safeguards.

> **Not an official Proxmox product.** Lab-validated on Windows Server 2019 and Windows 11. Support is community / GitHub issues only — [open an issue](https://github.com/RobertLukan/proxmox-guestos-customization/issues).

GuestOS is **Sysprep-only** (WinRM removed in 2.0 — see [docs/MIGRATE_2.0.md](docs/MIGRATE_2.0.md)). In-place Sysprep of existing/production VMs is **disabled**.

| Path | Status | How it works |
|------|--------|----------------|
| **Sysprep customize** | **Supported** (only path) | Template → clone → guest-agent writes unattend + `setup.ps1` → `sysprep /generalize` → verify |
| **Bulk Win11 batch** | **Supported** (Win11 tagged templates) | CSV/API batch → one task per desktop → shared network/DNS defaults |

## Start here

1. **Deploy GuestOS alone** (recommended first step): Docker Compose + TLS — follow [docs/INSTALL.md](docs/INSTALL.md) §1–2.
2. Prepare a **Windows golden image** template — [docs/WINDOWS_TEMPLATE.md](docs/WINDOWS_TEMPLATE.md). Tag templates (`windows11` / `windowsserver2019`) so caps and UI modes classify correctly.
3. Open the UI, change the default password, run one **Clone + Sysprep** smoke.
4. *(Optional)* For VDI fleets, use **Bulk desktops** on a Win11 template — [docs/BULK_PROVISIONING.md](docs/BULK_PROVISIONING.md).
5. *(Optional / advanced)* Wire **Proxmox Datacenter Manager** with the AGPL GuestOS fork — [docs/INSTALL.md](docs/INSTALL.md) §3 and [PDM integration](docs/PDM_INTEGRATION.md).

### Screenshots

| Template select | Sysprep wizard | Bulk Win11 | Jobs / batches | PDM Customize |
|-----------------|----------------|------------|----------------|---------------|
| ![Initial](screenshots/Initial.png) | ![Clone](screenshots/Clone.png) | ![Bulk](screenshots/Bulk.png) | ![Progress](screenshots/Progress.png) | ![PDM](screenshots/PDM-2.jpg) |

## What’s stable vs advanced

| Area | Notes |
|------|--------|
| Clone + Sysprep (hostname, static/DHCP, optional AD join) | **Stable** — validated on Server 2019 and Win11 in lab |
| Bulk Win11 batch (CSV / API) | **Stable for lab** — max 10/batch, 20/day; Win11 only |
| Configure disks (OS / data / pagefile) | **Server 2019 only** (name/tag); hidden for Win11 |
| Provisioning safeguards (cores/RAM/disk/storage) | **Stable** — no in-app override; use PVE for exceptions |
| PDM Customize + GuestOS tab | **Optional** AGPL fork; build/install `.deb`s yourself (no public APT feed yet) |

## Features

- Clone Windows templates and run **Clone + Sysprep (customize)** in one job.
- Sysprep applies hostname + timezone, **static** or **DHCP** networking, optional AD join (credentials server-side).
- **Bulk Win11 provisioning:** CSV rows (`hostname,ip/prefix[,vlan]`), shared gateway/DNS, batch monitor, idempotent API.
- **Safeguards:** batch/day/inflight limits; Win11 vs Server cores/RAM/disk caps; storage warn@65% / block@80%; live CSV duplicate hostname/IP checks; clone VMID collision retries.
- **Family-aware UI:** bulk mode for `windows11` only; Configure disks for Server templates only.
- Background tasks with Celery + Redis (clone + verify queues); web UI + machine API for PDM.

## Workflow overview

1. Pick a **Windows Proxmox template** (`ostype` `win10` / `win11` / …) tagged for family.
2. **Single:** Clone + Sysprep → guest agent writes unattend + `setup.ps1` → `sysprep /generalize /oobe /shutdown`.
3. **Bulk (Win11):** submit shared settings + CSV desktops → one Celery task per row → Jobs filtered by `batch_id`.
4. After OOBE, **FirstLogonCommands** runs `setup.ps1` (network, cleanup, optional domain join), then logs off to the login screen.
5. GuestOS verifies setup markers / hostname / expected static IP via the guest agent.

From **PDM** (optional): template → **Customize (GuestOS)** → signed `/launch` → wizard.

## Prerequisites (short)

- Proxmox VE API reachable from the GuestOS **worker** (clone, config, start/stop, guest agent).
- Windows **template** with working QEMU Guest Agent — details: [docs/WINDOWS_TEMPLATE.md](docs/WINDOWS_TEMPLATE.md).
- Linux host with Docker Engine + Compose v2 (recommended), or Python 3.12+ for local/venv.

## Installation

**Full guide (GuestOS + optional PDM fork):** [docs/INSTALL.md](docs/INSTALL.md).

| Doc | Topic |
|-----|--------|
| [docs/WINDOWS_TEMPLATE.md](docs/WINDOWS_TEMPLATE.md) | Golden-image checklist + tags |
| [docs/BULK_PROVISIONING.md](docs/BULK_PROVISIONING.md) | Batch provisioning, quotas, caps |
| [docs/TLS_PRODUCTION.md](docs/TLS_PRODUCTION.md) | Trusted TLS / `PROXMOX_VERIFY_SSL` |
| [docs/PDM_INTEGRATION.md](docs/PDM_INTEGRATION.md) | Machine API + PDM + lab notes |
| [docs/openapi.yaml](docs/openapi.yaml) | Start / bulk / limits API schema |
| [docs/FAILURE_RUNBOOK.md](docs/FAILURE_RUNBOOK.md) | Failure triage + limit saturation |
| [docs/MIGRATE_2.0.md](docs/MIGRATE_2.0.md) | 1.x → 2.0 (WinRM removed) |

### Docker Compose (recommended)

```bash
git clone https://github.com/RobertLukan/proxmox-guestos-customization/
cd proxmox-guestos-customization
cp .env.example .env
# Edit .env: SECRET_KEY, PROXMOX_*, PRIMARY_BRIDGE, BEHIND_REVERSE_PROXY=True,
# GUESTOS_TLS_HOST=<your-dns-or-ip>, optional GUESTOS_API_TOKEN / GUESTOS_LAUNCH_SECRET
chmod +x deploy/caddy/gen-selfsigned.sh
./deploy/caddy/gen-selfsigned.sh "$GUESTOS_TLS_HOST"   # lab self-signed; use real certs in prod
docker compose up -d --build
curl -fsS "https://${GUESTOS_TLS_HOST}/api/version"
```

- **HTTPS:** `https://<GUESTOS_TLS_HOST>/` (Caddy `:443`)
- **HTTP loopback debug:** `http://127.0.0.1:5001` on the GuestOS host only

Default UI password: `changeme` — change it immediately via **Change Password**.

### Quick local / venv setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # SECRET_KEY is required
python3 init_db.py
```

### Offline / Compose V1

See `docker-compose.offline.yml`. Prefer `--platform linux/amd64` for Proxmox utility VMs. If only `docker-compose` (V1) exists: `docker-compose up -d`.

## Configuration

See [`.env.example`](.env.example). Key variables:

| Variable | Purpose |
|----------|---------|
| `PROXMOX_HOST` / `USER` / `PASSWORD` | Proxmox API (user + password; dedicated least-privilege user preferred) |
| `PROXMOX_VERIFY_SSL` | TLS verify for Proxmox API (use `True` in production) |
| `GUESTOS_API_TOKEN` / `API_TOKENS` | Machine API auth for PDM / integrators |
| `PVE_REMOTES_JSON` | Optional named remotes (`remote_id` in sysprep JSON) |
| `GUESTOS_LAUNCH_SECRET` | HMAC secret for PDM `/launch` (must match PDM `guestos.cfg`) |
| `GUESTOS_TLS_HOST` | Hostname/IP for Caddy TLS |
| `PRIMARY_BRIDGE` | Bridge for the cloned VM NIC |
| `SECRET_KEY` | **Required** — sessions + CSRF |
| `BEHIND_REVERSE_PROXY` | ProxyFix + Secure cookies (set `True` with Compose Caddy) |
| `DOMAIN_PROFILES_JSON` | Named AD/network profiles |
| `BULK_MAX_ITEMS` / `PROVISION_MAX_PER_DAY` | Batch size (default 10) and daily task quota (20) |
| `BULK_MAX_CONCURRENT_GLOBAL` | Inflight clone/sysprep tasks (default 10) |
| `WIN11_*` / `SERVER_*` | Cores / RAM / disk ceilings per template family |
| `STORAGE_WARN_PCT` / `STORAGE_BLOCK_PCT` | Storage used% warn (65) / block (80) |

## Usage

1. Open `https://<GUESTOS_TLS_HOST>/`.
2. Log in; change the default password.
3. Run **Clone + Sysprep** from a Windows template — or **Bulk desktops** on a Win11 template.

Smoke helpers (after `.env` is set):

```bash
venv/bin/python scripts/smoke_check.py
python3 scripts/pdm_api_smoke.py --base-url "https://${GUESTOS_TLS_HOST}" --token "$GUESTOS_API_TOKEN"
```

## Development / Testing

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Security notes

- Run behind TLS (`BEHIND_REVERSE_PROXY=True`). Production: [docs/TLS_PRODUCTION.md](docs/TLS_PRODUCTION.md).
- Keep `GUESTOS_LAUNCH_SECRET` / `GUESTOS_API_TOKEN` on the GuestOS host and in PDM `guestos.cfg` only (not in UI wasm).
- Do not re-enable in-place Sysprep on arbitrary VMs.
- There is **no** superadmin bypass for provisioning caps — size exceptions in Proxmox VE.

## License

- **This repository (GuestOS app):** [MIT](LICENSE).
- **Optional PDM UI/server fork** ([proxmox-datacenter-manager-guestos](https://github.com/RobertLukan/proxmox-datacenter-manager-guestos)): **AGPL-3** — see that repo’s `README.GUESTOS.md`.
