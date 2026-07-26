# TLS and SSL verification (production checklist)

Use this when moving GuestOS / PDM off the lab subnet. Lab defaults intentionally
trust self-signed Caddy and skip Proxmox API cert checks.

## GuestOS (Caddy)

1. Replace the lab self-signed material under `deploy/caddy/` with a certificate
   trusted by operator browsers and by the PDM host (Let's Encrypt, internal CA, etc.).
2. Set `GUESTOS_TLS_HOST` to the public DNS name clients use.
3. Rebuild/restart Compose so Caddy serves the new cert:
   `./deploy/caddy/gen-selfsigned.sh` is **lab-only** — do not use it for production.
4. Confirm `https://<GUESTOS_TLS_HOST>/api/health` succeeds in a browser **without**
   certificate warnings.

## GuestOS → Proxmox API

In GuestOS `.env`:

| Variable | Lab | Production |
|----------|-----|------------|
| `PROXMOX_VERIFY_SSL` | `False` | **`True`** |
| Per-remote `verify_ssl` in `PVE_REMOTES_JSON` | `false` | **`true`** |

Ensure the Proxmox API presents a cert the GuestOS worker trusts (or install the
CA into the worker image / host trust store).

## PDM → GuestOS proxy

In `/etc/proxmox-datacenter-manager/guestos.cfg`:

| Key | Lab | Production |
|-----|-----|------------|
| `base-url` | `https://<lab-ip>` | `https://guestos.example.com` |
| `verify-tls` | `false` | **`true`** (or omit; default true) |
| `api-token` / `launch-secret` | lab values | rotate; match GuestOS `.env` |

After changing the cfg, restart `proxmox-datacenter-manager.service` (or reboot).

## Auth hygiene

- Change GuestOS default login away from `changeme`.
- Keep `GUESTOS_API_TOKEN` and `GUESTOS_LAUNCH_SECRET` only on the GuestOS host
  and in PDM `guestos.cfg` — never in UI wasm or public docs.
- Restrict `GUESTOS_CORS_ORIGINS` if browsers still call GuestOS directly (PDM
  proxy path does not need browser CORS to GuestOS).

## Quick verify

```bash
# From an operator workstation (trusted CA):
curl -fsS "https://guestos.example.com/api/health"

# From the PDM host (with guestos.cfg verify-tls true):
# Customize (GuestOS) on a Windows template should open /launch without TLS errors;
# GuestOS tab should list tasks.
```
