# PDM / machine API integration (sysprep-only)

**Install / production deploy:** see [`INSTALL.md`](INSTALL.md) (GuestOS Compose +
optional PDM fork packages, privileges, firewall). This page is the API and
lab-integration reference.

**Upgrading GuestOS:** stay on the current **2.x** release line (`VERSION` /
GitHub Releases / GHCR tags). There is no supported 1.x upgrade path.

GuestOS is one app. **Sysprep customize** (template → clone → guest agent) is the
only supported customization path for PDM and standalone use.

Current app version is in [`VERSION`](../VERSION) and `GET /api/version`.

## Prerequisites

1. GuestOS Compose (web + worker + Redis + **Caddy TLS**) reachable from operator browsers / PDM.
2. Worker can reach the target Proxmox API(s).
3. Set in `.env` (see `.env.example`):

```bash
GUESTOS_API_TOKEN=generate-a-long-random-string
BEHIND_REVERSE_PROXY=True
GUESTOS_TLS_HOST=guestos.example.com   # your DNS or IP; see Lab notes for maintainer lab
GUESTOS_LAUNCH_SECRET=must-match-pdm-guestos-cfg
# optional multi-cluster:
# PVE_REMOTES_JSON={"lab":{"host":"pve.example","user":"api@pve","password":"...","verify_ssl":false}}
```

4. Generate lab certs and start (prefer GHCR pull; build is fine for lab forks):

```bash
./deploy/caddy/gen-selfsigned.sh "$GUESTOS_TLS_HOST"
export GUESTOS_VERSION=2.6.0   # pin a release
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up -d --no-build
# Dev / unreleased tree instead:
# docker compose up -d --build
```

## Endpoints for PDM

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/health` | none |
| GET | `/api/version` | none |
| GET | `/launch` | HMAC query (`exp`, `jti`, `sig`, `template_vmid`, `remote_id`) → session |
| POST | `/start_sysprep_workflow` | Bearer / `X-Api-Token` **or** session+CSRF |
| POST | `/start_sysprep_bulk_workflow` | Bearer / token **or** session+CSRF |
| GET | `/task_status/<task_id>` | Bearer / token **or** session |
| GET | `/api/tasks` / `/api/tasks/<id>` | Bearer / token **or** session |
| GET/POST | `/api/specs` | Bearer / token **or** session+CSRF |
| GET/PUT/DELETE | `/api/specs/<id>` | Bearer / token **or** session+CSRF |
| GET | `/api/provision_limits` | Bearer / token **or** session |

`POST /start_sysprep_existing_vm_task` returns **403** (disabled — protects production VMs).

### Optional disk reconcile (`manage_disks`)

Same opt-in pattern as domain join. Default **off**. When `manage_disks` is true:

```json
{
  "manage_disks": true,
  "disks": [
    { "role": "os", "grow_to_gb": 80 },
    { "role": "pagefile", "size_gb": 16, "drive_letter": "P", "ensure_pagefile": true },
    { "role": "data", "size_gb": 100, "drive_letter": "D", "label": "Data" }
  ]
}
```

New disks are created on the **boot disk’s storage** with the boot disk’s Proxmox
options (`aio`, `discard`, `cache`, …). Existing template disks are reused
(online/extend) instead of duplicated. Post-Sysprep verify checks volumes and
pagefile placement when requested.

**Policy:** disk reconcile runs only for **Windows Server** templates
(matched by template **name** or **tags**, e.g. `windowsserver2019|2022|2025`,
`server2022`, `ws2025`, `guestos-disk`). If `manage_disks` is set on a non-Server
template (e.g. Win11), the workflow **fails validation** with a clear error
(no silent disable). Disk customize is available via the machine API / smoke
scripts; it is **not** exposed in the PDM Customize UI.

## Job history (PDM GuestOS tab)

GuestOS keeps a SQLite task ledger. PDM’s **GuestOS** tab calls the **PDM server
proxy** (`GET /api2/extjs/guestos/tasks`), which uses `guestos.cfg` (`base-url`,
`api-token`) to fetch:

```http
GET /api/tasks?kind=customization&limit=100&offset=0
GET /api/tasks?kind=customization&remote_id=<pdm-remote>&limit=100&offset=0
Authorization: Bearer <GUESTOS_API_TOKEN>
```

Browsers no longer need the GuestOS API token. Configure PDM with
`/etc/proxmox-datacenter-manager/guestos.cfg` (see the PDM fork
`docs/guestos.cfg.example`).

Response shape from GuestOS:
`{ "tasks": [ … ], "count": N, "total": T, "limit": L, "offset": O }`.
Each task includes `id`, `name`, `status`, `progress`, `message`, `timestamp`,
`updated_at`, `remote_id`, `template_vmid`, `hostname`, `result_vmid`, ….
Also: `GET /api/tasks/<id>` and HTML `/jobs`.

## One-click launch (browser)

PDM **Customize (GuestOS)** (Windows templates only) asks PDM
`POST /api2/extjs/guestos/launch`, then opens the returned URL:

`https://<host>/launch?template_vmid=…&remote_id=…&exp=…&jti=…&sig=…`

