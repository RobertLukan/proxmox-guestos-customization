# UnattendedJoin (specialize) — `0x52e` investigation

Working notes for the DHCP-only `Microsoft-Windows-UnattendedJoin` experiment
(`unattend_join`, see [AD_VALIDATION.md](AD_VALIDATION.md)). The goal is joining
AD during the `specialize` pass instead of late `Add-Computer` in `setup.ps1`.

**Status (2026-08-16): closed — credential-based UnattendedJoin abandoned.**
It failed with `0x52e` on every attempt, the element-order hypothesis was tested
and disproven (run 4), and `TimeoutPeriodInMinutes` turned out to be ignored, so
each attempt cost ~15 minutes of `specialize`. The specialize join is now done
with Offline Domain Join instead, which carries no credentials and cannot
produce a logon failure — see [OFFLINE_DOMAIN_JOIN.md](OFFLINE_DOMAIN_JOIN.md).
Late `Add-Computer` remains the fallback and joined successfully in all runs.

The evidence below is kept because it rules out most of the usual suspects, and
because anyone re-attempting a credential-based join will retrace these steps.

## Symptom

`C:\Windows\Panther\UnattendGC\setupact.log` during `specialize`:

```
[DJOIN.EXE] Unattended Join: Calling DsGetDcName for lab.test...
[DJOIN.EXE] Unattended Join: DsGetDcName returned [WIN-OP4EHT86289.lab.test]
[DJOIN.EXE] Unattended Join: Constructed domain parameter [lab.test\WIN-OP4EHT86289.lab.test]
[DJOIN.EXE] Unattended Join: NetJoinDomain attempt failed: 0x52e, will retry in 10 seconds...
```

`C:\Windows\debug\NetSetup.LOG` for the same attempt:

```
NetpJoinDomain
  HostName: W11UJ211057
  Domain: lab.test\WIN-OP4EHT86289.lab.test
  Account: lab.test\administrator
  Options: 0x23
NetpDisableIDNEncoding: no domain dns available - IDN encoding will NOT be disabled
NetUseAdd to \\WIN-OP4EHT86289.lab.test\IPC$ returned 1326
```

`0x52e` = 1326 = `ERROR_LOGON_FAILURE`. DJOIN retries every 10 s for ~15 minutes,
then gives up — so a failed UnattendedJoin currently adds ~15 minutes to every
DHCP + AD deploy before OOBE even starts.

The later `Add-Computer` in `setup.ps1` succeeds on a different code path:

```
NetpJoinDomain  Domain: lab.test  Account: administrator@lab.test  Options: 0x3
NetpDsGetDcName: found DC \\WIN-OP4EHT86289.lab.test
NetpDisableIDNEncoding: using FQDN lab.test from dcinfo
NetpJoinDomainOnDs: status of connecting to dc '\\WIN-OP4EHT86289.lab.test': 0x0
NetpLdapBind: Verified minimum encryption strength
```

Note the difference: the failing path authenticates over **SMB (`NetUseAdd` to
`IPC$`)**, the working path over **LDAP**. `Options: 0x23` vs `0x3` is only
`NETSETUP_DOMAIN_JOIN_IF_JOINED` (0x20) and is not significant.

## What has been ruled out

Verified against lab DC `WIN-OP4EHT86289.lab.test` / `192.168.123.191`,
domain `lab.test`, join account `administrator`.

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Wrong username shape in the answer file | **Ruled out** | Three runs with `lab.test\administrator`, `administrator@lab.test`, and `LAB` + `administrator` all failed identically with `0x52e`; the DJOIN parameter dump confirms each value arrived. |
| Bad password | **Ruled out** | Same secret joins via `Add-Computer` minutes later, and authenticates over SMB (below). |
| DC blocks NTLM (Restrict NTLM / Protected Users) | **Ruled out** | From a booted guest, `net use \\192.168.123.191\IPC$ /user:LAB\administrator` (by IP, so NTLM) returns RC=0. |
| Account restricted for network logon | **Ruled out** | SMB `IPC$` succeeds with both `LAB\administrator` and `administrator@lab.test`; LDAP bind also succeeds. |
| DNS / DC discovery broken at specialize | **Ruled out** | `DsGetDcName` returns the DC FQDN before the failure. |
| Network unreachable at specialize | **Ruled out** | Failure is at SMB session setup, not connect — the DC answered. |
| Clock skew breaking Kerberos | **Ruled out (probable)** | Panther logged specialize at `22:28:37` local while the run started `22:23` host time, so the guest clock was already correct. NTLM is not time-sensitive anyway. |
| Legacy LM/NTLM policy on the guest | **Ruled out** | Guest `LmCompatibilityLevel` unset (default), `NoLmHash=1`, `RestrictSendingNTLMTraffic` unset. |

