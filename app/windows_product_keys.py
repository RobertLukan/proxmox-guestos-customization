"""Microsoft Generic Volume License Keys (GVLK / KMS client setup keys).

These are public Microsoft keys used to skip the OOBE product-key page after
Sysprep on volume-license Server editions. They do not replace proper
activation (KMS/MAK/AVMA); they only satisfy Setup so GuestOS AutoLogon can run.

Source: https://learn.microsoft.com/en-us/windows-server/get-started/kms-client-activation-keys
"""
from __future__ import annotations

import re

# (year, edition) -> GVLK
_SERVER_GVLK = {
    (2025, 'standard'): 'TVRH6-WHNXV-R9WG3-9XRFY-MY832',
    (2025, 'datacenter'): 'D764K-2NDRG-47T6Q-P8T8W-YP6DF',
    (2022, 'standard'): 'VDYBN-27WPP-V4HQT-9VMD4-VMK7H',
    (2022, 'datacenter'): 'WX4NM-KYWYW-QJJR4-XV3QB-6VM33',
    (2019, 'standard'): 'N69G4-B89J2-4G8F4-WWYCC-J464C',
    (2019, 'datacenter'): 'WMDGN-G9PQG-XVVXX-R3X43-63DFG',
    (2016, 'standard'): 'WC2BQ-8NRM3-FDDYY-2BFGV-KHKQY',
    (2016, 'datacenter'): 'CB7KF-BWN84-R7R2Y-793K2-8XDDG',
}

# Approximate CurrentBuild → Server LTSC year
_BUILD_YEAR = {
    26100: 2025,  # Server 2025
    25398: 2025,
    20348: 2022,  # Server 2022
    17763: 2019,  # Server 2019
    14393: 2016,  # Server 2016
}


def _normalize_edition(edition_id: str = '', caption: str = '') -> str:
    blob = f'{edition_id} {caption}'.lower()
    if 'datacenter' in blob:
        return 'datacenter'
    if 'standard' in blob or 'server' in blob:
        return 'standard'
    return ''


def _detect_year(caption: str = '', build: int = 0) -> int:
    cap = caption or ''
    for year in (2025, 2022, 2019, 2016):
        if str(year) in cap:
            return year
    if build:
        # Exact or nearest lower known build.
        if build in _BUILD_YEAR:
            return _BUILD_YEAR[build]
        known = sorted(_BUILD_YEAR.items())
        year = 0
        for b, y in known:
            if build >= b:
                year = y
        return year
    return 0


def resolve_server_product_key(
    *,
    product_key: str = '',
    edition_id: str = '',
    caption: str = '',
    build: int = 0,
    default_year: int = 2022,
) -> str:
    """Return a product key for unattend specialize, or empty if not Server.

    Preference:
    1. Explicit ``product_key`` from the request (operator override)
    2. GVLK matched from guest edition + year
    3. Empty (caller may skip injecting ProductKey)
    """
    override = (product_key or '').strip().upper()
    if override:
        # Soft validate XXXXX-XXXXX-XXXXX-XXXXX-XXXXX
        if not re.fullmatch(r'[A-Z0-9]{5}(-[A-Z0-9]{5}){4}', override):
            raise ValueError(
                'product_key must look like XXXXX-XXXXX-XXXXX-XXXXX-XXXXX'
            )
        return override

    edition = _normalize_edition(edition_id, caption)
    if not edition:
        return ''
    year = _detect_year(caption, build) or int(default_year or 0) or 2022
    return _SERVER_GVLK.get((year, edition), '')
