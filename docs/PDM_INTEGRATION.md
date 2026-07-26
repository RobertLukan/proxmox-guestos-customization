# PDM / machine API integration (sysprep-only)

**Install / production deploy:** see [`INSTALL.md`](INSTALL.md) (GuestOS Compose +
optional PDM fork packages, privileges, firewall). This page is the API and
lab-integration reference.

**Upgrading from 1.x:** see [`MIGRATE_2.0.md`](MIGRATE_2.0.md) — GuestOS 2.0 removed
the legacy WinRM reconfigure path.

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
GUESTOS_TLS_HOST=192.168.123.197
GUESTOS_LAUNCH_SECRET=must-match-pdm-guestos-cfg
# optional multi-cluster:
# PVE_REMOTES_JSON={"lab":{"host":"pve.example","user":"api@pve","password":"...","verify_ssl":false}}
```

4. Generate lab certs and rebuild:

```bash
./deploy/caddy/gen-selfsigned.sh "$GUESTOS_TLS_HOST"
docker compose up -d --build
```

## Endpoints for PDM

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/health` | none |
| GET | `/api/version` | none |
| GET | `/launch` | HMAC query (`exp`, `jti`, `sig`, `template_vmid`, `remote_id`) → session |
| POST | `/start_sysprep_workflow` | Bearer / `X-Api-Token` **or** session+CSRF |
| GET | `/task_status/<task_id>` | Bearer / token **or** session |

`POST /start_sysprep_existing_vm_task` returns **403** (disabled — protects production VMs).

## Job history (PDM GuestOS tab)

GuestOS keeps a SQLite task ledger. PDM’s **GuestOS** tab calls the **PDM server
proxy** (`GET /api2/extjs/guestos/tasks`), which uses `guestos.cfg` (`base-url`,
`api-token`) to fetch:

```http
GET /api/tasks?kind=customization&limit=150
GET /api/tasks?kind=customization&remote_id=<pdm-remote>&limit=150
Authorization: Bearer <GUESTOS_API_TOKEN>
```

Browsers no longer need the GuestOS API token. Configure PDM with
`/etc/proxmox-datacenter-manager/guestos.cfg` (see the PDM fork
`docs/guestos.cfg.example`).

Response shape from GuestOS: `{ "tasks": [ { "id", "name", "status", "progress", "message", "timestamp", "updated_at", "remote_id", "template_vmid", "hostname", "result_vmid", … } ] }`.

Also: `GET /api/tasks/<id>` and HTML `/jobs`.

## One-click launch (browser)

PDM **Customize (GuestOS)** (Windows templates only) asks PDM
`POST /api2/extjs/guestos/launch`, then opens the returned URL:

`https://<host>/launch?template_vmid=…&remote_id=…&exp=…&jti=…&sig=…`

GuestOS verifies HMAC (`GUESTOS_LAUNCH_SECRET`), creates a session, redirects to `/sysprep_form`. Tokens expire (`GUESTOS_LAUNCH_TTL`, default 300s) and are single-use.
## Smoke check from the PDM host

```bash
export GUESTOS_URL=https://192.168.123.197
export GUESTOS_API_TOKEN=your-token

curl -fk "$GUESTOS_URL/api/health"
curl -fk "$GUESTOS_URL/api/version"
python3 scripts/pdm_api_smoke.py --base-url "$GUESTOS_URL" --token "$GUESTOS_API_TOKEN"
```

(Use `-k` / configure trust for the lab self-signed cert.)

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
pytest tests/test_sysprep_workflow_smoke.py
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

## Phase 4 — Thin PDM UI fork

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

`guestos_min_version` ≥ `1.5.0`. Check with `GET /api/version`.

## Lab notes

| Item | Value |
|------|--------|
| GuestOS host | `192.168.123.197` (`guestos-lab`) |
| GuestOS URL | `https://192.168.123.197` |
| PDM host | `192.168.123.198` (`pdm-lab`) |
| PDM remote | `vie-1` |
| Windows templates | VMID **120** / **122** |

AD join: [AD_VALIDATION.md](AD_VALIDATION.md).
