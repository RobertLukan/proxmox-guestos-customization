# Windows golden-image checklist

GuestOS only customizes **Proxmox Windows templates** (clone → Sysprep). Use this
checklist before the first Customize / Clone + Sysprep job.

Validated in lab: see **[VALIDATED_MATRIX.md](VALIDATED_MATRIX.md)** (versions +
editions). Short form: Server **2019 Standard Evaluation**, Server **2022
Standard** (VL), **Windows 11**. Other years/editions (2025, Datacenter, 2016,
VL 2019, …) share the same code path — community test reports wanted.

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
- The wizard lists **one set of unique bridge names** from the cluster (not a
  per-host dump). Keep bridge / SDN VNet names **identical on every node**
  where clones may land so the chosen bridge exists on the target host.
- **Proxmox SDN:** after you create a VNet and click Apply, it appears as a
  Linux bridge with that VNet’s name. Set `PRIMARY_BRIDGE` / wizard bridge to
  the **VNet name**. Leave GuestOS **VLAN** empty unless the VNet is
  VLAN-aware and you intentionally need a guest tag (many SDN zones reject
  `tag=` on the NIC). DNS/gateway/AD must be reachable **on that VNet**.
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

The UI **disk planner** inventories the template (`GET /api/templates/<vmid>/disks`),
shows bus keys and current sizes, and lets the admin assign each secondary as
**Data**, **Pagefile**, or **Leave as-is**, swap roles, and grow target sizes
(never shrink). The submitted `disks[]` plan may include `source_key` (e.g.
`scsi2`) so reconcile binds the chosen slot on the clone. Matching order:
`source_key` → serial → size best-fit → attach new. There is **no** silent
server-side 16/50 GB default — the job must send an explicit plan.

The template must be recognized as **Windows Server** via **name or tags**, for example:

- Name contains: `server2019`, `server2022`, `server2025`, `win2019`, `ws2022`, …
- Or tags such as: `windowsserver2019`, `windowsserver2022`, `windowsserver2025`,
  `guestos-disk`, `guestos-disks`, `server2022`, …

If `manage_disks=true` on a non-Server template, the job **fails validation**
(no silent skip). Disk customize is exposed in the **GuestOS UI / machine API**,
not in the PDM Customize button.

**EFI / TPM:** UEFI templates may include `efidisk0` and optionally `tpmstate0`.
Those firmware volumes are **not** treated as OS/data/pagefile disks and are
left alone. Configure disks allocates new `scsi`/`virtio`/… bus slots for
pagefile/data when needed, or reuses existing non-boot disks via the planner
`source_key` / serial / size match.
## What GuestOS does on the clone

1. Clone the template and power on; wait for a stable guest agent.
2. For Windows Server **volume-license** templates, detect edition and inject a
   Microsoft GVLK into unattend specialize `ProductKey` when the request omits
   `product_key`, so OOBE does not stop on Enter product key. (Unattend cannot
   click **Do this later**; empty Setup `ProductKey`/`WillShowUI` in specialize
   fails.) Pass `product_key` to use your own key instead.
   **Evaluation** images never get a VL GVLK — that combination fails OOBE
   `InstallPid` (`0xC004F015`) and blocks FirstLogon (regression found when
   Server 2022 GVLK auto-inject was added; fixed in 2.6.3 — see
   [FAILURE_RUNBOOK.md](FAILURE_RUNBOOK.md#evaluation-vs-gvlk-server-2019-regression)).
3. Write `unattended.xml` and persist `setup.ps1` as
   `HKLM\SOFTWARE\GuestOS\SetupPs1B64` (specialize deletes loose Sysprep/ProgramData
   copies; embedding in unattend hung Sysprep). FirstLogon extracts and runs it.
   Large staging writes use chunked guest-agent transfers.
4. Run Sysprep generalize/OOBE; on first logon, `setup.ps1` applies network /
   cleanup / optional domain join / disk letters.
5. Verify durable setup markers and expected hostname/IP before marking SUCCESS.

If a job fails mid-flight, the clone may remain on the cluster — see
[FAILURE_RUNBOOK.md](FAILURE_RUNBOOK.md).

## Smoke tip

Use a **disposable** template clone (or accept that failed smoke jobs leave
orphaned VMs to delete). Do not point first tests at production golden images
you cannot recreate. Lab-validated: Server 2019 Eval, Server 2022 VL, Win11.
For Server 2025, run at least one single Customize before production.
