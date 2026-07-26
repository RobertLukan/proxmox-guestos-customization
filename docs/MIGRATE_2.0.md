# Migrating from GuestOS 1.x to 2.0

GuestOS **2.0** is **Sysprep-only**. The legacy **WinRM reconfigure** path
(temp NIC, WinRM ports, “Existing VMs”, `GUESTOS_ENABLE_WINRM`) is removed.

## What still works the same

- **Clone + Sysprep** from a Windows Proxmox template (standalone UI or PDM
  **Customize (GuestOS)**).
- Hostname, static/DHCP networking, optional AD join via domain profiles.
- Machine API: `/start_sysprep_workflow`, `/task_status/…`, `/api/tasks`, HMAC
  `/launch`.
- In-place Sysprep of arbitrary VMs stays **disabled** (same as 1.6).

## What was removed

| 1.x capability | 2.0 |
|----------------|-----|
| Clone & Configure (WinRM only) | Gone — use Clone + Sysprep |
| Reconfigure existing / lifecycle-tagged VMs over WinRM | Gone |
| `WINRM_*`, `TEMP_BRIDGE`, `GUESTOS_ENABLE_WINRM`, `pywinrm` | Gone |
| Temp NIC on a management bridge for guest config | Gone |

## How to replace old WinRM workflows

**Wrong hostname / IP / domain on a deployed clone?**  
Do **not** expect GuestOS to “patch” that guest. Delete (or archive) the VM and
run **Customize / Clone + Sysprep** again from the golden **template**.

**Lifecycle-tagged clones that only ever used WinRM?**  
Treat them as out of scope for GuestOS 2.0. Rebuild from a Windows template with
Sysprep customize when you next need a known-good guest.

**Automation that called WinRM routes?**  
Retarget to `POST /start_sysprep_workflow` (see [PDM_INTEGRATION.md](PDM_INTEGRATION.md)).
Endpoints such as `/start_clone_task`, `/start_reconfigure_task`, and
`/reconfigure_existing_vm` no longer exist.

## Upgrade steps

1. Deploy GuestOS **2.0.0** (`docker compose up -d --build` from the `v2.0.0` tag
   or updated tree).
2. Remove obsolete keys from `.env` if present: `WINRM_*`, `TEMP_BRIDGE`,
   `GUESTOS_ENABLE_WINRM`. Keep `PRIMARY_BRIDGE`, Proxmox, and `GUESTOS_*`
   launch/API secrets.
3. Confirm `GET /api/version` reports `2.0.0`.
4. Smoke: one disposable template Customize (or
   `scripts/pdm_api_smoke.py --start-workflow … --poll --cleanup`).
5. PDM fork unchanged in role: still Sysprep-only; no WinRM UI there.

## Why this break

Sysprep + guest agent already covers greenfield customize without opening WinRM.
Keeping both stacks doubled surface area and confused the supported path. 2.0
makes rebuild-from-template the only GuestOS customization model.
