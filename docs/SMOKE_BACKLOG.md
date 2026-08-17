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
| `win11-25h2-empty-ou` | Win11 **25H2** DHCP+AD, blank Target OU, ODJ off | 2026-08-17 — guestos-lab template `121` `WIn1125H2-1`; clone `W25E190624` VMID **123** SUCCESS. GuestOS 2.7.1; `join-path: add-computer`; DHCP `192.168.123.161`; `DisplayVersion=25H2` build `26200.8894`. Empty OU created `CN=W25E190624,CN=Computers,DC=lab,DC=test` (no “is not an OU”). Join user was lab `administrator@lab.test` (Domain Admin) — does **not** reproduce production `SCCMJoin` + 24H2 `0x2`. Clone left running. |
| `odj-fallback` | Fallback when GuestOS cannot reach the DC | 2026-08-16 — `W11FB193536` VMID 121 SUCCESS. Worker `join-path: host_dc=unreachable` and `provision=add-computer`; guest `pending_reboot (domain-join)`; verify `domain[lab.test]: joined (lab.test, add-computer, host DC unreachable)`; DHCP `192.168.123.164`. |
| `odj-static-win11` | Offline Domain Join on Win11 + static IP + AD | 2026-08-16 — guestos-lab `W11ST191727` VMID 121 SUCCESS. `join-path: provision=odj`; verify `IP 192.168.123.211 present`; `domain[lab.test]: joined (lab.test, odj, host DC reachable)`; `already-joined` then `setup.done`. |
| `uac-rid500-logon` | First console logon as `LAB\Administrator` after `FilterAdministratorToken=1` | 2026-08-16 — `W11ODJ185325` VMID 121. `FilterAdministratorToken=1`; first logon as `LAB\Administrator` got a desktop (`explorer.exe` session 1). |
| `odj-static-s2019` | Offline Domain Join on Server 2019 Eval + static IP + AD | 2026-08-16 — guestos-lab `S19ODJ152322` VMID 123 SUCCESS. Worker `ODJ: provisioned`; `already-joined` then `setup.done`; static `192.168.123.210`; disks P:/D:/E: ok. |
| `odj-dhcp-s2022` | Offline Domain Join on Server 2022 VL + DHCP + AD | 2026-08-16 — guestos-lab `S22ODJ151539` VMID 123 SUCCESS (fast waits 30s/15s; 24h first-boot trigger, no task kick). Worker `ODJ: provisioned`; guest `already-joined` then `setup.done`; DHCP `192.168.123.162`; disks P:/D:/E: ok. First console logon as `administrator@lab.test` worked. Earlier `S22ODJ145513` needed a manual `schtasks /Run` after the 2h window expired. |
| `odj-dhcp-win11` | Offline Domain Join on Win11 + DHCP + AD | 2026-08-16 — `W11ODJ111506` join; `W11ODJ114420` LogonUI; `W11ODJ120940` VMID 121 SUCCESS with `pending_reboot`/`already-joined` then `ok-after-reboot`. Console `LogonUI`, no `FirstLogonAnim`. |
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
