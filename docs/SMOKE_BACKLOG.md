# Lab smoke backlog (long-term)

Persistent checklist for the **next** lab smoke round(s). Do not clear items
when committing unrelated work — mark them done only after a live lab run
confirms the check.

When running smoke tests, also see [VALIDATED_MATRIX.md](VALIDATED_MATRIX.md)
and [AD_VALIDATION.md](AD_VALIDATION.md).

## Open

| ID | Item | Why | How to verify |
|----|------|-----|----------------|
| `ntp-pve-localtime` | Proxmox `localtime=1` on Windows templates | RTC semantics for Windows guests; currently unset on lab templates. | After setting on template(s), re-clone smoke once and confirm no clock jump vs domain. |

## Done (recent rounds)

| ID | Item | Confirmed |
|----|------|-----------|
| `win11-bulk-ad` | Win11 bulk 2× VDI DHCP + AD (`lab_bulk_win11_ad_smoke.py`) | 2026-08-02 — `VDI698941A`→144, `VDI698941B`→142 SUCCESS (domain joined) |
| `secrets-stash-2022` | Admin/domain secrets side-channel + 2022 VL full smoke | 2026-08-02 — `S22S185146` SUCCESS |
| `eval-failclosed-2019` | Eval skip GVLK + fail-closed unknown edition | 2026-08-02 — `S19S185702` SUCCESS |
| `win11-domain-nodisk` | Win11 join without disks | 2026-08-02 — `W11S190319` SUCCESS |
| `recycle-bin-reset` | `$RECYCLE.BIN` reset after data volume setup | Code added 2026-08-02; live confirm on next disk smoke (optional note in verify) |
| `lab-ram-preflight` | Refuse lab smoke if PVE free RAM &lt; guests + 4 GiB reserve | 2026-08-02 — `scripts/lab_smoke_preflight.py` wired into all `lab_*_smoke.py` |
| `linux-static-detach` | Linux static IP + detach cloud-init + OS disk resize | 2026-08-11 — guestos-lab `192.168.123.197` VMID 123 SUCCESS (static `192.168.123.200`; froze cloud-init `ide0`) |
| `linux-multinic` | Linux 2-NIC (v4 + v6) smoke | 2026-08-11 — guestos-lab `192.168.123.197` VMID 124 SUCCESS (static `192.168.123.201`; IPv6 `fd00::20`) |

## Suggested next smoke sequence (when ready)

1. Optionally set Proxmox `localtime=1` on Windows templates and re-check (**`ntp-pve-localtime`**).
2. Optionally re-check Recycle Bin on D:/E: after disk smoke.
3. Update this file: move open → done with host/VMID/date.
