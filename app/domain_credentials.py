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
    domain_ou: str | None = None,
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
    if domain_ou:
        lines.append(f'  domain_ou={domain_ou}')
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


# Domain wellKnownObjects GUID for the default computer location (redircmp).
_COMPUTERS_WKGUID = 'AA312825768811D1ADED00C04FD8D5CD'


def _ldap_attr_values(entry: Any, name: str) -> list[str]:
    """Read a multi-value LDAP attribute from an ldap3 Entry or a test fake."""
    attr = getattr(entry, name, None)
    if attr is None:
        return []
    if isinstance(attr, (list, tuple)):
        return [str(v) for v in attr if v is not None]
    vals = getattr(attr, 'values', None)
    if vals is not None:
        return [str(v) for v in vals if v is not None]
    val = getattr(attr, 'value', None)
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        return [str(v) for v in val if v is not None]
    return [str(val)]


def is_computers_container_dn(dn: str) -> bool:
    """True when the DN's first RDN is the built-in Computers container."""
    first = (dn or '').split(',', 1)[0].strip()
    return first.lower() == 'cn=computers'


def _entry_object_classes(entry: Any) -> list[str]:
    return [v.lower() for v in _ldap_attr_values(entry, 'objectClass')]


def _parse_well_known_dn(values: list[str], guid: str) -> str | None:
    needle = guid.replace('-', '').upper()
    for raw in values:
        parts = str(raw).split(':', 3)
        if len(parts) >= 4 and parts[2].replace('-', '').upper() == needle:
            found = parts[3].strip()
            if found:
                return found
    return None


def _ou_not_an_ou_message(dn: str, classes: list[str] | None = None) -> str:
    shown = ','.join(classes) if classes else 'container'
    if is_computers_container_dn(dn):
        return (
            f'domain_ou {dn!r} is the default Computers container, not an OU. '
            'Windows 11 24H2/25H2 cannot join there (NetJoin 0x2, no downlevel '
            'retry) — use a real OU=… distinguished name.'
        )
    return (
        f'domain_ou {dn!r} exists but is not an organizationalUnit '
        f'(objectClass={shown}). NetJoin requires an OU=… path.'
    )


def _empty_ou_default_container_warning(dn: str) -> str:
    return (
        f'Target OU is empty; domain default computer location is {dn} '
        '(a container, not an OU). GuestOS requires a real OU=… distinguished '
        'name. Windows 11 24H2/25H2 cannot join CN=Computers via -OUPath '
        '(NetJoin 0x2, no downlevel retry). Set Target OU to an OU=… DN, or '
        'redirect the default with redircmp.'
    )


def _lookup_default_computer_dn(conn) -> str | None:
    """Return the domain's default computer container/OU DN, or None."""
    from ldap3 import BASE

    conn.search(
        '',
        '(objectClass=*)',
        search_scope=BASE,
        attributes=['defaultNamingContext'],
    )
    if not conn.entries:
        return None
    ncs = _ldap_attr_values(conn.entries[0], 'defaultNamingContext')
    nc = ncs[0] if ncs else None
    if not nc:
        return None
    conn.search(
        nc,
        '(objectClass=*)',
        search_scope=BASE,
        attributes=['wellKnownObjects'],
    )
    if conn.entries:
        found = _parse_well_known_dn(
            _ldap_attr_values(conn.entries[0], 'wellKnownObjects'),
            _COMPUTERS_WKGUID,
        )
        if found:
            return found
    return f'CN=Computers,{nc}'


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
    from ldap3.utils.conv import escape_filter_chars

    host = (hostname or '').strip().split('.')[0]
    if not host:
        return
    domain = validate_domain(data.get('domain_name'))
    username = normalize_domain_username(data.get('domain_username'))
    password, _ = normalize_domain_password(data.get('domain_password'))
    targets = _ldap_server_candidates(data)
    safe_host = escape_filter_chars(host)
    sam = f'{safe_host}$'
    base = ','.join(f'DC={escape_filter_chars(p)}' for p in domain.split('.') if p)
    filt = f'(&(objectClass=computer)(|(sAMAccountName={sam})(cn={safe_host})))'
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


