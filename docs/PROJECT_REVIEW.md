# Project Review

## Scope and outcome

Reviewed the current working tree, including uncommitted Linux package and
appliance workflow changes, for correctness, security, and provisioning
reliability. This review did not change application source files or perform
live provisioning validation.

The project has useful provisioning safeguards, clear operational documentation,
and a substantial test suite. However, the findings below should be addressed
before relying on the reviewed working tree for provisioning, particularly the
new appliance workflow and Linux data-disk path.

File and line references describe the working tree at the time of review and
may shift as fixes are applied.

## Findings

### 1. High — Linux deployments with data disks always fail verification

**Status:** Fixed — `else` is under the mount check; regression tests in
`tests/test_linux_verify.py`.

**Location:** `app/linux_verify.py` (mount loop in `verify_linux_result`)

The `else` belonged to the `for` loop rather than the
`if _verify_mount(...)` condition. Because the loop never broke, verification
always set `ok = False` after checking data mounts, even when every mount
succeeded.

**Impact:** Otherwise successful deployments containing data disks were reported
as failures. Subsequent package installation was also skipped because it is
conditional on verification remaining successful.

### 2. High — Accepted appliance requests can reverse WAN and LAN

**Status:** Fixed — `prepare_appliance_payload` normalizes to `[WAN, LAN]`
before clone; LAN-first covered in `tests/test_appliance.py`.

**Locations:** `app/appliance.py`, `app/linux_cloudinit.py` (`clone_nic_list`)

Payload validation accepted either NIC order and retained that order. The clone
NIC list preserved it, but the OPNsense setup script always assigns WAN to
`vtnet0` and LAN to `vtnet1`.

**Impact:** A valid request could connect the firewall's logical interfaces to
the wrong bridges, creating both connectivity problems and a security-boundary
risk.

### 3. High — Data-disk initialization can skip unmounted directories

**Status:** Fixed — finalize and `_verify_mount` use `findmnt --mountpoint`.

**Location:** `app/linux_verify.py` (`_linux_data_disk_script`, `_verify_mount`)

The generated initialization script used `findmnt --target`, which finds the
filesystem containing a path and does not require that the path itself be a
mountpoint. An existing `/data` directory on the root filesystem could cause
the script to skip formatting and mounting.

**Impact:** Requested data disks may remain unused. Applications can write into
the root filesystem instead of the intended data volume.

### 4. Medium — Secret-store fallback loses credentials when Redis recovers

**Status:** Fixed — Redis key miss returns `None` so SQLite is consulted;
recovery test in `tests/test_task_secrets.py`.

**Locations:** `app/task_secrets.py` (`_load_redis`, `load_task_secrets`)

Secret storage falls back to SQLite when the Redis write fails. Loading
consulted SQLite only when the Redis loader returned `None`, not when Redis
was healthy but the key was absent (`{}`).

**Impact:** Provisioning could proceed without the submitted credentials or fail
later with a misleading error, despite successful secret storage.

### 5. Medium — XFS formatting uses an unsupported option

**Status:** Fixed — XFS uses `mkfs.xfs … -f`; ext4 keeps `-F`.

**Location:** `app/linux_verify.py` (`_linux_data_disk_script`)

The shared formatting command used uppercase `-F` (ext4). XFS requires
lowercase `-f`.

**Impact:** Selecting the supported XFS filesystem option failed when formatting
was reached.

## Validation results and limitations

- Targeted unit tests cover findings 1–5 after the fixes above.
- No live Proxmox, Windows, or OPNsense validation is recorded in this file;
  see lab smoke notes / `docs/SMOKE_BACKLOG.md` for live runs.

## Test coverage gaps (remaining)

1. Linux workflow smoke tests still mock `verify_linux_result` for
   orchestration; direct disk-verify coverage is in `tests/test_linux_verify.py`.
2. Default pytest network isolation (so a developer `.env` cannot cause
   infrastructure calls) remains a follow-up.
