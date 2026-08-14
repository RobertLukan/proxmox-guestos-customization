# GuestOS failure runbook

Operational guide when Clone + Sysprep jobs fail or stall. For the API contract
see [PDM_INTEGRATION.md](PDM_INTEGRATION.md) and [openapi.yaml](openapi.yaml).
Batch-specific controls and limits are in [BULK_PROVISIONING.md](BULK_PROVISIONING.md).

## Quick triage

| Symptom | Likely cause | First checks |
|---------|--------------|--------------|
| Task `FAILURE` early (“Invalid sysprep input”) | Bad hostname/IP/domain/`manage_disks` | Task message; Server name/tag for disks |
| Stuck ~60–85% “Waiting for QEMU Guest Agent” | Guest agent / first-boot loop | Console on clone; QGA package; template health |
| Stuck ~88–92% after Sysprep | Shutdown wait missed / hung generalize | Console; agent bounce; orphaned clone VMID in task; worker logs `[sysprep]:` + Panther probe lines |
| Task `FAILURE` `sysprep_guest_failed` | Sysprep validate failed in guest (AppX/Copilot/unattend) | Task message includes Panther log excerpt; fix template AppX then re-clone |
| Fail writing unattend / setup before Sysprep | QGA `file-write` ACL / agent overload | Worker log: `agent file-write` / Permission denied — see [QGA file writes](#qga-file-writes) |
| `verification failed` / `setup.done missing` | SYSTEM `GuestOS-Setup` task never finished `setup.ps1` | Guest `C:\ProgramData\GuestOS\` markers + transcript; `schtasks /Query /TN GuestOS-Setup`; on **Eval** Server check InstallPid / GVLK regression below |
| Stuck ~98% on `pending reboot` | Pagefile/domain reboot never finalized `setup.done` | `setup.pending_reboot` present; task still registered and fires at next startup? |
| Stuck ~98% waiting for `setup.ps1` after Sysprep | OOBE never completed / task never ran (often Eval+GVLK) | Console: product-key / InstallPid `0xC004F015` → see Evaluation vs GVLK; confirm `GuestOS-Setup` task exists |
| `setup.ps1 failed` | Network, disk serial, pagefile, domain join | `setup.failed` contents; guest event log |
| DHCP verify fail after `setup.done` | No lease on expected NIC | Bridge/VLAN; DHCP server; MAC match |
| Domain verify fail / `WARNING: domain join failed` | Join failed (DC down, bad creds/DNS) or slow | Guest marker `domain-join-failed` shortens verify; check DC/DNS preflight; `PartOfDomain` via QGA |
| Admit fails: DC/DNS unreachable | `join_domain` preflight (TCP 53/88/389) | Power on DC; fix `dns_servers` / routing before clone |
| Task `FAILURE` `domain_cred_probe` before Sysprep | In-clone LDAP/ADSI bind failed | Read `class=` / `guest_ip=` / `username=` in task message; fix VLAN/DHCP/DNS or join account; never re-run Sysprep on that clone |
| Admit fails: computer already exists / bad OU | Hostname uniqueness or `domain_ou` LDAP check | Rename hostname; remove stale computer object; fix OU DN |
| Disk verify fail / `pagefile_pending_reboot` | Volume/pagefile not ready (legacy) | Prefer builds that wait for `setup.done` after pagefile reboot; check serials/letters |
| Wrong data/pagefile roles on multi-disk template | Plan used bus order without `source_key` | Use disk planner; bind `source_key`; match sizes |

## Marker files (guest)

`setup.ps1` is persisted as `HKLM\SOFTWARE\GuestOS\SetupPs1B64` before Sysprep
(specialize deletes loose copies under Sysprep/ProgramData; embedding the script
in unattend hung Sysprep in lab). Specialize registers scheduled task
**`GuestOS-Setup`** (SYSTEM, AtStartup +45s plus a short Once+repeat catch-up
for the first post-Sysprep boot — AtStartup alone is missed when the task is
registered during specialize), which extracts the script to
`C:\Windows\Temp\GuestOS-setup.ps1` via `GuestOS-FirstLogon.cmd`. There is **no**
AutoLogon / FirstLogonCommands. Markers:

- `setup.done` — setup finished; required for SUCCESS
- `setup.done` with detail `domain-join-failed` — OS/network setup finished but AD join failed (soft); verify emits **WARNING** and skips the long domain wait
- `setup.failed` — setup threw; verify fails with detail
- `setup.pending_reboot` — pagefile or domain join scheduled a reboot; verify keeps waiting until `setup.done` after the next AtStartup task run
- `setup.lock` / transcript — concurrent run / debugging

**Domain-join preflight:** When `join_domain` is true, admit/worker probe configured
`dns_servers` (and resolved `domain_name`) on TCP 53/88/389 before clone so a
powered-off DC fails fast instead of after Sysprep.

**In-clone credential probe (pre-Sysprep):** After QGA is up and **before** writing
Sysprep payloads, GuestOS runs a short guest LDAP/ADSI bind with the join account
(`DOMAIN_JOIN_CRED_PROBE`, default on). Failures use error code `domain_cred_probe`
and include debug fields (`class`, `domain`, `username`, `dns_servers`,
`bind_target`, `guest_ip`) — never the password. At probe time the NIC bridge/VLAN
is already set, but the guest IP is usually still DHCP (not the final unattend
static IP). That is intentional: it answers “can this account bind to the DC from
this L2 path?” Operators can also call `POST /api/domain/test_credentials` (or use
the UI **Test credentials** buttons) from the GuestOS host without cloning.

Also written: `HKLM\SOFTWARE\GuestOS` `SetupStatus=done|failed|pending_reboot` (survives ProgramData cleanup).

**Credential scrub:** After setup reaches **final** `done` (including the post-reboot finalize),
`setup.ps1` removes `SetupPs1B64`, `C:\GuestOS\setup.ps1`, and Temp
`GuestOS-setup.ps1` copies so domain-join credentials are not left on disk, and
**unregisters** the `GuestOS-Setup` task. While `pending_reboot` is set,
`SetupPs1B64` is kept and the scheduled task stays enabled so AtStartup can
finalize after reboot. Markers (`setup.done`) remain for verify.
**Do not run `setup.ps1` from SetupComplete.cmd as the primary runner** — that path
runs during specialize and specialize cleanup can delete `ProgramData\GuestOS`
after the script finishes. SetupComplete only re-registers the task as a safety net.

## Agent hang

Worker Compose logs should show Sysprep phase lines tagged ``[sysprep]:``
(command issued, still running, Panther probe OK/failure, stopped/power-on).
Task API progress alone previously looked silent in ``docker compose logs``.

1. Open the clone console (VMID on the task / PDM GuestOS tab).
2. Confirm QEMU Guest Agent is running inside Windows.
3. If OOBE is stuck on **Enter product key** on a **volume-license** Server image
   (common on Server 2022/2025 VL), GuestOS should inject a Microsoft GVLK into
   unattend specialize when no `product_key` was provided. Re-run on a build that
   includes that support, or pass an explicit `product_key`. Do not use empty Setup
   `ProductKey`/`WillShowUI` in specialize — that fails with "could not apply
   unattend settings".
4. If OOBE stalls / the setup task never runs on an **Evaluation** Server image, see
   [Evaluation vs GVLK](#evaluation-vs-gvlk-server-2019-regression) below —
   do **not** inject a VL GVLK into Eval.
5. If OOBE is stuck elsewhere, confirm `GuestOS-Setup` exists
   (`schtasks /Query /TN GuestOS-Setup`), `GuestOS-FirstLogon.cmd` is present, and
   setup markers under `C:\ProgramData\GuestOS\`. Winlogon should **not** have
   `AutoAdminLogon` / `DefaultPassword`.
6. Do not re-run in-place Sysprep on a production VM; delete the clone and start a new customize.

## Evaluation vs GVLK (Server 2019 regression)

**Background:** Support for Windows Server **2022** (and later) needed an automatic
KMS client setup key (GVLK) in unattend so VL images skip the OOBE **Enter product
key** page. That path landed in 2.6.x and is correct for **volume-license** SKUs
(`ServerStandard`, `ServerDatacenter`, …).

**Regression:** The same auto-GVLK was also applied to **Evaluation** templates
(e.g. lab Server 2019 Standard Evaluation). A VL GVLK is invalid on Eval; OOBE
`InstallPid` fails with **`hr=0xC004F015`**, OOBE never finishes, the
`GuestOS-Setup` task never usefully runs `setup.ps1`, and the
job sits at ~98% waiting for `setup.ps1` / `setup.done`.

**Fix (2.6.3+):** Guest edition is detected via the guest agent. Evaluation SKUs
(`*Eval*` in edition/caption):

- never receive an auto-injected GVLK;
- omit specialize `<ProductKey>`;
- set OOBE registry markers (`SetupDisplayedProductKey` /
  `UnattendCreatedUser`) so the product-key UI is skipped without InstallPid.

VL guests still get the matched GVLK when edition is known. One unattend
template; Jinja branches on `windows_evaluation` — not separate setup trees.

**Client OOBE user-name prompt (no AutoLogon):** On Win10/11,
`HideLocalAccountScreen` does **not** apply (Server-only). Without AutoLogon,
OOBE still asks “Who's going to use this device?” unless unattend creates a
`LocalAccounts` entry. GuestOS creates a short-lived `GuestOSOobe` admin for
that page; `setup.ps1` removes it after enabling built-in Administrator.
`UnattendCreatedUser` is set for all SKUs as an extra OOBE marker.

**Fail closed (2.6.5+):** If the guest-agent edition probe returns empty (WMI/QGA
glitch), GuestOS does **not** invent a Standard GVLK. Worker log:
`Skipping auto-GVLK … guest edition unknown`. Pass explicit `product_key` or
fix QGA/WMI, then re-run. Blind Standard fallback could recreate the Eval
`0xC004F015` failure.

**Operator checks:**

| Guest (agent) | Worker log | Expected unattend |
|---------------|------------|-------------------|
| `ServerStandard` / `… Standard` (no Eval) | `Using Server GVLK for VM …` | `<ProductKey>` present |
| `ServerStandardEval` / `… Evaluation` | `Skipping GVLK for Evaluation guest …` | no ProductKey; Eval OOBE regs |
| empty / unreadable | `Skipping auto-GVLK … edition unknown` | no ProductKey |

If you still see Eval guests getting a ProductKey, upgrade GuestOS to **2.6.3+**
(prefer current release).

## QGA file writes

GuestOS prefers Proxmox native ``agent/file-write`` (with chunking for large
payloads), then falls back to guest-exec PowerShell. Common failures:

1. **Permission denied / file-write unsupported** — confirm the PVE token can use
   guest agent file APIs; check worker logs for `agent file-write` / fallback.
2. **Staged `setup.ps1` missing after write** — older chunked exec overload;
   upgrade so native file-write is used. Confirm `GuestOS-FirstLogon.cmd` exists
   under `C:\Windows\System32\` before Sysprep.
3. Re-run Customize on a fresh clone after fixing the template agent.

## Verify failed but Sysprep ran

The guest may be usable. Inspect markers, hostname, IP, and disks, then either:

- Fix manually and keep the VM, or
- Delete the clone and re-run Customize with corrected payload (static IP, domain, disks).

## Orphaned / failed clones

Failed jobs **do not** auto-delete the clone. GuestOS:

1. Marks the task `FAILURE` (see `message` / `error_code` / `result_vmid`).
2. Renames the Proxmox VM to `failed-<hostname>` (truncated if needed).
3. Sets tags **`failed-customization`** and **`lifecycle-failed`**.

Find them in PVE by tag `failed-customization` or name prefix `failed-`. Inspect, then delete manually when done.

Cancelled jobs (batch cancel or mid-flight cancel after clone) also tag/stop the
clone when a `result_vmid` is known — they should not remain as untagged
`lifecycle-*` runners.

### Stage tags (lifecycle-*)

While a customize runs, GuestOS replaces a single `lifecycle-*` tag so operators can see progress in PVE:

| Stage | Tag |
|-------|-----|
| Clone / configure | `lifecycle-cloning` |
| Boot / guest agent | `lifecycle-booting` |
| Write unattend / setup | `lifecycle-customizing` |
| Sysprep | `lifecycle-sysprep` |
| Verify | `lifecycle-verifying` |
| Success | `lifecycle-ready` (also clears `failed-customization` if present) |
| Failure | `lifecycle-failed` (+ `failed-customization`) |

Tag updates parse both Proxmox `;` and `,` delimiters so lifecycle replace
works when the live config already uses semicolons.

### Stuck around 95% (“queued for verification”)

After Sysprep, the clone worker enqueues `verify_queue` and returns. If
**verify-worker** is down or not consuming that queue, the UI can sit near 95%
while the guest may already have `setup.done`. Check:

1. `docker compose ps` — `verify-worker` healthy/running
2. `GET /api/health` — `checks.clone_worker` and `checks.verify_worker` should be `ok`
3. Worker logs for the verify task

Non-lifecycle tags (e.g. `uuid:…`, family tags) are preserved.

## Silent disk skip (removed)

`manage_disks=true` on a non-Server template now **fails validation** (no silent disable). Name/tag the template (`windowsserver2019|2022|2025`, `server2022`, `guestos-disk`, …) or omit `manage_disks`. Disk customize is not exposed in the PDM Customize UI — use the machine API / smoke script.

## Compose / Redis

- `/api/health` reports `database`, `redis`, `clone_worker`, `verify_worker`, and `default_password` (503 when degraded).
- Set `REDIS_PASSWORD` in `.env`; Compose wires it into Celery URLs via `deploy/compose-redis-env.sh`.
- Air-gap: `docker-compose.offline.yml` sets `pull_policy: never` so recreate does not hit the registry.
- Durable launch JTIs use Redis when available, else SQLite `launch_jti`.

## Bulk saturation

Symptoms:

- `POST /start_sysprep_bulk_workflow` returns admission-limit errors.
- `GET /api/metrics` shows sustained high `tasks.inflight` or skewed `inflight_by_remote`.
- Batch rows stay `PENDING` for long periods.
- API/UI errors mentioning daily / batch / inflight / storage limits.

Mitigations:

1. Retry with fewer rows per batch (max 10 by default).
2. Check `GET /api/provision_limits` for remaining daily quota and storage %.
3. Free Proxmox storage if used ≥ 80% (hard block) or reduce load if ≥ 65% (warning).
4. Scale workers by queue (`clone_queue` and `verify_queue`) and host resources.
5. There is **no** in-app override — provision exceptions directly in Proxmox VE.
6. Tune `BULK_MAX_*` / `PROVISION_MAX_PER_DAY` / `STORAGE_*_PCT` only after validating capacity.

### Stuck RUNNING / PROGRESS jobs

GuestOS auto-heals the ledger on Jobs page, `/api/metrics`, batch GET, and bulk
admission:

- **Finished batches** left `RUNNING` are finalized to `SUCCESS` / `FAILED` /
  `CANCELLED` once every child task is terminal.
- **Orphan inflight tasks** (`PENDING`/`STARTED`/`PROGRESS`) with no
  `updated_at` activity for `TASK_STALE_AFTER_SECONDS` (default **6 hours**) are
  marked `FAILURE` (`error_code=stale`) and any clone is tagged
  `failed-customization`. Celery workflows also have a ~4h soft time limit.

To force a sweep: open **Jobs** or call `GET /api/metrics` (response includes
`janitor.tasks_reaped` / `janitor.batches_finalized`).

## SQLite ledger backup

Default DB is under the Compose `app-instance` volume (`instance/site.db`). Backup that volume (or the file) before upgrades; restore by replacing the file while web/worker are stopped.
