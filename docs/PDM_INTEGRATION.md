# PDM / machine API integration (sysprep-only)

GuestOS stays one app. **Standalone** keeps WinRM reconfigure in the browser UI.
**PDM / integrators** use the machine API for **sysprep only**.

Current app version is in [`VERSION`](../VERSION) and `GET /api/version`.

## Prerequisites

1. GuestOS Compose (web + worker + Redis) reachable from the PDM host.
2. Worker can reach the target Proxmox API(s).
3. Set in `.env` (see `.env.example`):

```bash
GUESTOS_API_TOKEN=generate-a-long-random-string
# optional multi-cluster:
# PVE_REMOTES_JSON={"lab":{"host":"pve.example","user":"api@pve","password":"...","verify_ssl":false}}
```

4. Rebuild/restart after changing env:

```bash
docker compose up -d --build
```

## Endpoints for PDM

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/health` | none |
| GET | `/api/version` | none |
| POST | `/start_sysprep_workflow` | Bearer / `X-Api-Token` **or** session+CSRF |
| POST | `/start_sysprep_existing_vm_task` | same |
| GET | `/task_status/<task_id>` | Bearer / token **or** session |

Do **not** call reconfigure/WinRM routes from PDM.

## Smoke check from the PDM host

From a shell **on the PDM host** (or any host that can reach GuestOS):

```bash
export GUESTOS_URL=http://guestos-host:5001
export GUESTOS_API_TOKEN=your-token

# Health
curl -fsS "$GUESTOS_URL/api/health"
curl -fsS "$GUESTOS_URL/api/version"

# Or use the helper (polls optional start — default is health-only):
python3 scripts/pdm_api_smoke.py --base-url "$GUESTOS_URL" --token "$GUESTOS_API_TOKEN"
```

Example start (existing VM, DHCP, **destructive** — use a lab VM):

```bash
curl -fsS -X POST "$GUESTOS_URL/start_sysprep_existing_vm_task" \
  -H "Authorization: Bearer $GUESTOS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "vmid": 121,
    "hostname": "LABTEST01",
    "network_mode": "dhcp",
    "administrator_password": "ChangeMe123!",
    "timezone": "Central European Standard Time",
    "join_domain": false,
    "remote_id": "lab"
  }'
```

Poll:

```bash
curl -fsS -H "Authorization: Bearer $GUESTOS_API_TOKEN" \
  "$GUESTOS_URL/task_status/TASK_ID_HERE"
```

Omit `remote_id` to use default `PROXMOX_HOST` / `USER` / `PASSWORD`.

## B-lite deep links (browser wizards)

Operators can open the existing wizards with a remote preselected:

- Existing VM: `/sysprep_existing_vm_form/<vmid>?remote_id=lab`
- New from template: `/sysprep_form?template_vmid=100&remote_id=lab` (GET; still requires login)

These pages use the normal session cookie + CSRF. For PDM iframe later, plan a launch-token or proxy (Phase 4).

## Firewall sketch

- PDM host → GuestOS `:5001` (HTTPS if you terminate TLS)
- GuestOS worker → each PVE API port (usually 8006)
- **No** WinRM subnet requirement for this path

## Version pinning

PDM (or your notes) should record `guestos_min_version` ≥ the build you tested (e.g. `1.4.0`). Check with `GET /api/version`.