Probe used (via QGA against the still-running smoke guest):

```
net use \\192.168.123.191\IPC$ /user:LAB\administrator <pw>          -> RC=0
net use \\WIN-OP4EHT86289.lab.test\IPC$ /user:administrator@lab.test -> RC=0
net use \\WIN-OP4EHT86289.lab.test\IPC$ /user:LAB\administrator      -> RC=0
DirectoryEntry LDAP://WIN-OP4EHT86289.lab.test/DC=lab,DC=test        -> bind OK
```

Build the UNC and `DOMAIN\user` strings with `([char]92)` inside PowerShell —
literal backslashes get eaten somewhere in the QGA/PVE exec path and you end up
testing `\192.168.123.191\IPC$` (System error 67) instead.

## Tested and disproven: answer-file element order

The unattend schema defines `Identification` and `Credentials` as **ordered
sequences**. Per Microsoft's documentation the order is `Credentials`,
`JoinDomain`, `MachineObjectOU`, and inside `Credentials` it is `Domain`,
`Password`, `Username`:

```xml
<Identification>
   <Credentials>
      <Domain>fabrikam.com</Domain>
      <Password>MyPassword</Password>
      <Username>MyUserName</Username>
   </Credentials>
   <JoinDomain>fabrikam.com</JoinDomain>
</Identification>
```

Our template emitted `JoinDomain`, `MachineObjectOU`, then `Credentials` with
`Domain`, `Username`, `Password` — wrong at both levels. A sequence parser reads
`Domain`, then meets `Username` where `Password` was expected; it can accept
`Username` and leave the trailing `Password` unmatched, i.e. DJOIN authenticates
with an empty password and the DC returns `ERROR_LOGON_FAILURE`.

This fits everything observed: `Domain` and `Username` show up populated in the
DJOIN dump (they are matched), the DC is reached, and only authentication fails.
Run 2 omitted `<Domain>` entirely and still had `Username` before `Password`, so
the same mismatch applies.

Caveat: DJOIN masks secrets in its log (`Password = [secret not logged]`), so an
empty password cannot be confirmed from logs. It prints the same string for
`MachinePassword`, a field we never set, which suggests the mask is
unconditional and not evidence that a value is present.

**Result (run 4, 2026-08-16): this did not fix it.** The template now emits the
documented order with a UPN username and no `Domain` element, and DJOIN confirms
it read exactly that:

```
Unattended Join: Domain = [NULL]
Unattended Join: Username = [administrator@lab.test]
Unattended Join: TimeoutPeriodInMinutes = [3]
Unattended Join: DsGetDcName returned [WIN-OP4EHT86289.lab.test]
Unattended Join: NetJoinDomain attempt failed: 0x52e, will retry in 10 seconds...
```

So the ordering was genuinely wrong and is now fixed, but it was not the cause.
Keep the corrected order — it removes a real defect — and look elsewhere.

### `TimeoutPeriodInMinutes` does not work

Run 4 set `TimeoutPeriodInMinutes` to 3 and DJOIN logged it as `[3]`, yet the
retry loop still ran from 11:48:20 to at least 12:02:02 (~14 minutes), the same
as with the setting absent. The knob is parsed but does not bound the retries,
so it cannot be used to cap the cost of a failed experiment. The only reliable
way to avoid the ~15 minute specialize stall is to not emit the
`Microsoft-Windows-UnattendedJoin` component at all.

While DJOIN is looping, specialize has not completed, so the guest still carries
the OOBE-generated random computer name (e.g. `WIN-GK50I6CQN9P`) rather than the
requested hostname. That is expected mid-run, not a separate bug.

## Documented knobs we were not using

- `TimeoutPeriodInMinutes` — caps the retry loop. Currently `[NULL]`, which is
  why a failure costs ~15 minutes.
- `DebugJoin` + `DebugJoinOnlyOnThisError` — extra diagnostics on a chosen error
  code (e.g. `0x52e`).
- `UnsecureJoin` / `MachinePassword` — pre-created account join, not used here.
- `Provisioning` / `AccountData` — offline domain join blob (see below).
- Per the docs, when `Username` is a UPN or `DOMAIN\user`, `Domain` **must** be
  omitted. Do not send both shapes.

## Ideas considered, and the decision

