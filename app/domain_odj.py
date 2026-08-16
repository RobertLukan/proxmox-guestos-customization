"""Offline Domain Join (ODJ) provisioning via Samba's ``net offlinejoin``.

Creates the computer account in AD from the GuestOS host and returns the
base64 provisioning blob for the answer file's
``Microsoft-Windows-UnattendedJoin/Identification/Provisioning/AccountData``.

The guest then joins during ``specialize`` with no credentials and no network
round-trip, which sidesteps the ``0x52e`` failures documented in
``docs/UNATTENDED_JOIN_INVESTIGATION.md``.

Every failure here is soft: the caller falls back to late ``Add-Computer`` in
``setup.ps1``.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import socket
import subprocess

from flask import current_app

from app.util import as_bool as _as_bool

NET_BINARY = '/usr/bin/net'
HOSTS_FILE = '/etc/hosts'

# Samba prints the blob as one long base64 line; anything else is chatter.
_BLOB_RE = re.compile(r'^[A-Za-z0-9+/=]{64,}$')
# `net ads info` reports the DC's own FQDN, which is the name Samba insists on
# connecting to during the join (see _dc_fqdn).
_LDAP_NAME_RE = re.compile(r'^LDAP server name:\s*(\S+)\s*$', re.MULTILINE)
_HOSTNAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,253}$')


def odj_enabled() -> bool:
    try:
        return bool(current_app.config.get('DOMAIN_JOIN_ODJ', False))
    except RuntimeError:
        return False


def _timeout_seconds() -> int:
    try:
        return max(10, int(current_app.config.get('DOMAIN_JOIN_ODJ_TIMEOUT_SECONDS') or 60))
    except (TypeError, ValueError, RuntimeError):
        return 60


def _split_username(username: str) -> tuple[str, str]:
    """Return (user, domain_hint) from a UPN or DOMAIN\\user string."""
    user = (username or '').strip()
    if '@' in user:
        left, right = user.rsplit('@', 1)
        return left, right
    if '\\' in user:
        dom, left = user.split('\\', 1)
        return left, dom
    return user, ''


def _resolve_credentials(data: dict) -> tuple[str, str, str, str]:
    """Return (domain, username, password, ou) from ``data`` or the packed blob.

    ``_prepare_domain_join`` pops the raw password once it is packed into
    ``domain_join_b64``, so by the time provisioning runs the secret usually
    only exists inside that blob.
    """
    domain = (data.get('domain_name') or '').strip().rstrip('.')
    username = (data.get('domain_username') or '').strip()
    password = data.get('domain_password') or ''
    ou = (data.get('domain_ou') or '').strip()
    if password or not data.get('domain_join_b64'):
        return domain, username, password, ou
    try:
        blob = json.loads(base64.b64decode(data['domain_join_b64']).decode('utf-8'))
    except Exception as e:  # noqa: BLE001 — malformed blob must not break the deploy
        logging.warning('ODJ: could not unpack domain_join_b64: %s', e)
        return domain, username, password, ou
    return (
        (blob.get('domain') or domain).strip().rstrip('.'),
        (blob.get('username') or username).strip(),
        blob.get('password') or '',
        (blob.get('ou') or ou).strip(),
    )


def _extract_blob(stdout: str) -> str:
    for line in (stdout or '').splitlines():
        candidate = line.strip()
        if _BLOB_RE.match(candidate):
            return candidate
    return ''


def _dc_fqdn(dc: str, timeout: int) -> str:
    """Ask the DC at ``dc`` for its own FQDN over CLDAP.

    Samba discards the ``dcname`` we pass and reconnects to the name the DC
    reports, so provisioning fails unless that name resolves here. Asking first
    lets us fix resolution before the join instead of failing on it.
    """
    try:
        proc = subprocess.run(
            [NET_BINARY, '-s', '/dev/null', 'ads', 'info', '-S', dc],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logging.warning('ODJ: could not query DC info from %s: %s', dc, e)
        return ''
    match = _LDAP_NAME_RE.search(proc.stdout or '')
    name = match.group(1) if match else ''
    return name if _HOSTNAME_RE.match(name or '') else ''


def _ensure_resolvable(name: str, ip: str) -> bool:
    """Make ``name`` resolve to ``ip``, adding a hosts entry if DNS cannot.

    A GuestOS host generally does not use the AD DNS servers, so the DC's FQDN
    is usually unresolvable here even though the DC itself is reachable by IP.
    """
    try:
        socket.getaddrinfo(name, None)
        return True
    except socket.gaierror:
        pass
    entry = f'{ip} {name} {name.split(".")[0]}'
    try:
        with open(HOSTS_FILE, 'r', encoding='utf-8') as fh:
            if any(line.split('#', 1)[0].split()[1:2] == [name]
                   for line in fh if line.strip()):
                return True
        # Append only: /etc/hosts is a bind mount in a container and cannot be
        # replaced, only extended.
        with open(HOSTS_FILE, 'a', encoding='utf-8') as fh:
            fh.write(f'{entry}\n')
    except OSError as e:
        logging.warning('ODJ: %s is unresolvable and %s is not writable: %s',
                        name, HOSTS_FILE, e)
        return False
    logging.info('ODJ: mapped %s in %s so Samba can reach the DC', entry, HOSTS_FILE)
    try:
        socket.getaddrinfo(name, None)
    except socket.gaierror:
        logging.warning('ODJ: %s still does not resolve after the hosts entry', name)
        return False
    return True


def provision_odj_blob(data: dict, hostname: str) -> str | None:
    """Provision ``hostname`` in AD and return the ODJ blob, or None.

    Returns None whenever ODJ is disabled, prerequisites are missing, or the
    provisioning call fails for any reason — the caller keeps the credential
    based ``Add-Computer`` path in that case. Never raises.
    """
    if not _as_bool(data.get('join_domain'), False):
        return None
    if not odj_enabled():
        logging.info('join-path: provision=add-computer reason=odj-disabled')
        return None

    machine = (hostname or '').strip().split('.')[0]
    domain, username, password, ou = _resolve_credentials(data)
    if not (machine and domain and username and password):
        logging.warning('ODJ: missing hostname/domain/credentials; skipping provisioning')
        logging.info('join-path: provision=add-computer reason=missing-creds')
        return None

    if not os.path.exists(NET_BINARY):
        logging.warning(
            'ODJ: %s not found (samba-common-bin missing); falling back to Add-Computer',
            NET_BINARY,
        )
        logging.info('join-path: provision=add-computer reason=samba-missing')
        return None

    from app.domain_preflight import _collect_join_targets

    targets = _collect_join_targets(data) or []
    user, _domain_hint = _split_username(username)
    timeout = _timeout_seconds()

    # Samba reads the password from PASSWD so it never lands in the process list.
    env = dict(os.environ)
    env['PASSWD'] = password

    last_error = ''
    for dc in targets or ['']:
        # Prefer the DC's own FQDN: Samba reconnects to it regardless of what we
        # pass, so it has to resolve either way.
        dc_name = dc
        if dc:
            fqdn = _dc_fqdn(dc, timeout)
            if fqdn and _ensure_resolvable(fqdn, dc):
                dc_name = fqdn

        argv = [
            NET_BINARY,
            '-s', '/dev/null',
            'offlinejoin', 'provision',
            f'domain={domain}',
            f'machine_name={machine}',
        ]
        if ou:
            argv.append(f'machine_account_ou={ou}')
        if dc_name:
            argv.append(f'dcname={dc_name}')
        argv += ['printblob', '-U', user]

        try:
            proc = subprocess.run(
                argv,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_error = f'timeout after {timeout}s'
            logging.warning('ODJ: provisioning timed out via dc=%s', dc_name or '(auto)')
            continue
        except OSError as e:
            last_error = str(e)
            logging.warning('ODJ: could not run %s: %s', NET_BINARY, e)
            break

        if proc.returncode == 0:
            blob = _extract_blob(proc.stdout)
            if blob:
                logging.info(
                    'ODJ: provisioned %s in %s (dc=%s, ou=%s)',
                    machine, domain, dc_name or '(auto)', ou or '(default)',
                )
                logging.info(
                    'join-path: provision=odj hostname=%s domain=%s dc=%s',
                    machine, domain, dc_name or '(auto)',
                )
                return blob
            last_error = 'no blob in output'
            logging.warning('ODJ: provisioning returned 0 but no blob was printed')
            continue

        # stderr can echo the machine/domain but never the password (PASSWD env).
        last_error = (proc.stderr or proc.stdout or '').strip().replace('\n', ' ')[:240]
        logging.warning(
            'ODJ: provisioning failed via dc=%s (rc=%s): %s',
            dc_name or '(auto)', proc.returncode, last_error,
        )

    logging.warning(
        'ODJ: could not provision %s in %s (%s); falling back to Add-Computer',
        machine, domain, last_error or 'no DC targets',
    )
    logging.info(
        'join-path: provision=add-computer hostname=%s reason=%s',
        machine, last_error or 'no DC targets',
    )
    return None
