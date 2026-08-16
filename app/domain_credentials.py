"""Domain-join credential normalization and host-side LDAP helpers."""
from __future__ import annotations

import logging
import re
from typing import Any

from app.validators import ValidationError, validate_domain, validate_dns_servers

# Zero-width / BOM characters often introduced by copy-paste.
_ZW_RE = re.compile(
    '[\u200b\u200c\u200d\ufeff\u00a0]'
)
_UPN_RE = re.compile(
    r'^[A-Za-z0-9._+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$'
)
_DOWNLEVEL_RE = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9._\-]{0,14}\\[A-Za-z0-9._+\-]{1,64}$'
)


def scrub_username_noise(value: str) -> str:
    """Strip BOM / zero-width chars and outer whitespace."""
    v = str(value or '')
    v = _ZW_RE.sub('', v)
    return v.strip()


def normalize_domain_password(password: Any) -> tuple[str, bool]:
    """Return (password, trimmed_trailing_newline).

    Only strips trailing ``\\r`` / ``\\n`` (common .env / paste artifacts).
    Does not strip intentional leading/trailing spaces.
    """
    if password is None:
        raise ValidationError('Domain join requires a username and password.')
    raw = str(password)
    if raw == '':
        raise ValidationError('Domain join requires a username and password.')
    trimmed = raw.rstrip('\r\n')
    if trimmed == '':
        raise ValidationError('Domain join requires a username and password.')
    return trimmed, trimmed != raw


def normalize_domain_username(username: Any) -> str:
    """Normalize join username to UPN or DOMAIN\\user. Reject bare names."""
    v = scrub_username_noise(username or '')
    if not v:
        raise ValidationError('Domain join requires a username and password.')
    # Normalize DOMAIN/user → DOMAIN\user
    if '/' in v and '@' not in v and v.count('/') == 1:
        left, right = v.split('/', 1)
        v = f'{left}\\{right}'
    if '@' in v:
        if not _UPN_RE.match(v):
            raise ValidationError(
                f'Invalid domain username (expected user@domain): {username!r}'
            )
        return v
    if '\\' in v:
        if not _DOWNLEVEL_RE.match(v):
            raise ValidationError(
                f'Invalid domain username (expected DOMAIN\\user): {username!r}'
            )
        # Preserve domain NetBIOS case as typed; user part as typed.
        dom, user = v.split('\\', 1)
        return f'{dom}\\{user}'
    raise ValidationError(
        'Domain username must be UPN (user@domain.tld) or down-level '
        f'(DOMAIN\\user); bare names are not allowed: {username!r}'
    )


def prepare_join_credentials(data: dict) -> dict:
    """Normalize domain/username/password/ou on ``data`` for join.

    Mutates ``data`` in place. Returns the packed blob dict
    ``{domain, username, password[, ou]}``.
    """
    domain = validate_domain(data.get('domain_name'))
    username = normalize_domain_username(data.get('domain_username'))
    password, trimmed_nl = normalize_domain_password(data.get('domain_password'))
    if trimmed_nl:
        logging.info('domain password: trimmed trailing newline before join packing')
    ou = (data.get('domain_ou') or '').strip()
    data['domain_name'] = domain
    data['domain_username'] = username
    data['domain_password'] = password
    data['domain_ou'] = ou
    blob = {'domain': domain, 'username': username, 'password': password}
    if ou:
        blob['ou'] = ou
    return blob


def format_cred_probe_failure(
    *,
    error_class: str,
    domain: str,
    username: str,
    dns_servers: str | list | None,
    bind_target: str | None = None,
    guest_ip: str | None = None,
    result: str | None = None,
    domain_profile: str | None = None,
) -> str:
    """Build operator-facing failure text (never includes password)."""
    if isinstance(dns_servers, (list, tuple)):
        dns = ','.join(str(x) for x in dns_servers if x)
    else:
        dns = (dns_servers or '').strip()
    lines = [
        'Domain credential check failed before Sysprep.',
        f'  class={error_class}',
        f'  domain={domain}',
        f'  username={username}',
        f'  dns_servers={dns or "(none)"}',
    ]
    if bind_target:
        lines.append(f'  bind_target={bind_target}')
    if guest_ip:
        lines.append(f'  guest_ip={guest_ip}')
    if domain_profile:
        lines.append(f'  domain_profile={domain_profile}')
    if result:
        # Scrub accidental password echoes
        safe = str(result).replace('\r', ' ').replace('\n', ' ')
        if len(safe) > 240:
            safe = safe[:237] + '...'
        lines.append(f'  result={safe}')
    return '\n'.join(lines)


