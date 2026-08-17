# Validated Windows versions and editions

GuestOS supports a **Windows Server family** (2016–2025) and **Windows 11** in
code. The maintainer lab cannot cover every year × edition × channel. This page
is the single source of truth for **what has been live-tested** vs **what still
needs community reports**.

**GuestOS release when this matrix was last updated:** see [VERSION](../VERSION)
(matrix date below). Prefer current `main` / latest GHCR tag when testing.

**Matrix date:** 2026-08-16 (GuestOS **2.8.0** lab; Win11 static+AD via ODJ)

## What “lab OK” means

A row marked **OK** means a disposable template clone completed Clone + Sysprep
to task `SUCCESS` with the features listed (typically DHCP, optional AD join,
optional Server disks). It does **not** mean Microsoft support, activation
licensing advice, or production certification.

| Symbol | Meaning |
|--------|---------|
| **OK** | Live lab success on maintainer hardware |
| **Code** | Supported in GuestOS (tags/GVLK/unattend branches); **no live report yet** |
| **—** | Out of scope or not applicable |

## Lab-validated (maintainer)

| OS | Channel / edition (as seen by guest) | Tag used | DHCP | Static IP | AD join | Configure disks | Notes |
|----|--------------------------------------|----------|------|-----------|---------|-----------------|-------|
| Windows Server **2019** | **Standard Evaluation** (`ServerStandardEval`) | `windowsserver2019` | OK | OK | OK | OK | Eval path (no VL GVLK); DHCP+AD+disks reconfirmed 2.7.1; static `192.168.123.210` + AD+disks 2026-08-14 (`S19ST224349`) |
| Windows Server **2022** | **Standard** volume-license (`ServerStandard`) | `windowsserver2022` | OK | — | OK | OK | Auto GVLK for OOBE; reconfirmed 2.7.1 (DHCP+AD+disks) |
| Windows Server **2025** | **Datacenter Evaluation** | `windowsserver2025` | — | OK | — | — | Eval path; static IP smoke 2026-08 |
| Windows **11** | Desktop / VDI template | `windows11` | OK | OK | OK | — | Disks N/A (Win11); DHCP+AD reconfirmed 2.7.1; static `192.168.123.211` + AD 2026-08-16 (`W11ST191727`, ODJ). **25H2** (`26200.8894`) DHCP+AD empty OU 2026-08-17 (`W25E190624` VMID 123, `add-computer`) |

Lab smoke helpers: `scripts/lab_full_feature_smoke.py` (see [AD_VALIDATION.md](AD_VALIDATION.md)).

## Lab-validated Linux (maintainer)

| OS | Template notes | DHCP | Static IP | OS disk grow | Freeze/detach cloud-init | Multi-NIC | Notes |
|----|----------------|------|-----------|--------------|--------------------------|-----------|-------|
| **Ubuntu 24.04** cloud | `ostype` `l26`, QEMU GA | — | OK | OK | OK | OK | 2.7.1 lab 2026-08-12 (`scripts/lab_linux_smoke.py`); multi-NIC smoked 2026-08-11 |

See [LINUX_TEMPLATE.md](LINUX_TEMPLATE.md).

## Supported in code — community testing wanted

These are first-class in tags / GVLK tables / Server disk path, but the
maintainer has **not** run a full live Sysprep+verify (or has only partial
coverage). **Please report** success or failure.

| OS | Edition / channel | GuestOS support | Priority ask |
|----|-------------------|-----------------|--------------|
| Server **2019** | Standard (VL / MAK / KMS client) | Code (GVLK Standard) | High — mirror of Eval lab, VL SKU |
| Server **2019** | Datacenter (VL or Eval) | Code (GVLK DC / Eval skip) | High |
| Server **2022** | Datacenter (VL or Eval) | Code | High |
| Server **2022** | Standard **Evaluation** | Code (Eval skip) | Medium |
| Server **2025** | Standard / Datacenter **VL** | Code (tags + GVLK) | High — VL SKUs; AD + disks not smoked |
| Server **2025** | Datacenter Eval + AD and/or disks | Partial lab (static only) | High — extend the Eval row |
| Server **2016** | Standard / Datacenter (VL or Eval) | Code (GVLK 2016) | Medium |
| Windows **11** | Other builds / editions than the lab template | Code (Win11 family) | Medium — note build |
| Windows **10** | Desktop | Not a separate family | Low — may work as `win10` ostype; report if used |

**Not requested (unless you hit a bug):** in-place Sysprep of existing VMs
(disabled), Server disk customize on Win11.

## How to report a test result

Open a GitHub issue (or comment on an existing matrix issue) with:

1. **GuestOS version** (`GET /api/version` → `version` + `build_time` if present).
2. **Windows:** year, edition/channel (Eval vs VL), caption from WMI if known  
   (`Win32_OperatingSystem.Caption` / `OperatingSystemSKU` / edition id).
3. **Proxmox template tag(s)** and approximate guest build number.
4. **Features exercised:** DHCP or static; AD join yes/no; `manage_disks` yes/no.
5. **Outcome:** `SUCCESS` / `FAILURE` + task message or worker log lines  
   (`Using Server GVLK…` / `Skipping GVLK for Evaluation…` / errors).
6. Optional: confirm hostname, domain membership, drive letters after SUCCESS.

Suggested issue title:

```text
Validation: Server 2025 Standard VL — SUCCESS (DHCP + AD, no disks)
```

or

```text
Validation: Server 2019 Datacenter Eval — FAILURE at 98% (paste log)
```

Maintainers will update this matrix when reports are confirmed.

## Feature coverage (lab)

| Feature | 2019 Eval | 2022 Standard VL | 2025 Datacenter Eval | Win11 | Ubuntu 24.04 |
|---------|-----------|------------------|----------------------|-------|--------------|
| Clone + customize + verify | OK | OK | OK | OK | OK (cloud-init) |
| DHCP | OK | OK | — | OK | — |
| Static IP | OK | — | OK | OK | OK |
| AD join (`lab.test`) | OK | OK | — | OK | N/A |
| Disks (OS + pagefile + data) | OK | OK | — | N/A | OS grow OK |
| Bulk Win11 batch | — | — | — | Lab-exercised separately | N/A |
| IPv6 / multi-NIC | Not smoked | Not smoked | Not smoked | Not smoked | Lab OK (2-NIC) |

Static IP is lab-OK on Server **2019** Eval (DHCP+AD path also OK; static+AD+disks
2026-08-14), **Win11** (`192.168.123.211` + AD, ODJ, 2026-08-16), Server **2025**
Datacenter Eval, and Ubuntu. Windows IPv6/multi-NIC remain unit-covered only.

## Related docs

- Golden image prep: [WINDOWS_TEMPLATE.md](WINDOWS_TEMPLATE.md), [LINUX_TEMPLATE.md](LINUX_TEMPLATE.md)
- AD join checklist: [AD_VALIDATION.md](AD_VALIDATION.md)
- vs vSphere Customization Spec: [VMWARE_GAP_ANALYSIS.md](VMWARE_GAP_ANALYSIS.md)
- Next-round smoke backlog (NTP, etc.): [SMOKE_BACKLOG.md](SMOKE_BACKLOG.md)
- Eval vs GVLK: [FAILURE_RUNBOOK.md](FAILURE_RUNBOOK.md#evaluation-vs-gvlk-server-2019-regression)
- Failure triage: [FAILURE_RUNBOOK.md](FAILURE_RUNBOOK.md)