1. ~~**Fix the element order**~~ — done in run 4, did not fix `0x52e`. Kept
   anyway, since the ordering really was wrong.
2. ~~**Cap the blast radius** with `TimeoutPeriodInMinutes`~~ — parsed but
   ignored (see above). Only removing the component avoids the stall.
3. **Decide value-vs-environment** — a temporary specialize
   `RunSynchronousCommand` running `net use` against the DC with the same literal
   password would prove whether the answer file mangles the credential. Not
   pursued: it writes the password into Panther logs, and ODJ makes the question
   moot.
4. **Enable `DebugJoin` for `0x52e`** — same reasoning; available if anyone
   revisits this.
5. **Offline Domain Join — chosen.** Pre-create the computer account and pass an
   `AccountData` blob via `Provisioning`. No credentials in the guest, so it
   cannot fail on logon, and it works for static-IP guests too. Samba 4.15+
   provides `net offlinejoin provision`, so the blob can be generated from the
   Linux control plane. See [OFFLINE_DOMAIN_JOIN.md](OFFLINE_DOMAIN_JOIN.md).
6. **Run `Add-Computer` from a specialize `RunSynchronousCommand`** — would use
   the LDAP path that always works, but keeps the domain credential in the guest.
   Reasonable fallback if ODJ does not pan out in lab.

For reference, vSphere Guest Customization drives the *same* credential-based
`DJOIN.EXE` at specialize, so this failure class is not unique to GuestOS —
Broadcom's KB on customization not completing shows the identical retry loop.
Choosing ODJ means going past VMware's mechanism rather than matching it.

Late `Add-Computer` in `setup.ps1` stays as the fallback regardless; it has
joined successfully in all runs, and `setup.ps1` skips it when `PartOfDomain` is
already true.

Note that our `RunSynchronousCommand` entries execute **before** DJOIN in the
same pass, so a specialize-time probe or `Add-Computer` is straightforward to
slot in:

```
[Action Queue] : Executing command "C:\WINDOWS\SYSTEM32\SETUPUGC.EXE"  specialize
[Action Queue] : Executing command "C:\WINDOWS\SYSTEM32\RUNDLL32.EXE" shsetup.dll,SHUnattendedSetup specialize
[Action Queue] : Executing command "C:\WINDOWS\SYSTEM32\DJOIN.EXE"  specialize
```

## Operational notes for this experiment

- **Rebuild, do not just restart.** `docker-compose.yml` bakes the source into
  the images (no bind mount), so `docker compose restart` silently keeps running
  the old code. Run 3 was wasted this way: the guest received the previous
  answer file even though the lab host's files were current. Use
  `rsync … && docker compose build && docker compose up -d`, then verify with
  `docker compose exec worker grep … /app/app/...`.
- **Backslashes do not survive the QGA/PVE exec path.** Build UNC paths and
  `DOMAIN\user` strings from `([char]92)` inside PowerShell, or you silently test
  the wrong target (System error 67).
- **Nested double quotes do not survive either.** `_write_sysprep_files` threw
  `throw "GuestOS staged setup.ps1 missing: $staged"`, which reached the guest
  unquoted and failed with `The term 'GuestOS' is not recognized`. That broke the
  retry-on-missing-staged-file logic, turning a transient write miss into a task
  failure (run 3). Fixed to `throw ('...' + $staged)`; use single quotes only.
- Each smoke round costs ~25 minutes, most of it the DJOIN retry stall. Delete
  the leftover clone (VMID 121) between rounds or the RAM preflight will refuse
  to start.

## Run log

| Date | Guest / VMID | Unattend credential shape | specialize | late `Add-Computer` |
|---|---|---|---|---|
| 2026-08-15 | `W11UJ211057` / 121 | `Domain=lab.test`, `Username=administrator` | `0x52e` | OK |
| 2026-08-15 | `W11UJ214143` / 121 | no `Domain`, `Username=administrator@lab.test` | `0x52e` | OK |
| 2026-08-15 | `W11UJ222331` / 121 | `Domain=LAB`, `Username=administrator` | `0x52e` | OK |
| 2026-08-16 | `S22F86873230` / 121 | schema order, no `Domain`, UPN, timeout 3 | `0x52e` (retried ~15 min; timeout ignored) | OK — task SUCCESS, DHCP 192.168.123.160, joined `lab.test` |

Template: `Win11-templ2` (VMID 127), DHCP, domain `lab.test`.
Smoke command: `python3 scripts/lab_full_feature_smoke.py --template-vmid 127 --no-disks --poll`.
