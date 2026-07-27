# Windows golden-image checklist

GuestOS only customizes **Proxmox Windows templates** (clone → Sysprep). Use this
checklist before the first Customize / Clone + Sysprep job.

Validated in lab: **Windows Server 2019**, **Windows 11**.

## Required

1. **Install Windows** in a disposable VM (VirtIO drivers recommended for disk/NIC).
2. Install the **QEMU Guest Agent** and confirm it reports online in Proxmox
   (VM → Summary / guest agent).
3. In Proxmox **Options**, enable **QEMU Guest Agent** for the VM.
4. Set QEMU **OS Type** to a Windows value (`win10`, `win11`, …). GuestOS filters
   templates by this `ostype`.
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

## Optional: Configure disks (Server 2019 only)

`manage_disks` attaches/onlines/extends OS, data, and pagefile volumes at
customize time. It is **not** available for Win11 (flat disk layout).

The template must be recognized as Server 2019 via **name or tags**, for example:

- Name contains: `server2019`, `win2019`, `ws2019`, …
- Or tags such as: `guestos-disk`, `guestos-disks`, `server2019`, …

If `manage_disks=true` on a non–2019 template, the job **fails validation**
(no silent skip). Disk customize is exposed in the **GuestOS UI / machine API**,
not in the PDM Customize button.

## What GuestOS does on the clone

1. Clone the template and power on; wait for a stable guest agent.
2. Write `unattended.xml` and `C:\ProgramData\GuestOS\setup.ps1` (and related
   scripts) via the guest agent.
3. Run Sysprep generalize/OOBE; on first logon, `setup.ps1` applies network /
   cleanup / optional domain join.
4. Verify durable setup markers and expected hostname/IP before marking SUCCESS.

If a job fails mid-flight, the clone may remain on the cluster — see
[FAILURE_RUNBOOK.md](FAILURE_RUNBOOK.md).

## Smoke tip

Use a **disposable** template clone (or accept that failed smoke jobs leave
orphaned VMs to delete). Do not point first tests at production golden images
you cannot recreate.
