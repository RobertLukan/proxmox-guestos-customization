"""Tests for Server GVLK / product-key resolution."""
import pytest

from app.windows_product_keys import resolve_server_product_key


def test_explicit_product_key_wins():
    key = resolve_server_product_key(
        product_key='vdybn-27wpp-v4hqt-9vmd4-vmk7h',
        edition_id='ServerDatacenter',
        caption='Windows Server 2022 Datacenter',
    )
    assert key == 'VDYBN-27WPP-V4HQT-9VMD4-VMK7H'


def test_rejects_malformed_product_key():
    with pytest.raises(ValueError):
        resolve_server_product_key(product_key='not-a-key')


def test_server_2022_standard_from_edition_id():
    key = resolve_server_product_key(
        edition_id='ServerStandard',
        caption='Microsoft Windows Server 2022 Standard',
        build=20348,
    )
    assert key == 'VDYBN-27WPP-V4HQT-9VMD4-VMK7H'


def test_server_2022_datacenter_from_caption():
    key = resolve_server_product_key(
        edition_id='',
        caption='Windows Server 2022 Datacenter Evaluation',
        build=20348,
    )
    assert key == 'WX4NM-KYWYW-QJJR4-XV3QB-6VM33'


def test_server_2025_from_build():
    key = resolve_server_product_key(
        edition_id='ServerDatacenter',
        caption='Windows Server',
        build=26100,
    )
    assert key == 'D764K-2NDRG-47T6Q-P8T8W-YP6DF'


def test_empty_when_not_server_edition():
    assert resolve_server_product_key(edition_id='Professional', caption='Windows 11 Pro') == ''
