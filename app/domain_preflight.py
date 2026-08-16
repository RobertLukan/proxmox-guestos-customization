"""Operational preflight for AD domain join (DC / DNS reachability)."""
from __future__ import annotations

import logging
import socket

from app.util import as_bool as _as_bool
from app.validators import ValidationError


def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _collect_join_targets(data) -> list[str]:
    """Return host/IP targets to probe for domain join (DNS list first).

    Profile-stored join passwords use the profile's DNS, not request DNS.
    """
    from app.domain_credentials import credential_dns_servers

    targets: list[str] = []
    try:
        dns = credential_dns_servers(data)
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
            safe_domain = str(domain).replace('\r', '').replace('\n', '')[:253]
            logging.info(
                'domain preflight: could not resolve %s (%s)',
                safe_domain,
                type(e).__name__,
            )
    return targets


def check_domain_join_preflight(data, timeout: float = 2.0):
    """Best-effort host reachability check when ``join_domain`` is set.

    Probes TCP 53/88/389 on ``dns_servers`` (and resolved ``domain_name``).
    Any open port on any target is enough to pass (returns ``None``).

    Host unreachable / missing DNS is **advisory**: returns a warning string so
    admit can continue. The guest VLAN may still reach the DC; the in-clone
    credential probe remains the hard gate before Sysprep.

    Returns ``None`` when join is not requested, a target is reachable, or
    nothing to warn about.
    """
    payload = data if isinstance(data, dict) else {}
    if not _as_bool(payload.get('join_domain'), False):
        return None

    targets = _collect_join_targets(payload)
    if not targets:
        payload['host_dc_reachable'] = False
        payload['host_dc_target'] = ''
        msg = (
            'Domain join: no dns_servers (or resolvable domain_name) for GuestOS-host '
            'DC preflight; continuing — in-clone probe / guest DNS will validate the path.'
        )
        logging.warning('join-path: host_dc=unreachable reason=no-targets')
        logging.warning(msg)
        return msg

    ports = (389, 88, 53)
    for host in targets:
        for port in ports:
            if _tcp_reachable(host, port, timeout=timeout):
                payload['host_dc_reachable'] = True
                payload['host_dc_target'] = f'{host}:{port}'
                logging.info('join-path: host_dc=reachable target=%s:%s', host, port)
                logging.info('domain preflight: %s:%s reachable', host, port)
                return None

    tried = ', '.join(f'{h}:53/88/389' for h in targets)
    payload['host_dc_reachable'] = False
    payload['host_dc_target'] = ''
    msg = (
        'Domain join: DC/DNS not reachable from the GuestOS host '
        f'(probed {tried}). Continuing — the guest VLAN may still reach AD; '
        'in-clone credential probe is authoritative before Sysprep.'
    )
    logging.warning('join-path: host_dc=unreachable probed=%s', tried)
    logging.warning(msg)
    return msg
