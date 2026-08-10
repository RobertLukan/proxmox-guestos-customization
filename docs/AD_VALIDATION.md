# Active Directory join validation

GuestOS can join clones to AD from `setup.ps1` using credentials from `DOMAIN_PROFILES_JSON` (or form fields when profile credentials are disabled).

A domain profile under **Network** is only a DNS/VLAN shortcut (blank fields filled
from the profile). **Join domain** is a separate step and reuses that same profile
for credentials when “Use Domain Profile Credentials” is checked.

For the full **OS / edition** lab matrix (not only AD), see
[VALIDATED_MATRIX.md](VALIDATED_MATRIX.md).

## Lab status (2026-08-02)

| Check | Result |
|-------|--------|
| Profile JSON loads | OK (2 profiles in lab `.env`) |
| Profile shape (domain, user, password) | OK |
| Placeholder detection | Lab may still have placeholders — replace before production |
| Live Sysprep+join on **Windows Server 2019** (Eval) | **OK** (confirmed after Eval/GVLK fix in 2.6.3) |
| Live Sysprep+join + disks on **Windows Server 2022** (VL) | **OK** (confirmed; GVLK path) |
| Live Sysprep+join on **Windows 11** (no disks) | **OK** (confirmed; verify uses PowerShell CIM — WMIC removed on Win11) |

Dry-run command used:

```bash
python3 scripts/ad_join_validate.py --env-file .env --check-dns
# exit 1 with placeholder profiles is expected (issues>0)
python3 scripts/ad_join_validate.py --env-file .env --require-real-ad
# exit 3 until real AD is configured
```

Full-feature lab smoke (domain + optional disks):

```bash
# Server 2022 VL + disks
python3 scripts/lab_full_feature_smoke.py --template-vmid 130 --poll
# Server 2019 Eval + disks
python3 scripts/lab_full_feature_smoke.py --template-vmid 120 --poll
# Win11 + domain, no disk customize
python3 scripts/lab_full_feature_smoke.py --template-vmid 127 --no-disks --poll
# Win11 bulk: 2 VDIs, DHCP, DNS=lab DC, AD join
python3 scripts/lab_bulk_win11_ad_smoke.py --poll
```

Lab smokes refuse to start when Proxmox free RAM is below
`(guests × ram) + 4 GiB` reserve (`scripts/lab_smoke_preflight.py`).
Override only with `--skip-ram-check` / `LAB_SMOKE_SKIP_RAM_CHECK=1`.

## What “validated” means here

1. **Code path:** unattend + `setup.ps1` domain-join blob packing is covered by unit tests (`tests/test_sysprep.py`).
2. **Config dry-run:** `scripts/ad_join_validate.py` confirms profiles are complete and optionally resolves DNS.
3. **Live join:** requires a real DC reachable from the guest VLAN, correct DNS in the profile, and a one-time Clone+Sysprep with **Join domain** checked.

## Operator checklist (live)

1. Replace placeholder entries in `DOMAIN_PROFILES_JSON` with your AD domain, join account, DNS IPs, optional VLAN/OU.
2. `python3 scripts/ad_join_validate.py --check-dns --require-real-ad` → must exit 0.
3. From GuestOS (or PDM Customize), run Clone+Sysprep on a **disposable** template clone:
   - Under **Network**, enable **Use domain profile for DNS/VLAN** and pick the profile
     (fills blank DNS/VLAN only — does **not** join AD by itself).
   - On the Domain step, enable **Join domain** and **Use Domain Profile Credentials**
     (reuses the same Network profile for join account/password).
   - Prefer static IP/DNS that can reach the DC (or DHCP + profile DNS).
4. After SUCCESS, confirm in the guest: domain membership + reboot completed.
5. Keep this table updated when re-validating Server / Win11.

## Security

Join passwords stay server-side when using profile credentials; they are never sent to the browser.
On the guest (2.6.7+), `setup.ps1` scrubs `SetupPs1B64` / `C:\GuestOS\setup.ps1` after join so the AD password is not left on disk.
