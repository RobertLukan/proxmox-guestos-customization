"""Operational preflight for AD domain join (DC / DNS reachability)."""
from __future__ import annotations

import logging
import socket

from app.util import as_bool as _as_bool
from app.validators import ValidationError, validate_dns_servers


def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _collect_join_targets(data) -> list[str]:
    """Return host/IP targets to probe for domain join (DNS list first)."""
    targets: list[str] = []
    try:
        dns = validate_dns_servers(data.get('dns_servers'), allow_ipv6=True)
    except ValidationError:
        dns = []
    for item in dns:
        if item and item not in targets:
            targets.append(item)

    domain = (data.get('domain_name') or '').strip().rstrip('.')
    if domain and domain not in targets:
        # Best-effort: resolve DC name / domain DNS; ignore resolution failures.
        try:
            infos = socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
            for info in infos:
                addr = info[4][0]
                if addr and addr not in targets:
                    targets.append(addr)
        except OSError as e:
            logging.info('domain preflight: could not resolve %s: %s', domain, e)
    return targets


def check_domain_join_preflight(data, timeout: float = 2.0):
    """Raise ValidationError when join is requested but no DC/DNS endpoint answers.

    Probes TCP 53 (DNS), 88 (Kerberos), and 389 (LDAP) on configured
    ``dns_servers`` (and resolved ``domain_name`` addresses). Any single open
    port on any target is enough to pass.

    Returns ``None`` when join is not requested or a target is reachable.
    """
    if not _as_bool((data or {}).get('join_domain'), False):
        return None

    targets = _collect_join_targets(data or {})
    if not targets:
        raise ValidationError(
            'Domain join requested but no dns_servers (or resolvable domain_name) '
            'were provided to locate a domain controller. Set dns_servers to the DC '
            'or domain DNS IP before joining.'
        )

    ports = (389, 88, 53)
    for host in targets:
        for port in ports:
            if _tcp_reachable(host, port, timeout=timeout):
                logging.info(
                    'domain preflight: %s:%s reachable', host, port
                )
                return None

    tried = ', '.join(f'{h}:53/88/389' for h in targets)
    raise ValidationError(
        'Domain join requested but no domain controller / DNS endpoint is reachable '
        f'(probed {tried}). Power on the DC, fix routing, or correct dns_servers.'
    )