def classify_ldap_error(exc: BaseException | str) -> tuple[str, str]:
    """Return (error_class, short_result) for LDAP / ADSI failures."""
    msg = str(exc or '')
    low = msg.lower()
    if any(
        x in low
        for x in (
            'invalid credentials',
            'invalidcredentials',
            '0x8007052e',
            '52e',
            'logon failure',
            'username or password',
            'authentication failed',
            'data 52e',
            'acceptsecuritycontext',
        )
    ):
        return 'invalid_credentials', msg
    if any(
        x in low
        for x in (
            'account locked',
            'account disabled',
            'logon hours',
            'password expired',
            'must change password',
            '0x8007052f',
            '0x800703ee',
            'data 533',
            'data 701',
            'data 775',
            'data 532',
        )
    ):
        return 'account_restricted', msg
    if any(
        x in low
        for x in (
            'timeout',
            'timed out',
            'unreachable',
            'connection refused',
            'no route',
            'network is unreachable',
            'can\'t contact',
            'cannot contact',
            'server down',
            'no ip',
            'dns',
        )
    ):
        return 'unreachable', msg
    return 'other', msg


def _uses_profile_credentials(data: dict) -> bool:
    """True when join secrets come from ``DOMAIN_PROFILES``, not the request."""
    from app.util import as_bool as _as_bool

    if not (data.get('domain_profile') or '').strip():
        return False
    return _as_bool(data.get('use_domain_profile_credentials'), True)


def credential_dns_servers(data: dict | None) -> list[str]:
    """DNS IPs used to reach AD for LDAP bind, ODJ, and the in-guest probe.

    When profile credentials are in use, this is **always** the profile's
    ``dns_servers`` from server config — never caller-supplied DNS. Guest NIC
    DNS (``data['dns_servers']``) is unchanged and may still come from the form.
    """
    payload = data if isinstance(data, dict) else {}
    if _uses_profile_credentials(payload):
        name = (payload.get('domain_profile') or '').strip()
        profile = {}
        try:
            from flask import current_app

            profile = (current_app.config.get('DOMAIN_PROFILES') or {}).get(name) or {}
        except RuntimeError:
            profile = {}
        raw = (profile.get('dns_servers') or '').strip()
        if not raw:
            return []
        try:
            return validate_dns_servers(raw, allow_ipv6=True)
        except ValidationError:
            return []
    try:
        return validate_dns_servers(payload.get('dns_servers'), allow_ipv6=True)
    except ValidationError:
        return []


def _ldap_server_candidates(data: dict) -> list[str]:
    try:
        dns = credential_dns_servers(data)
    except ValidationError:
        dns = []
    out = list(dns)
    domain = (data.get('domain_name') or '').strip().rstrip('.')
    if domain and domain not in out:
        out.append(domain)
    return out


def _ldap_timeout_seconds(timeout: float | int) -> int:
    """ldap3 connect/receive timeouts must be ints on some platforms."""
    try:
        return max(1, int(timeout))
    except (TypeError, ValueError):
        return 5


def host_ldap_bind(
    data: dict,
    *,
    timeout: float = 5.0,
) -> dict:
    """LDAP simple bind from the GuestOS host (profile test / admit helpers).

    Returns ``{ok, class, result, bind_target, domain, username}``.
    """
    from ldap3 import Connection, Server, ALL
    from ldap3.core.exceptions import LDAPException

    domain = validate_domain(data.get('domain_name'))
    username = normalize_domain_username(data.get('domain_username'))
    password, _ = normalize_domain_password(data.get('domain_password'))
    targets = _ldap_server_candidates(data)
    if not targets:
        return {
            'ok': False,
            'class': 'unreachable',
            'result': 'no dns_servers or domain to contact for LDAP bind',
            'bind_target': None,
            'domain': domain,
            'username': username,
        }
    # Prefer DC IPs from credential_dns_servers (profile DNS when using stored
    # passwords). Binding only to the domain FQDN often fails inside the
    # GuestOS container (no domain DNS).
    dns_list = []
    try:
        dns_list = credential_dns_servers(data)
    except ValidationError:
        dns_list = []
    if not dns_list and not _uses_profile_credentials(data):
        return {
            'ok': False,
            'class': 'unreachable',
            'result': (
                'dns_servers is empty — set DNS on the Network step to your '
                'DC/domain DNS IP(s) before testing from the GuestOS host'
            ),
            'bind_target': None,
            'domain': domain,
            'username': username,
        }

    tmo = _ldap_timeout_seconds(timeout)
    last_err = 'no LDAP target responded'
    last_target = None
    for host in targets:
        last_target = f'{host}:389'
        try:
            server = Server(host, port=389, get_info=ALL, connect_timeout=tmo)
            conn = Connection(
                server,
                user=username,
                password=password,
                auto_bind=True,
                receive_timeout=tmo,
            )
            conn.unbind()
            return {
                'ok': True,
                'class': 'ok',
                'result': 'OK',
                'bind_target': last_target,
                'domain': domain,
                'username': username,
            }
        except LDAPException as e:
            cls, short = classify_ldap_error(e)
            last_err = short
            if cls == 'invalid_credentials' or cls == 'account_restricted':
                return {
                    'ok': False,
                    'class': cls,
                    'result': short,
                    'bind_target': last_target,
                    'domain': domain,
                    'username': username,
                }
        except Exception as e:  # noqa: BLE001 — socket/struct quirks from ldap3
            last_err = str(e)
    cls, short = classify_ldap_error(last_err)
    return {
        'ok': False,
        'class': cls if cls != 'other' else 'unreachable',
        'result': short,
        'bind_target': last_target,
        'domain': domain,
        'username': username,
    }


