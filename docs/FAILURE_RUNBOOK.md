# GuestOS failure runbook

Operational guide when Clone + Sysprep jobs fail or stall. For the API contract
see [PDM_INTEGRATION.md](PDM_INTEGRATION.md) and [openapi.yaml](openapi.yaml).
Batch-specific controls and limits are in [BULK_PROVISIONING.md](BULK_PROVISIONING.md).

## Quick triage

| Symptom | Likely cause | First checks |
|---------|--------------|--------------|
| Task `FAILURE` early (“Invalid sysprep input”) | Bad hostname/IP/domain/`manage_disks` | Task message; Server name/tag for disks |
| Stuck ~60–85% “Waiting for QEMU Guest Agent” | Guest agent / first-boot loop | Console on clone; QGA package; template health |
| Stuck ~88–92% after Sysprep | Shutdown wait missed / hung generalize | Console; agent bounce; orphaned clone VMID in task |
| `verification failed` / `setup.done missing` | FirstLogon `setup.ps1` never finished | Guest `C:\ProgramData\GuestOS\` markers + transcript; on **Eval** Server check InstallPid / GVLK regression below |
| Stuck ~98% waiting for `setup.ps1` after Sysprep | OOBE never reached FirstLogon (often Eval+GVLK) | Console: product-key / InstallPid `0xC004F015` → see Evaluation vs GVLK |
| `setup.ps1 failed` | Network, disk serial, pagefile, domain join | `setup.failed` contents; guest event log |
| DHCP verify fail after `setup.done` | No lease on expected NIC | Bridge/VLAN; DHCP server; MAC match |
| Domain verify fail | Join or reboot incomplete | Creds/OU/DNS; `PartOfDomain` via QGA |
| Disk verify fail / `pagefile_pending_reboot` | Volume/pagefile not ready | Serials; drive letters; reboot once then re-check |

## Marker files (guest)

`setup.ps1` is persisted as `HKLM\SOFTWARE\GuestOS\SetupPs1B64` before Sysprep
(specialize deletes loose copies under Sysprep/ProgramData; embedding the script
in unattend hung Sysprep in lab). FirstLogon extracts it to
`C:\Windows\Temp\GuestOS-setup.ps1`. Markers:

- `setup.done` — FirstLogon setup finished; required for SUCCESS
- `setup.failed` — setup threw; verify fails with detail
- `setup.lock` / transcript — concurrent run / debugging

Also written: `HKLM\SOFTWARE\GuestOS` `SetupStatus=done|failed` (survives ProgramData cleanup).

**Do not run `setup.ps1` from SetupComplete.cmd** — that path runs during specialize and
specialize cleanup can delete `ProgramData\GuestOS` after the script finishes, which
drops `setup.done` while leaving IP/disks applied. FirstLogonCommands is the only runner.

## Agent hang

1. Open the clone console (VMID on the task / PDM GuestOS tab).
2. Confirm QEMU Guest Agent is running inside Windows.
3. If OOBE is stuck on **Enter product key** on a **volume-license** Server image
   (common on Server 2022/2025 VL), GuestOS should inject a Microsoft GVLK into
   unattend specialize when no `product_key` was provided. Re-run on a build that
   includes that support, or pass an explicit `product_key`. Do not use empty Setup
   `ProductKey`/`WillShowUI` in specialize — that fails with "could not apply
   unattend settings".
4. If OOBE stalls / FirstLogon never runs on an **Evaluation** Server image, see
   [Evaluation vs GVLK](#evaluation-vs-gvlk-server-2019-regression) below —
   do **not** inject a VL GVLK into Eval.
5. If OOBE is stuck elsewhere, check AutoLogon (`LogonCount=3`) and that
   `GuestOS-FirstLogon.cmd` / setup markers exist.
6. Do not re-run in-place Sysprep on a production VM; delete the clone and start a new customize.

## Evaluation vs GVLK (Server 2019 regression)

**Background:** Support for Windows Server **2022** (and later) needed an automatic
KMS client setup key (GVLK) in unattend so VL images skip the OOBE **Enter product
key** page. That path landed in 2.6.x and is correct for **volume-license** SKUs
(`ServerStandard`, `ServerDatacenter`, …).

**Regression:** The same auto-GVLK was also applied to **Evaluation** templates
(e.g. lab Server 2019 Standard Evaluation). A VL GVLK is invalid on Eval; OOBE
`InstallPid` fails with **`hr=0xC004F015`**, FirstLogonCommands never run, and the
job sits at ~98% waiting for `setup.ps1` / `setup.done`.

**Fix (2.6.3+):** Guest edition is detected via the guest agent. Evaluation SKUs
(`*Eval*` in edition/caption):

- never receive an auto-injected GVLK;
- omit specialize `<ProductKey>`;
- set OOBE registry markers (`SetupDisplayedProductKey` /
  `UnattendCreatedUser`) so the product-key UI is skipped without InstallPid.

VL guests still get the matched GVLK. One unattend template; Jinja branches on
`windows_evaluation` — not separate setup trees.

**Operator checks:**

| Guest (agent) | Worker log | Expected unattend |
|---------------|------------|-------------------|
| `ServerStandard` / `… Standard` (no Eval) | `Using Server GVLK for VM …` | `<ProductKey>` present |
| `ServerStandardEval` / `… Evaluation` | `Skipping GVLK for Evaluation guest …` | no ProductKey; Eval OOBE regs |

If you still see Eval guests getting a ProductKey, upgrade GuestOS past 2.6.2.

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

### Stage tags (lifecycle-*)

While a customize runs, GuestOS replaces a single `lifecycle-*` tag so operators can see progress in PVE:

| Stage | Tag |
|-------|-----|
| Clone / configure | `lifecycle-cloning` |
| Boot / guest agent | `lifecycle-booting` |
| Write unattend / setup | `lifecycle-customizing` |
| Sysprep | `lifecycle-sysprep` |
| Verify | `lifecycle-verifying` |
| Success | `lifecycle-ready` |
| Failure | `lifecycle-failed` (+ `failed-customization`) |

Non-lifecycle tags (e.g. `uuid:…`, family tags) are preserved.

## Silent disk skip (removed)

`manage_disks=true` on a non-Server template now **fails validation** (no silent disable). Name/tag the template (`windowsserver2019|2022|2025`, `server2022`, `guestos-disk`, …) or omit `manage_disks`. Disk customize is not exposed in the PDM Customize UI — use the machine API / smoke script.

## Compose / Redis

- `/api/health` reports `database` + `redis` (503 when degraded).
- Set `REDIS_PASSWORD` in `.env`; Compose wires it into Celery URLs via `deploy/compose-redis-env.sh`.
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

## SQLite ledger backup

Default DB is under the Compose `app-instance` volume (`instance/site.db`). Backup that volume (or the file) before upgrades; restore by replacing the file while web/worker are stopped.
