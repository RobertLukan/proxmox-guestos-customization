"""Bulk CSV / items validation (duplicates, hostname, IP sanity)."""
from __future__ import annotations

import ipaddress

from app.validators import (
    ValidationError,
    validate_hostname,
    validate_ipv4,
    validate_vlan,
)


def _assert_usable_host_ip(ip_str, field='IP address'):
    """Reject loopback / link-local / multicast / unspecified / limited broadcast."""
    addr = ipaddress.IPv4Address(validate_ipv4(ip_str, field=field))
    if addr.is_loopback:
        raise ValidationError(f'{field} must not be loopback: {ip_str}')
    if addr.is_link_local:
        raise ValidationError(f'{field} must not be link-local (169.254.x.x): {ip_str}')
    if addr.is_multicast:
        raise ValidationError(f'{field} must not be multicast: {ip_str}')
    if addr.is_unspecified:
        raise ValidationError(f'{field} must not be 0.0.0.0: {ip_str}')
    if int(addr) == 0xFFFFFFFF:
        raise ValidationError(f'{field} must not be 255.255.255.255: {ip_str}')
    return str(addr)


def validate_bulk_items(items, network_mode='static'):
    """Validate bulk item list; raise ValidationError on first hard failure.

    Checks:
    - non-empty hostname, NetBIOS-safe
    - no duplicate hostnames (case-insensitive)
    - static: required usable IPv4, no duplicate IPs
    - optional VLAN 1–4094
    """
    if not isinstance(items, list) or not items:
        raise ValidationError('items is required and must be a non-empty array.')

    mode = (network_mode or 'static').strip().lower()
    seen_host = {}
    seen_ip = {}

    for idx, raw in enumerate(items, start=1):
        item = raw if isinstance(raw, dict) else {}
        try:
            hostname = validate_hostname(item.get('hostname'))
        except ValidationError as e:
            raise ValidationError(f'Row {idx}: {e}') from e

        key = hostname.lower()
        if key in seen_host:
            raise ValidationError(
                f'Duplicate hostname {hostname!r} in rows {seen_host[key]} and {idx}.'
            )
        seen_host[key] = idx

        if item.get('vlan') not in (None, ''):
            try:
                validate_vlan(item.get('vlan'))
            except ValidationError as e:
                raise ValidationError(f'Row {idx}: {e}') from e

        if mode == 'static':
            ip_raw = item.get('ip_address')
            if not ip_raw:
                raise ValidationError(f'Row {idx}: ip_address is required in static mode.')
            try:
                ip = _assert_usable_host_ip(ip_raw, field='ip_address')
            except ValidationError as e:
                raise ValidationError(f'Row {idx}: {e}') from e
            if ip in seen_ip:
                raise ValidationError(
                    f'Duplicate IP {ip} in rows {seen_ip[ip]} and {idx}.'
                )
            seen_ip[ip] = idx
            item['ip_address'] = ip
        item['hostname'] = hostname

    return items
