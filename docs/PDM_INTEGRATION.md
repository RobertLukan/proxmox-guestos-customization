# PDM / machine API integration (sysprep-only)

GuestOS stays one app. **Standalone** keeps WinRM reconfigure in the browser UI.
**PDM / integrators** use the machine API for **template → clone → Sysprep** only
(VMware-style guest OS customization). In-place Sysprep of existing/production VMs is disabled.

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
| GET | `/task_status/<task_id>` | Bearer / token **or** session |

`POST /start_sysprep_existing_vm_task` returns **403** (disabled — protects production VMs).

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

Example start (**template** clone + Sysprep — lab only):

```bash
curl -fsS -X POST "$GUESTOS_URL/start_sysprep_workflow" \
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

Poll:

```bash
curl -fsS -H "Authorization: Bearer $GUESTOS_API_TOKEN" \
  "$GUESTOS_URL/task_status/TASK_ID_HERE"
```

Omit `remote_id` to use default `PROXMOX_HOST` / `USER` / `PASSWORD`.

## B-lite deep links (browser wizards)

Operators open the golden-image wizard with a remote preselected:

- **Customize from template:** `/sysprep_form?template_vmid=120&remote_id=lab` (GET; requires login)
- Legacy existing-VM URL redirects: templates → sysprep_form; non-templates → home (blocked)

These pages use the normal session cookie + CSRF. For PDM iframe later, plan a launch-token or proxy (later slice).

## Phase 4 — Thin PDM UI fork (template Customize deep-link)

Lab runs a forked **UI-only** package that adds **Customize (GuestOS)** on the QEMU panel
**only when the guest is a Proxmox template**. Click opens a new tab to the clone+Sysprep
wizard; the GuestOS API token stays on the sidecar (operator uses a normal GuestOS browser login).
The button is hidden on ordinary VMs so production guests cannot be Sysprep'd from PDM.

| Item | Value |
|------|--------|
| Fork (AGPL corresponding source) | https://github.com/RobertLukan/proxmox-datacenter-manager-guestos — branch `guestos-sysprep` |
| Upstream UI pin | `proxmox/proxmox-datacenter-manager` tree matching UI **1.1.3** / package SOURCE `2b7254dc…` |
| Installed package | `proxmox-datacenter-manager-ui` **1.1.3+guestos.3** on `pdm-lab` (apt-hold) |
| Baked `GUESTOS_BASE` | `http://192.168.123.197:5001` (`ui/src/guestos.rs`) |
| Deep link opened | `{GUESTOS_BASE}/sysprep_form?template_vmid={vmid}&remote_id={pdm_remote}` (e.g. `vie-1`) |

**Operator smoke:** PDM → remote `vie-1` → **Windows template** → **Customize (GuestOS)** → GuestOS wizard (clone + Sysprep). Log into GuestOS if prompted; do not start unless intended.

**Build notes (pdm-lab):** Proxmox `devel` apt suite + `mk-build-deps` for UI Build-Depends; `PATH` without rustup (`/usr/bin` first); `cd ui && make deb`. Fat LTO needs ~8–15 GiB RAM.

## Firewall sketch

- PDM host → GuestOS `:5001` (HTTPS if you terminate TLS)
- GuestOS worker → each PVE API port (usually 8006)
- **No** WinRM subnet requirement for this path

## Version pinning

PDM (or your notes) should record `guestos_min_version` ≥ the build you tested (e.g. `1.4.7`). Check with `GET /api/version`.

## Lab notes (Phase 0 / 2)

Recorded against this lab (update if hosts change):

| Item | Value |
|------|--------|
| GuestOS host | `192.168.123.197` (`guestos-lab`) |
| GuestOS URL | `http://192.168.123.197:5001` |
| PDM host | `192.168.123.198` (`pdm-lab`) |
| PDM remote | `vie-1` |
| Windows templates | VMID **120** / **122** |
