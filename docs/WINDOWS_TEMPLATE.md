# Windows golden-image checklist

GuestOS only customizes **Proxmox Windows templates** (clone → Sysprep). Use this
checklist before the first Customize / Clone + Sysprep job.

Validated in lab: **Windows Server 2019**, **Windows 11**.  
Supported Server family (same code path): **2019 / 2022 / 2025** — tag templates
and run a smoke job before production use.

## Required

1. **Install Windows** in a disposable VM (VirtIO drivers recommended for disk/NIC).
2. Install the **QEMU Guest Agent** and confirm it reports online in Proxmox
   (VM → Summary / guest agent).
3. In Proxmox **Options**, enable **QEMU Guest Agent** for the VM.
4. Set QEMU **OS Type** to a Windows value (`win10`, `win11`, …). GuestOS filters
   templates by this `ostype`. Proxmox does **not** encode Server year in `ostype`.
5. Set a known local **Administrator** password (GuestOS Sysprep will set a new
   password from the wizard / API payload).
6. Prefer a **clean** image: few extra local users, no half-finished domain join,
   guest agent healthy after reboot.
7. **Do not** leave the golden image already generalized in a broken state.
   GuestOS runs `sysprep /generalize /oobe` on the **clone**, not on the template.
8. **Convert to template** in Proxmox (right-click → Convert to template).

## Network readiness

- Clones attach to `PRIMARY_BRIDGE` (or the bridge chosen in the wizard).
- Static IP customize needs a free address, correct prefix/gateway/DNS.
- Domain join needs DNS that can resolve the domain controller (see
  [AD_VALIDATION.md](AD_VALIDATION.md)).

## Template tags (resource family)

GuestOS classifies caps primarily from Proxmox **tags**:

| Tag | Family | Caps (defaults) |
|-----|--------|-----------------|
| `windows11` | Win11 / VDI | 8 cores, 64 GB RAM, 600 GB requested disks |
| `windowsserver2019` / `windowsserver2022` / `windowsserver2025` (or any `windowsserver*`) | Server | 16 cores, 64 GB RAM, 2 TB requested disks |

Lab examples: template **127** → `windows11`; template **120** → `windowsserver2019`.

Also accepted: name heuristics (`server2022`, `ws2025`, …) and tags such as
`guestos-disk`, `server2019`, `server2022`, `server2025`, `win11`, `vdi`.

## Optional: Configure disks (Windows Server family)

`manage_disks` attaches/onlines/extends OS, data, and pagefile volumes at
customize time. It is **not** available for Win11 (flat disk layout) — the Disks
wizard step is hidden for `windows11` templates and the API rejects
`manage_disks=true` for non-Server families.

The template must be recognized as **Windows Server** via **name or tags**, for example:

- Name contains: `server2019`, `server2022`, `server2025`, `win2019`, `ws2022`, …
- Or tags such as: `windowsserver2019`, `windowsserver2022`, `windowsserver2025`,
  `guestos-disk`, `guestos-disks`, `server2022`, …

If `manage_disks=true` on a non-Server template, the job **fails validation**
(no silent skip). Disk customize is exposed in the **GuestOS UI / machine API**,
not in the PDM Customize button.

**EFI / TPM:** UEFI templates may include `efidisk0` and optionally `tpmstate0`.
Those firmware volumes are **not** treated as OS/data/pagefile disks and are
left alone. Configure disks only allocates new `scsi`/`virtio`/… bus slots for
pagefile/data (or reuses existing non-boot bus disks with matching serials).

## What GuestOS does on the clone

1. Clone the template and power on; wait for a stable guest agent.
2. For Windows Server templates, detect edition and inject a Microsoft GVLK into
   unattend specialize `ProductKey` when the request omits `product_key`, so OOBE
   does not stop on Enter product key. (Unattend cannot click **Do this later**;
   empty Setup `ProductKey`/`WillShowUI` in specialize fails.) Pass `product_key`
   to use your own key instead.
3. Write `unattended.xml` and `C:\ProgramData\GuestOS\setup.ps1` (and related
   scripts) via the guest agent. Large `setup.ps1` (disk plans) is written in
   chunks to stay under QEMU guest-agent command-line limits.
4. Run Sysprep generalize/OOBE; on first logon, `setup.ps1` applies network /
   cleanup / optional domain join / disk letters.
5. Verify durable setup markers and expected hostname/IP before marking SUCCESS.

If a job fails mid-flight, the clone may remain on the cluster — see
[FAILURE_RUNBOOK.md](FAILURE_RUNBOOK.md).

## Smoke tip

Use a **disposable** template clone (or accept that failed smoke jobs leave
orphaned VMs to delete). Do not point first tests at production golden images
you cannot recreate. For Server 2022/2025, run at least one single Customize
(static or DHCP) before enabling `manage_disks` or AD join in production.
