# PDM / machine API integration (sysprep-only)

GuestOS is one app. **Sysprep customize** (template → clone → guest agent) is the
supported path for PDM and new standalone use.

**WinRM reconfigure** remains in the standalone browser UI as **legacy / deprecated**
(lifecycle-tagged clones only). Do not build PDM or new automation on WinRM routes.

In-place Sysprep of existing/production VMs is disabled.

Current app version is in [`VERSION`](../VERSION) and `GET /api/version`.

## Prerequisites

1. GuestOS Compose (web + worker + Redis + **Caddy TLS**) reachable from operator browsers / PDM.
2. Worker can reach the target Proxmox API(s).
3. Set in `.env` (see `.env.example`):

```bash
GUESTOS_API_TOKEN=generate-a-long-random-string
BEHIND_REVERSE_PROXY=True
GUESTOS_TLS_HOST=192.168.123.197
GUESTOS_LAUNCH_SECRET=must-match-pdm-ui-bake-in
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

Do **not** call reconfigure/WinRM routes from PDM (legacy standalone only; deprecated).

## One-click launch (browser)

PDM **Customize (GuestOS)** opens:

`https://<host>/launch?template_vmid=…&remote_id=…&exp=…&jti=…&sig=…`

GuestOS verifies HMAC (`GUESTOS_LAUNCH_SECRET`), creates a session, redirects to `/sysprep_form`. Tokens expire (`GUESTOS_LAUNCH_TTL`, default 300s) and are single-use.

## Job history (PDM GuestOS tab)

GuestOS keeps a SQLite task ledger. PDM’s **GuestOS** tab (global Remotes panel and per-PVE remote) polls:

```http
GET /api/tasks?kind=customization&limit=150
GET /api/tasks?kind=customization&remote_id=<pdm-remote>&limit=150
Authorization: Bearer <GUESTOS_API_TOKEN>
```

Response shape: `{ "tasks": [ { "id", "name", "status", "progress", "message", "timestamp", "updated_at", "remote_id", "template_vmid", "hostname", "result_vmid", … } ] }`.

Also: `GET /api/tasks/<id>` and HTML `/jobs`. Set `GUESTOS_CORS_ORIGINS` if you restrict browser CORS (default `*`).

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

## Phase 4 — Thin PDM UI fork

| Item | Value |
|------|--------|
| Fork | https://github.com/RobertLukan/proxmox-datacenter-manager-guestos — branch `guestos-sysprep` |
| Installed package | `proxmox-datacenter-manager-ui` **1.1.3+guestos.5** (apt-hold) |
| Baked `GUESTOS_BASE` | `https://192.168.123.197` |
| Deep link | `{GUESTOS_BASE}/launch?…` (HMAC; skips GuestOS password) |
| Job history | PDM **GuestOS** tab → `{GUESTOS_BASE}/api/tasks` |

**Operator smoke:** PDM → remote `vie-1` → **Windows template** → **Customize (GuestOS)** → GuestOS wizard already logged in.

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