GuestOS verifies HMAC (`GUESTOS_LAUNCH_SECRET`), creates a session, redirects to `/sysprep_form`. Tokens expire (`GUESTOS_LAUNCH_TTL`, default 300s) and are single-use.
## Smoke check from the PDM host

```bash
export GUESTOS_URL=https://guestos.example.com   # or your lab URL — see Lab notes
export GUESTOS_API_TOKEN=your-token

curl -fk "$GUESTOS_URL/api/health"
curl -fk "$GUESTOS_URL/api/version"
python3 scripts/pdm_api_smoke.py --base-url "$GUESTOS_URL" --token "$GUESTOS_API_TOKEN"
```

(Use `-k` / configure trust for a lab self-signed cert.)

Example start (**template** clone + Sysprep — lab only):

```bash
curl -fk -X POST "$GUESTOS_URL/start_sysprep_workflow" \
  -H "Authorization: Bearer $GUESTOS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "template_vmid": 120,
    "hostname": "LABTEST01",
    "cores": 2,
    "ram": 4096,
    "network_mode": "dhcp",
    "administrator_password": "ChangeMe123!",
    "timezone": "Central European Standard Time",
    "join_domain": false,
    "remote_id": "lab"
  }'
```

## Automated smoke tests

### CI (mocked workflow — always safe)

```bash
pytest tests/test_sysprep_workflow_smoke.py tests/test_disks.py
```

Runs `sysprep_workflow_task` end-to-end with Proxmox/guest-agent calls stubbed. No lab required.

### Lab live (after major changes — destructive)

Run on the GuestOS host after meaningful workflow/API/Proxmox changes (not on a schedule). Needs Compose up, `.env`, and a disposable Windows template:

```bash
cd /opt/proxmox-guestos-customization
# Prefer docker exec so cleanup can import Flask/Proxmox helpers:
docker exec -e PYTHONUNBUFFERED=1 proxmox-guestos-customization-web-1 \
  python3 /app/scripts/pdm_api_smoke.py \
    --base-url http://127.0.0.1:5001 \
    --start-workflow --template-vmid 120 --remote-id vie-1 \
    --poll --cleanup
```

(`GUESTOS_API_TOKEN` / `PRIMARY_BRIDGE` come from the container env. Omit `--bridge` to use the API default.)

- Unique hostname is generated unless `--hostname` is set.
- `--cleanup` deletes the result VM after `SUCCESS` (uses `delete_vm`).
- Exit `0` = SUCCESS, `2` = FAILURE/timeout, `3` = cleanup failed.
- If `bridge` is omitted in the API body, GuestOS defaults to `PRIMARY_BRIDGE` (else `vmbr0`).

## Optional PDM UI fork

| Item | Value |
|------|--------|
| Fork | https://github.com/RobertLukan/proxmox-datacenter-manager-guestos — branch `guestos-sysprep` |
| Installed packages | UI **1.1.3+guestos.8**, server **1.1.7+guestos.8** (proxy + Customize) |
| Config | `/etc/proxmox-datacenter-manager/guestos.cfg` (`base-url`, `api-token`, `launch-secret`) |
| Customize | Windows templates only → PDM `POST …/guestos/launch` → GuestOS `/launch?…` |
| Job history | PDM **GuestOS** tab → PDM `GET …/guestos/tasks` (server proxies GuestOS) |

**Operator smoke:** PDM → remote `vie-1` → **Windows template** → **Customize (GuestOS)** → GuestOS wizard already logged in.

Production TLS / `PROXMOX_VERIFY_SSL`: see [`TLS_PRODUCTION.md`](TLS_PRODUCTION.md).

## Firewall sketch

- Operators / PDM host → GuestOS **`:443`** (TLS)
- GuestOS worker → each PVE API port (usually 8006)
- Loopback `:5001` on GuestOS host only (debug)

## Version pinning

PDM requires GuestOS **`version` ≥ `2.3.0`** (configurable as
`min-guestos-version` in `guestos.cfg`). GuestOS advertises
`min_pdm_guestos` on `GET /api/version`. Launch fails closed when the sidecar
is too old or unreachable.

Failure triage: [FAILURE_RUNBOOK.md](FAILURE_RUNBOOK.md). Start payload schema:
[openapi.yaml](openapi.yaml).
## Lab notes

**Maintainer lab inventory only** — not required for your deployment. Copy
these values only if you are on this same lab network.

| Item | Value |
|------|--------|
| GuestOS host | `192.168.123.197` (`guestos-lab`) |
| GuestOS URL | `https://192.168.123.197` |
| PDM host | `192.168.123.198` (`pdm-lab`) |
| PDM remote | `vie-1` |
| Windows templates | VMID **120** / **122** / **127** |

AD join: [AD_VALIDATION.md](AD_VALIDATION.md).