def host_ldap_check_computer_exists(data: dict, hostname: str, *, timeout: float = 5.0) -> None:
    """Raise ValidationError if a computer account for ``hostname`` already exists."""
    from ldap3 import Connection, Server, ALL, SUBTREE
    from ldap3.core.exceptions import LDAPException

    host = (hostname or '').strip().split('.')[0]
    if not host:
        return
    domain = validate_domain(data.get('domain_name'))
    username = normalize_domain_username(data.get('domain_username'))
    password, _ = normalize_domain_password(data.get('domain_password'))
    targets = _ldap_server_candidates(data)
    sam = f'{host}$'
    # Build base DN from domain DNS name
    base = ','.join(f'DC={p}' for p in domain.split('.'))
    filt = f'(&(objectClass=computer)(|(sAMAccountName={sam})(cn={host})))'
    tmo = _ldap_timeout_seconds(timeout)
    last_err = None
    for ldap_host in targets:
        try:
            server = Server(ldap_host, port=389, get_info=ALL, connect_timeout=tmo)
            conn = Connection(
                server,
                user=username,
                password=password,
                auto_bind=True,
                receive_timeout=tmo,
            )
            try:
                conn.search(base, filt, search_scope=SUBTREE, attributes=['cn'], size_limit=1)
                if conn.entries:
                    raise ValidationError(
                        f'Computer account already exists in AD for hostname {host!r} '
                        f'(domain {domain}). Choose another name or remove the existing object.'
                    )
                return
            finally:
                conn.unbind()
        except ValidationError:
            raise
        except LDAPException as e:
            last_err = e
            cls, _ = classify_ldap_error(e)
            if cls in ('invalid_credentials', 'account_restricted'):
                raise ValidationError(
                    f'Cannot check AD for existing computer {host!r}: {e}'
                ) from e
        except Exception as e:  # noqa: BLE001
            last_err = e
    if last_err:
        logging.warning(
            'AD computer uniqueness check skipped (LDAP unreachable): %s', last_err
        )


def host_ldap_validate_ou(data: dict, *, timeout: float = 5.0) -> None:
    """Raise ValidationError if domain_ou is set but not readable via LDAP."""
    from ldap3 import Connection, Server, ALL, BASE
    from ldap3.core.exceptions import LDAPException

    ou = (data.get('domain_ou') or '').strip()
    if not ou:
        return
    username = normalize_domain_username(data.get('domain_username'))
    password, _ = normalize_domain_password(data.get('domain_password'))
    targets = _ldap_server_candidates(data)
    if not targets:
        logging.warning(
            'domain_ou validation skipped: no dns_servers/domain for GuestOS-host LDAP'
        )
        return
    tmo = _ldap_timeout_seconds(timeout)
    last_err = None
    for host in targets:
        try:
            server = Server(host, port=389, get_info=ALL, connect_timeout=tmo)
            conn = Connection(
                server,
                user=username,
                password=password,
                auto_bind=True,
                receive_timeout=tmo,
            )
            try:
                ok = conn.search(ou, '(objectClass=*)', search_scope=BASE, attributes=['distinguishedName'])
                if not ok or not conn.entries:
                    raise ValidationError(
                        f'domain_ou DN was not found or is not readable: {ou!r}'
                    )
                return
            finally:
                conn.unbind()
        except ValidationError:
            raise
        except LDAPException as e:
            last_err = e
            cls, _ = classify_ldap_error(e)
            if cls in ('invalid_credentials', 'account_restricted'):
                raise ValidationError(f'Cannot validate domain_ou (LDAP auth failed): {e}') from e
        except Exception as e:  # noqa: BLE001
            last_err = e
    # Host cannot reach LDAP — skip OU check; guest path may still work.
    logging.warning(
        'domain_ou validation skipped (LDAP unreachable from GuestOS host): %s',
        last_err,
    )
    return


def run_admit_directory_checks(data: dict) -> None:
    """Hostname uniqueness + OU validate when join_domain (best-effort LDAP)."""
    from app.util import as_bool as _as_bool

    if not _as_bool(data.get('join_domain'), False):
        return
    # Ensure normalized creds present
    prepare_join_credentials(data)
    host_ldap_check_computer_exists(data, data.get('hostname') or '')
    host_ldap_validate_ou(data)