def host_ldap_check_join_ou(data: dict, *, timeout: float = 5.0) -> dict:
    """Inspect the join OU for the credential test / admit checks.

    ``ok`` is False when a *specified* ``domain_ou`` is invalid or not an
    organizationalUnit, or when Target OU is blank and the domain default
    computer location is still ``CN=Computers``. Unreachable LDAP sets
    ``skipped`` and does not fail the test.
    """
    from ldap3 import ALL, BASE, Connection, Server
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.dn import safe_dn

    specified = (data.get('domain_ou') or '').strip()
    empty = {
        'ok': True,
        'class': 'ok',
        'result': 'OK',
        'skipped': False,
        'domain_ou': None,
        'default_computer_container': None,
        'warning': None,
        'object_class': None,
    }
    if specified:
        try:
            specified = safe_dn(specified)
        except Exception as e:  # noqa: BLE001
            return {
                'ok': False,
                'class': 'invalid_ou',
                'result': f'domain_ou is not a valid DN: {specified!r}',
                'skipped': False,
                'domain_ou': specified,
                'default_computer_container': None,
                'warning': None,
                'object_class': None,
                'error': str(e),
            }

    username = normalize_domain_username(data.get('domain_username'))
    password, _ = normalize_domain_password(data.get('domain_password'))
    targets = _ldap_server_candidates(data)
    if not targets:
        logging.warning(
            'domain_ou validation skipped: no dns_servers/domain for GuestOS-host LDAP'
        )
        out = dict(empty)
        out['skipped'] = True
        out['class'] = 'skipped'
        out['result'] = 'OU check skipped: no LDAP target'
        out['domain_ou'] = specified or None
        return out

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
                if specified:
                    ok = conn.search(
                        specified,
                        '(objectClass=*)',
                        search_scope=BASE,
                        attributes=['distinguishedName', 'objectClass'],
                    )
                    if not ok or not conn.entries:
                        return {
                            'ok': False,
                            'class': 'invalid_ou',
                            'result': (
                                f'domain_ou DN was not found or is not readable: '
                                f'{specified!r}'
                            ),
                            'skipped': False,
                            'domain_ou': specified,
                            'default_computer_container': None,
                            'warning': None,
                            'object_class': None,
                        }
                    entry = conn.entries[0]
                    classes = _entry_object_classes(entry)
                    if 'organizationalunit' not in classes:
                        msg = _ou_not_an_ou_message(specified, classes)
                        return {
                            'ok': False,
                            'class': 'not_an_ou',
                            'result': msg,
                            'skipped': False,
                            'domain_ou': specified,
                            'default_computer_container': None,
                            'warning': msg,
                            'object_class': ','.join(classes) or None,
                        }
                    return {
                        'ok': True,
                        'class': 'ok',
                        'result': f'OU {specified} is an organizationalUnit',
                        'skipped': False,
                        'domain_ou': specified,
                        'default_computer_container': None,
                        'warning': None,
                        'object_class': ','.join(classes) or None,
                    }

                default_dn = _lookup_default_computer_dn(conn)
                out = dict(empty)
                out['default_computer_container'] = default_dn
                if not default_dn:
                    out['skipped'] = True
                    out['class'] = 'skipped'
                    out['result'] = (
                        'OU check skipped: could not read default computer location'
                    )
                    return out
                ok = conn.search(
                    default_dn,
                    '(objectClass=*)',
                    search_scope=BASE,
                    attributes=['distinguishedName', 'objectClass'],
                )
                classes = (
                    _entry_object_classes(conn.entries[0])
                    if ok and conn.entries
                    else []
                )
                out['object_class'] = ','.join(classes) or None
                if 'organizationalunit' in classes:
                    out['result'] = (
                        f'default computer location {default_dn} is an organizationalUnit'
                    )
                    return out
                out['ok'] = False
                out['warning'] = _empty_ou_default_container_warning(default_dn)
                out['class'] = 'empty_default_container'
                out['result'] = out['warning']
                return out
            finally:
                conn.unbind()
        except LDAPException as e:
            last_err = e
            cls, _ = classify_ldap_error(e)
            if cls in ('invalid_credentials', 'account_restricted'):
                return {
                    'ok': False,
                    'class': cls,
                    'result': f'Cannot validate domain_ou (LDAP auth failed): {e}',
                    'skipped': False,
                    'domain_ou': specified or None,
                    'default_computer_container': None,
                    'warning': None,
                    'object_class': None,
                }
        except Exception as e:  # noqa: BLE001
            last_err = e

    logging.warning(
        'domain_ou validation skipped (LDAP unreachable from GuestOS host): %s',
        last_err,
    )
    out = dict(empty)
    out['skipped'] = True
    out['class'] = 'skipped'
    out['result'] = f'OU check skipped (LDAP unreachable): {last_err}'
    out['domain_ou'] = specified or None
    return out


def _ou_admit_validate_enabled() -> bool:
    """Admit OU check is on unless DOMAIN_JOIN_VALIDATE_OU=false (lab kill-switch)."""
    try:
        from flask import current_app

        from app.util import as_bool as _as_bool

        return _as_bool(current_app.config.get('DOMAIN_JOIN_VALIDATE_OU', True), True)
    except RuntimeError:
        return True


def host_ldap_validate_ou(data: dict, *, timeout: float = 5.0) -> None:
    """Raise ValidationError if Target OU is missing or not an organizationalUnit.

    A blank Target OU is refused when LDAP is reachable and the domain default
    computer location is still ``CN=Computers``. ``redircmp`` to a real OU
    keeps a blank field valid. Lab-only ``DOMAIN_JOIN_VALIDATE_OU=false``
    skips this check (including an explicit Computers-container DN).
    """
    ou = (data.get('domain_ou') or '').strip()
    if not _ou_admit_validate_enabled():
        logging.warning(
            'domain_ou admit validation skipped (DOMAIN_JOIN_VALIDATE_OU=false) '
            'ou=%s',
            ou or '(empty)',
        )
        return
    info = host_ldap_check_join_ou(data, timeout=timeout)
    if info.get('skipped'):
        return
    if info.get('class') in ('invalid_credentials', 'account_restricted'):
        raise ValidationError(info.get('result') or 'Cannot validate domain_ou')
    if info.get('class') == 'empty_default_container':
        raise ValidationError(
            info.get('result')
            or info.get('warning')
            or 'Target OU is empty and the domain default is not an OU'
        )
    if not info.get('ok'):
        raise ValidationError(
            info.get('result') or f'domain_ou is invalid: {ou!r}'
        )


def run_admit_directory_checks(data: dict) -> None:
    """Hostname uniqueness + OU validate when join_domain (best-effort LDAP)."""
    from app.util import as_bool as _as_bool

    if not _as_bool(data.get('join_domain'), False):
        return
    # Ensure normalized creds present
    prepare_join_credentials(data)
    host_ldap_check_computer_exists(data, data.get('hostname') or '')
    host_ldap_validate_ou(data)
