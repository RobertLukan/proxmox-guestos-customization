# Security Policy

## Supported versions

Security fixes are applied on the current `main` branch / latest release
(see [`VERSION`](VERSION)). Older tags are not backported unless noted in a
release.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security-sensitive reports.

Preferred options:

1. GitHub **[Private vulnerability reporting](https://github.com/RobertLukan/proxmox-guestos-customization/security/advisories/new)**
   (Security → Advisories → Report a vulnerability), if enabled on the repo.
2. Or open a **private** contact via the maintainer listed on the GitHub profile
   / repository — ask for a secure channel before sending secrets or exploit
   details.

Include: affected version/commit, impact, reproduction steps, and whether a
fix is public. You should get an acknowledgement within a few days when
possible; this is a community project, not a commercial SLA.

## Threat model (short)

GuestOS is an **ops automation sidecar**. It typically holds:

- Proxmox VE API credentials (clone / configure / guest agent)
- Optional AD domain-join credentials (`DOMAIN_PROFILES_JSON`)
- UI session secret, API tokens, and PDM launch HMAC secret

Compromise of the GuestOS host or its `.env` is roughly equivalent to those
privileges. Treat the host accordingly: least-privilege PVE user, TLS,
firewall, rotate secrets, change the default UI password immediately.

GuestOS does **not** auto-delete failed clones; failed VMs are renamed/tagged
for operator triage. In-place Sysprep of arbitrary existing VMs is disabled.

## What we run in CI

On every push/PR (see `.github/workflows/`):

- Unit / API tests
- **CodeQL** (Python)
- **Bandit** (Python SAST)
- **pip-audit** (dependency CVEs)
- **gitleaks** (secret scanning)
- **Trivy** (filesystem + container image)

Releases may attach checksums and an SBOM. These checks reduce risk; they do
**not** prove the software is free of bugs or misuse.

## Operator checklist

- Deploy behind TLS (`BEHIND_REVERSE_PROXY=True`); see [docs/TLS_PRODUCTION.md](docs/TLS_PRODUCTION.md).
- Set a strong `SECRET_KEY`; never commit `.env`.
- Use a dedicated PVE API user with only the rights GuestOS needs — see
  [docs/INSTALL.md](docs/INSTALL.md) (**Proxmox privileges**).
- Keep `GUESTOS_API_TOKEN` / `GUESTOS_LAUNCH_SECRET` off shared clients and out of
  UI wasm / screenshots.
- Review templates and network before bulk Win11 provisioning.

## Proxmox privileges (summary)

GuestOS needs a Proxmox principal that can **clone a Windows template, fully
configure and power the new VM, and use the QEMU guest agent** (file write +
command exec). Prefer a dedicated role on a pool (or `/` in lab), not
`root@pam`. Operators using the UI/PDM do not need those PVE rights.

Full privilege list and layout: [docs/INSTALL.md — Proxmox privileges](docs/INSTALL.md#proxmox-privileges).
