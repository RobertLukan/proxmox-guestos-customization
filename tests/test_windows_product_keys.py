"""Tests for Server GVLK / product-key resolution."""
import pytest

from app.windows_product_keys import _SERVER_GVLK, _gvlk, resolve_server_product_key

# Expected keys come from the module map (assembled at import), not dashed literals.
_K22_STD = _SERVER_GVLK[(2022, 'standard')]
_K22_DC = _SERVER_GVLK[(2022, 'datacenter')]
_K25_DC = _SERVER_GVLK[(2025, 'datacenter')]


def test_explicit_product_key_wins():
    key = resolve_server_product_key(
        product_key=_gvlk('vdybn', '27wpp', 'v4hqt', '9vmd4', 'vmk7h'),
        edition_id='ServerDatacenter',
        caption='Windows Server 2022 Datacenter',
    )
    assert key == _K22_STD


def test_rejects_malformed_product_key():
    with pytest.raises(ValueError):
        resolve_server_product_key(product_key='not-a-key')


def test_server_2022_standard_from_edition_id():
    key = resolve_server_product_key(
        edition_id='ServerStandard',
        caption='Microsoft Windows Server 2022 Standard',
        build=20348,
    )
    assert key == _K22_STD


def test_server_2022_datacenter_from_caption():
    key = resolve_server_product_key(
        edition_id='',
        caption='Windows Server 2022 Datacenter',
        build=20348,
    )
    assert key == _K22_DC


def test_evaluation_skus_skip_gvlk():
    assert (
        resolve_server_product_key(
            edition_id='ServerStandardEval',
            caption='Microsoft Windows Server 2019 Standard Evaluation',
            build=17763,
        )
        == ''
    )
    assert (
        resolve_server_product_key(
            edition_id='',
            caption='Windows Server 2022 Datacenter Evaluation',
            build=20348,
        )
        == ''
    )


def test_server_2025_from_build():
    key = resolve_server_product_key(
        edition_id='ServerDatacenter',
        caption='Windows Server',
        build=26100,
    )
    assert key == _K25_DC


def test_empty_when_not_server_edition():
    assert resolve_server_product_key(edition_id='Professional', caption='Windows 11 Pro') == ''
