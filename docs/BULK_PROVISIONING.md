# Bulk Provisioning (Windows 11 desktops)

GuestOS supports batch provisioning using the same clone + Sysprep engine as
single workflows.

## API endpoints

- `POST /start_sysprep_bulk_workflow` — submit a batch with `shared` defaults and `items`.
- `GET /api/batches` — list batches.
- `GET /api/batches/<batch_id>` — batch details + tasks.
- `POST /api/batches/<batch_id>/cancel` — best-effort cancel pending/running items.
- `GET /api/tasks?batch_id=<batch_id>` — task-level view.
- `GET /api/metrics` — inflight counters by status/remote.
- `GET /api/provision_limits?template_vmid=&remote_id=` — ceilings, remaining daily/batch quota, storage status.

See request/response schema examples in [`openapi.yaml`](openapi.yaml).

## UI workflow

In **New VM with Sysprep** on a **Windows 11** template (`windows11` tag), choose
**Bulk desktops (batch)**:

1. On **Basics**, paste CSV lines (static: `hostname,ip/prefix[,vlan]`; DHCP: `hostname[,vlan]`),
   optional shared gateway (static only), and **optional DNS** (leave blank to use
   DHCP-provided DNS; set explicitly for AD/DC override). VLAN is per CSV row —
   there is no Domain Profile DNS/VLAN shortcut in bulk.
2. On **Network**, pick the bridge (and skip the single-customize profile DNS/VLAN
   controls — they are hidden in bulk).
3. On **Domain** (optional), enable Join domain. Profile credentials use a Domain-step
   profile select (credentials only; does not fill guest DNS/VLAN). LDAP, ODJ, and
   the credential probe always use the profile's DNS so join passwords never go to
   caller-supplied resolvers. Or enter manual join credentials.
4. Submit. GuestOS creates a `batch_id` and enqueues one task per row.
5. Jobs page can be filtered by `batch_id`.

The form shows how many rows you can still add from remaining batch/daily capacity,
and rejects duplicate hostnames/IPs plus unusable addresses (loopback, link-local,
multicast) as you type.

**Windows Server** templates (e.g. `windowsserver2019` / `2022` / `2025`) do **not**
offer bulk mode — use single Customize only. The API rejects bulk starts against
Server templates.

## Safeguards (no override)

GuestOS refuses oversized or over-quota work. There is **no** superadmin bypass —
provision exceptions directly in Proxmox VE.

| Guard | Default |
|-------|---------|
| Max items per batch | **10** (`BULK_MAX_ITEMS`) |
| Max tasks per rolling 24h | **20** (`PROVISION_MAX_PER_DAY`) |
| Global inflight tasks | **10** (`BULK_MAX_CONCURRENT_GLOBAL`) |
| Per-remote inflight | **10** (`BULK_MAX_CONCURRENT_PER_REMOTE`) |
| Inflight batches | **10** (`BULK_MAX_INFLIGHT_BATCHES`) |
| Win11 cores / RAM / disks | **8** / **65536 MB** / **600 GB** |
| Server cores / RAM / disks | **16** / **65536 MB** / **2048 GB** |
| Storage warn / block | **65%** / **80%** used on template boot storage |

Template family is classified primarily by Proxmox tags:

- `windows11` → Win11 caps (lab template **127**)
- `windowsserver2019` / `windowsserver2022` / `windowsserver2025` (or `windowsserver*`) → Server caps (lab template **120** is 2019)

Disk totals apply to requested `manage_disks` plan sizes only (thin clones without
Configure disks do not sum template size into the GuestOS disk cap). The wizard also
shows an informational **batch disk sum** (`per-VM × row count`) — display only,
not enforced.

Storage at ≥65% returns a **warning** on start (and in the wizard); ≥80% **blocks**
deployment.

## Operational limits

Tune via `.env` (see `.env.example`). Concurrent admission still applies in
addition to daily/batch quotas.

Default queues:

- `clone_queue` for clone/config/sysprep phase
- `verify_queue` for guest verification phase

Compose runs separate workers for these queues (`worker`, `verify-worker`).

## Failure behavior

- Batch submission validates shared caps/quota/storage before enqueue.
- Invalid rows are rejected before enqueue where possible.
- Idempotency supports safe retries via `Idempotency-Key` or payload `request_id`.
- Cancel marks matching pending/running tasks as `CANCELLED` best-effort.
  Clones that already have a `result_vmid` are renamed/tagged
  `failed-customization` and hard-stopped when PVE is reachable.
- Failed items remain in task history for triage.

See [`FAILURE_RUNBOOK.md`](FAILURE_RUNBOOK.md) for cleanup and saturation signals.
