# Proxmox GuestOS Utility

A Flask web application to automate cloning and **Sysprep guest OS customization** of Windows VMs in Proxmox VE (VMware-style: golden image template → clone → customize).

GuestOS **2.0** is **Sysprep-only**. The legacy WinRM reconfigure path (temp NIC,
WinRM ports, "Existing VMs") has been removed — see
[docs/MIGRATE_2.0.md](docs/MIGRATE_2.0.md) if you are upgrading from 1.x.

| Path | Status | How it works |
|------|--------|----------------|
| **Sysprep customize** | **Supported** (only path) | Template → clone → guest-agent writes unattend + `setup.ps1` → `sysprep /generalize` → verify |

In-place Sysprep of existing/production VMs is **disabled** (protects live guests).

## Recent Improvements

-   **Template-only customize:** PDM and GuestOS only run clone+Sysprep from Windows Proxmox templates (VMware-style golden image).
-   **Static IP reliability (Server 2019 / Win11):** `setup.ps1` lives under `C:\ProgramData\GuestOS\` and is invoked by unattend **FirstLogonCommands** (Sysprep often removes `C:\Windows\Setup\Scripts`).
-   **Domain join via Sysprep:** validated on **Windows Server 2019** in lab (profile credentials + `setup.ps1`).
-   **TLS + launch token:** Compose includes Caddy on `:443`; PDM can open a short-lived HMAC `/launch` URL that creates a GuestOS session (no second password).
-   **Security hardening:** validated guest values, server-side domain credentials, CSRF, required `SECRET_KEY`.
-   **Packaging & CI:** `Dockerfile`, `docker-compose.yml`, offline compose, `pytest` + GitHub Actions.

## Features

-   Clone Windows templates and run **Clone + Sysprep (customize)** in one job.
-   Sysprep applies:
    -   Hostname + timezone via answer file
    -   **Static** or **DHCP** networking (IP, prefix, gateway, DNS)
    -   Optional Active Directory join (profile credentials server-side)
    -   Domain profiles optionally fill **DNS** and **VLAN**
-   Background tasks with Celery + Redis.
-   Web UI + machine API for PDM (`/start_sysprep_workflow`, `/api/tasks`, `/task_status/...`).

## Project Status

**Active development** — Sysprep customize is the only product path (2.0).

-   **Sysprep customization** (hostname + static/DHCP + optional AD join): validated on **Windows Server 2019** and **Windows 11** in lab.
-   **Domain join**: live join confirmed on Server 2019; keep real (non-placeholder) `DOMAIN_PROFILES_JSON` for production (see [docs/AD_VALIDATION.md](docs/AD_VALIDATION.md)).

## Workflow Overview

### Sysprep path (golden image — recommended)

1. Pick a **Windows Proxmox template** (ostype `win10` / `win11` / …).
2. **Clone + Sysprep (customize)** → clone, power on, wait for a stable QEMU guest agent, write:
    -   `C:\Windows\System32\Sysprep\unattended.xml`
    -   `C:\ProgramData\GuestOS\setup.ps1` (survives `/generalize`)
    -   optional `C:\Windows\Setup\Scripts\SetupComplete.cmd` (best-effort; often removed by Sysprep)
3. Runs `sysprep /generalize /oobe /shutdown` with the answer file.
4. After OOBE, **FirstLogonCommands** (AutoLogon once as Administrator) runs `setup.ps1` to apply network, clean local users, optional domain join.
5. GuestOS verifies hostname / expected static IP via the guest agent.

From **PDM**: open a **template** → **Customize (GuestOS)** → signed `/launch` deep-link (HTTPS) → wizard.

## Design Choices

### Why Sysprep + guest agent (customize)?

No WinRM or temp NIC for hostname/network. Uses the QEMU guest agent and native unattend / FirstLogonCommands. Network config runs from `setup.ps1` so virtio adapters can be selected by MAC. This matches the VMware guest-customization model and is what PDM drives.

## Prerequisites

### Proxmox VE

-   Working Proxmox VE cluster/host.
-   API user with privileges to clone, configure, start/stop VMs, and use the guest agent.

### Templates

GuestOS lists and accepts **Windows templates only**, based on Proxmox QEMU `ostype` (`win10`, `win11`, `win8`, …).

#### Sysprep golden image (supported)

-   Converted to a Proxmox **template**.
-   **QEMU Guest Agent** installed and working.
-   Prefer a clean image (few extra local users).

Validated guests: **Windows Server 2019**, **Windows 11**.

### Network

-   **Sysprep:** static or DHCP on the primary NIC; domain join needs DNS that can resolve the DC.

### Software

-   Python 3.12+ (or Docker).
-   Redis (bundled in Compose).
-   Reverse proxy with TLS (Compose **Caddy** service; set `BEHIND_REVERSE_PROXY=True`).

## Installation and Setup

**Full guide (lab vs production, GuestOS + optional PDM fork):**
[`docs/INSTALL.md`](docs/INSTALL.md).

TLS hardening: [`docs/TLS_PRODUCTION.md`](docs/TLS_PRODUCTION.md).  
PDM / machine API: [`docs/PDM_INTEGRATION.md`](docs/PDM_INTEGRATION.md).  
1.x → 2.0: [`docs/MIGRATE_2.0.md`](docs/MIGRATE_2.0.md).

### Quick local / venv setup

```bash
git clone https://github.com/RobertLukan/proxmox-guestos-customization/
cd proxmox-guestos-customization
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — SECRET_KEY is required
python3 init_db.py
```

Default login password: `changeme`. Change it immediately via **Change Password**.

## Running with Docker (recommended)

```bash
cp .env.example .env
# set SECRET_KEY, GUESTOS_LAUNCH_SECRET, BEHIND_REVERSE_PROXY=True for TLS lab/prod
chmod +x deploy/caddy/gen-selfsigned.sh
./deploy/caddy/gen-selfsigned.sh 192.168.123.197   # or your host IP/DNS
docker compose up -d --build
```

-   **HTTPS (public):** `https://<GUESTOS_TLS_HOST>/` (Caddy `:443`, self-signed in lab)
-   **HTTP loopback debug:** `http://127.0.0.1:5001` on the GuestOS host only

