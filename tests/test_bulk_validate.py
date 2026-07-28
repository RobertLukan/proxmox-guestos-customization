"""Tests for bulk CSV / items validation."""
from __future__ import annotations

import pytest

from app.bulk_validate import validate_bulk_items
from app.validators import ValidationError


def test_validate_bulk_items_accepts_static():
    items = [
        {'hostname': 'VDI-001', 'ip_address': '192.168.10.101', 'vlan': 210},
        {'hostname': 'VDI-002', 'ip_address': '192.168.10.102'},
    ]
    out = validate_bulk_items(items, network_mode='static')
    assert out[0]['hostname'] == 'VDI-001'
    assert out[1]['ip_address'] == '192.168.10.102'


def test_validate_bulk_rejects_duplicate_hostname():
    with pytest.raises(ValidationError, match='Duplicate hostname'):
        validate_bulk_items(
            [
                {'hostname': 'VDI-001', 'ip_address': '10.0.0.1'},
                {'hostname': 'vdi-001', 'ip_address': '10.0.0.2'},
            ],
            network_mode='static',
        )


def test_validate_bulk_rejects_duplicate_ip():
    with pytest.raises(ValidationError, match='Duplicate IP'):
        validate_bulk_items(
            [
                {'hostname': 'VDI-001', 'ip_address': '10.0.0.1'},
                {'hostname': 'VDI-002', 'ip_address': '10.0.0.1'},
            ],
            network_mode='static',
        )


def test_validate_bulk_rejects_loopback_and_link_local():
    with pytest.raises(ValidationError, match='loopback'):
        validate_bulk_items(
            [{'hostname': 'VDI-001', 'ip_address': '127.0.0.1'}],
            network_mode='static',
        )
    with pytest.raises(ValidationError, match='link-local'):
        validate_bulk_items(
            [{'hostname': 'VDI-001', 'ip_address': '169.254.1.1'}],
            network_mode='static',
        )


def test_validate_bulk_dhcp_skips_ip():
    out = validate_bulk_items(
        [{'hostname': 'VDI-001', 'vlan': '20'}, {'hostname': 'VDI-002'}],
        network_mode='dhcp',
    )
    assert len(out) == 2
