# Lab smoke backlog (long-term)

Persistent checklist for the **next** lab smoke round(s). Do not clear items
when committing unrelated work — mark them done only after a live lab run
confirms the check.

When running smoke tests, also see [VALIDATED_MATRIX.md](VALIDATED_MATRIX.md)
and [AD_VALIDATION.md](AD_VALIDATION.md).

## Open

| ID | Item | Why | How to verify |
|----|------|-----|----------------|
| `ntp-dc-resync` | **NTP / domain time after join** | DC (lab VM 103) had wrong UTC + Pacific TZ; members looked “wrong” on CEST while following NT5DS. `setup.ps1` now best-effort `w32tm /resync /force` after `Add-Computer`. | Fix DC timezone + real NTP first. Then one AD-join smoke (2019 or 2022 or Win11). On guest: `w32tm /query /status`, compare UTC to PVE host / phone. Guest local time should match wall clock in GuestOS timezone. |
| `ntp-pve-localtime` | Proxmox `localtime=1` on Windows templates | RTC semantics for Windows guests; currently unset on lab templates. | After setting on template(s), re-clone smoke once and confirm no clock jump vs domain. |

## Done (recent rounds)

| ID | Item | Confirmed |
|----|------|-----------|
| `secrets-stash-2022` | Admin/domain secrets side-channel + 2022 VL full smoke | 2026-08-02 — `S22S185146` SUCCESS |
| `eval-failclosed-2019` | Eval skip GVLK + fail-closed unknown edition | 2026-08-02 — `S19S185702` SUCCESS |
| `win11-domain-nodisk` | Win11 join without disks | 2026-08-02 — `W11S190319` SUCCESS |
| `recycle-bin-reset` | `$RECYCLE.BIN` reset after data volume setup | Code added 2026-08-02; live confirm on next disk smoke (optional note in verify) |

## Suggested next smoke sequence (when ready)

1. Confirm DC clock/TZ/NTP fixed (`w32tm /query /status` on DC → not LOCL; TZ correct).
2. Run one full AD smoke (**2022** or **2019**) and check guest time (**`ntp-dc-resync`**).
3. Optionally re-check Recycle Bin on D:/E: after disk smoke.
4. Update this file: move open → done with host/VMID/date.
