# Active Directory join validation

GuestOS can join clones to AD from `setup.ps1` using credentials from `DOMAIN_PROFILES_JSON` (or form fields when profile credentials are disabled).

A domain profile under **Network** (single Customize) is only a DNS/VLAN shortcut
(blank fields filled from the profile). **Join domain** is a separate step and
reuses that same profile for credentials when “Use Domain Profile Credentials” is
checked.

**Bulk Win11** does not use the Network profile DNS/VLAN shortcut: DNS is optional
on Basics (blank → DHCP DNS), VLAN comes from each CSV row, and a Domain-step
profile select is used for join credentials only.

For the full **OS / edition** lab matrix (not only AD), see
[VALIDATED_MATRIX.md](VALIDATED_MATRIX.md).

## Lab status (2026-08-12 — GuestOS 2.7.1)

| Check | Result |
|-------|--------|
| Profile JSON loads | OK (2 profiles in lab `.env`) |
| Profile shape (domain, user, password) | OK |
| Placeholder detection | Lab may still have placeholders — replace before production |
| Live Sysprep+join on **Windows Server 2019** (Eval) + disks | **OK** (reconfirmed 2.7.1) |
| Live Sysprep+join + disks on **Windows Server 2022** (VL) | **OK** (reconfirmed 2.7.1) |
| Live Sysprep+join on **Windows 11** (no disks) | **OK** (reconfirmed 2.7.1) |
| Domain reachability preflight (TCP 53/88/389) | **OK** (advisory from GuestOS host; in-clone probe is the hard gate) |

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

## Credential hardening (pre-Sysprep)

GuestOS validates join credentials in layers (similar in spirit to vSphere
customization-spec “test” / Horizon pool credential checks, without Instant ClonePrep).
For a full security and functionality comparison to vSphere Guest Customization
Specifications, see [VMWARE_GAP_ANALYSIS.md](VMWARE_GAP_ANALYSIS.md).

Credential layers:

1. **Normalize** usernames to UPN (`user@domain.tld`) or `DOMAIN\user` (bare names rejected);
   trim trailing `\r`/`\n` on passwords only.
2. **Admit TCP** preflight to DC ports 53/88/389 from the **GuestOS host** —
   **advisory** when unreachable (response `warnings[]`); the guest VLAN may still
   reach AD. Continuing is allowed; in-clone probe is the hard gate.
3. **Admit LDAP** (best-effort from GuestOS host): refuse if the computer account
   for `hostname` already exists **when LDAP is reachable**; validate `domain_ou`
   DN when set and reachable. Unreachable LDAP skips uniqueness/OU with a log
   warning (does not block admit). Wrong join password while LDAP *is* reachable
   still fails admit.
4. **Profile / manual Test credentials** (`POST /api/domain/test_credentials`) —
   host LDAP bind from the GuestOS instance. **Profile credentials always bind
   to that profile's `dns_servers`** (never request DNS). Manual credentials use
   Network-step DNS (required for a meaningful manual test).
   A failed UI test is advisory only — the wizard can continue; failure often means
   routing/firewall between GuestOS and DNS/AD, not necessarily bad passwords.
   In-clone probe remains authoritative for the guest network path.
5. **In-clone QGA probe** after agent-up, before Sysprep write: wait for a non–link-local
   IPv4, then ADSI bind. Kill-switch: `DOMAIN_JOIN_CRED_PROBE=false`.

At probe time the Proxmox NIC (bridge/VLAN) is already configured; the guest IP is
usually still template/DHCP, not the final unattend static address.

Jobs history stores `domain_username` (and profile/domain) but never
`domain_password` / `domain_join_b64`.

Joining during **specialize** uses **Offline Domain Join** (`DOMAIN_JOIN_ODJ`,
default off): the GuestOS host provisions the computer account with Samba's
`net offlinejoin provision` and ships the blob as
`Microsoft-Windows-UnattendedJoin/Identification/Provisioning/AccountData`, so
no credentials go into the answer file and the guest needs no DC contact to
join. Unlike the old credential-based experiment this is **not** DHCP-only.
When provisioning is not possible (host cannot reach a DC, missing rights,
Samba error) the component is omitted and late `Add-Computer` runs as before;
`setup.ps1` skips `Add-Computer` when `PartOfDomain` is already true.
See [OFFLINE_DOMAIN_JOIN.md](OFFLINE_DOMAIN_JOIN.md) for the design and
[UNATTENDED_JOIN_INVESTIGATION.md](UNATTENDED_JOIN_INVESTIGATION.md) for why the
credential-based `JoinDomain` path was abandoned (always `0x52e`, ~15 min cost).

## What “validated” means here

1. **Code path:** unattend + `setup.ps1` domain-join blob packing is covered by unit tests (`tests/test_sysprep.py`, `tests/test_domain_credentials.py`).
2. **Config dry-run:** `scripts/ad_join_validate.py` confirms profiles are complete and optionally resolves DNS.
3. **Live join:** requires a real DC reachable from the guest VLAN, correct DNS in the profile, and a one-time Clone+Sysprep with **Join domain** checked.

## Operator checklist (live)

1. Replace placeholder entries in `DOMAIN_PROFILES_JSON` with your AD domain, join account (UPN or `DOMAIN\user`), DNS IPs, optional VLAN/OU.
2. `python3 scripts/ad_join_validate.py --check-dns --require-real-ad` → must exit 0.
3. Optionally **Test profile credentials** / **Test credentials** in the Customize UI (or `POST /api/domain/test_credentials`).
4. From GuestOS (or PDM Customize), run Clone+Sysprep on a **disposable** template clone:
   - **Single Customize:** Under **Network**, enable **Use domain profile for DNS/VLAN**
     and pick the profile (fills blank DNS/VLAN only — does **not** join AD by itself).
     On the Domain step, enable **Join domain** and **Use Domain Profile Credentials**.
   - **Bulk Win11:** On Basics set optional DNS (or leave blank for DHCP) and CSV rows
     with optional VLAN; on Domain pick the profile for credentials only (no Network
     DNS/VLAN profile control).
   - Prefer DNS that can reach the DC (explicit list, profile DNS on single Customize,
     or DHCP that hands out the DC).
5. After SUCCESS, confirm in the guest: domain membership + reboot completed.
6. Keep this table updated when re-validating Server / Win11.

## Security

Join passwords stay server-side when using profile credentials; they are never sent to the browser.
On the guest (2.6.7+), `setup.ps1` scrubs `SetupPs1B64` / `C:\GuestOS\setup.ps1` after join so the AD password is not left on disk.