Compose starts **web**, **worker**, **Redis**, and **Caddy**. SQLite lives in the `app-instance` volume.

### Older hosts (Compose V1)

If `docker compose` is missing but `docker-compose` exists:

```bash
docker-compose up -d
```

### Offline / air-gapped deploy

See `docker-compose.offline.yml` (images only; add Caddy/certs separately if you need TLS offline). Prefer `--platform linux/amd64` when building for Proxmox utility VMs.

## Running manually

Terminal 1 (with Redis running locally):

```bash
source venv/bin/activate
# BEHIND_REVERSE_PROXY=False in .env for direct HTTP
python3 run.py
```

Terminal 2:

```bash
source venv/bin/activate
celery -A app.celery worker --loglevel=info
```

Production-style: Compose Caddy + `BEHIND_REVERSE_PROXY=True`, or your own TLS proxy in front of gunicorn.

## Configuration

See `.env.example`. Key variables:

| Variable | Purpose |
|----------|---------|
| `PROXMOX_HOST` / `USER` / `PASSWORD` | Proxmox API (default remote) |
| `PROXMOX_VERIFY_SSL` | TLS verify for Proxmox API (default `False`) |
| `GUESTOS_API_TOKEN` / `API_TOKENS` | Machine API auth for PDM sysprep start+poll |
| `PVE_REMOTES_JSON` | Optional named remotes (`remote_id` in sysprep JSON) |
| `GUESTOS_LAUNCH_SECRET` | HMAC secret for PDM `/launch` one-click session (match PDM UI bake-in) |
| `GUESTOS_LAUNCH_TTL` | Launch token lifetime seconds (default `300`) |
| `GUESTOS_TLS_HOST` | Hostname/IP for Caddy TLS (default lab IP) |
| `GUESTOS_PATH_PREFIX` | Optional subpath mount (leave empty for site-root HTTPS) |
| `PRIMARY_BRIDGE` | Bridge for the cloned VM's network interface |
| `SECRET_KEY` | **Required** — sessions + CSRF |
| `APP_VERSION` | Optional override of `VERSION` file |
| `BEHIND_REVERSE_PROXY` | ProxyFix + Secure cookies |
| `DATABASE_URL` | SQLAlchemy URL (default SQLite under `instance/`) |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Redis |
| `PORT` | Listen port (default `5001`) |
| `DOMAIN_PROFILES_JSON` | Named AD/network profiles |

## Lab helper scripts

```bash
venv/bin/python scripts/smoke_check.py
python3 scripts/pdm_api_smoke.py --base-url https://192.168.123.197 --token "$GUESTOS_API_TOKEN"
python3 scripts/ad_join_validate.py --check-dns
```

PDM notes: [docs/PDM_INTEGRATION.md](docs/PDM_INTEGRATION.md).  
AD join checklist: [docs/AD_VALIDATION.md](docs/AD_VALIDATION.md).

## Usage

1. Open **https://\<host\>/** (accept the lab self-signed cert once) or loopback HTTP for debug.
2. Log in (or arrive via PDM **Customize** launch link); change the default password.
3. Run **Clone + Sysprep** from a Windows template.

## Development / Testing

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Security notes

-   Run behind TLS (`BEHIND_REVERSE_PROXY=True`). For non-lab deploys see [`docs/TLS_PRODUCTION.md`](docs/TLS_PRODUCTION.md) (`PROXMOX_VERIFY_SSL`, trusted Caddy cert, PDM `verify-tls`).
-   Keep `GUESTOS_LAUNCH_SECRET` / `GUESTOS_API_TOKEN` on the GuestOS host and in PDM `guestos.cfg` only (not in UI wasm).
-   Do not re-enable in-place Sysprep on arbitrary VMs.
-   Sysprep + the QEMU guest agent keep guest management ports closed (no WinRM).

## License

See repository license file.
