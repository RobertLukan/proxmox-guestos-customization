"""Input validation helpers.

These sanitize user-supplied values before they are written into Sysprep
answer files / guest-agent scripts. Rejecting malformed input early is the
first line of defense against command/script injection.
"""

import ipaddress
import re

# NetBIOS computer names: 1-15 chars, letters/digits/hyphen.
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9-]{1,15}$")
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")
# DNS/AD domain names: one or more dot-separated labels (e.g. corp.example.com).
DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
DOMAIN_RE = re.compile(rf"^(?=.{{1,253}}$){DOMAIN_LABEL}(?:\.{DOMAIN_LABEL})+$")


class ValidationError(ValueError):
    """Raised when a user-supplied value fails validation."""


def validate_ipv4(value, field="IP address"):
    """Return the canonical string form of an IPv4 address or raise."""
    try:
        return str(ipaddress.IPv4Address(str(value).strip()))
    except (ValueError, ipaddress.AddressValueError):
        raise ValidationError(f"Invalid {field}: {value!r}")


def validate_netmask(value):
    """Return an int prefix length in the range 0-32 or raise."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Invalid netmask (expected integer 0-32): {value!r}")
    if not 0 <= n <= 32:
        raise ValidationError(f"Netmask out of range 0-32: {n}")
    return n


def validate_vlan(value):
    """Return an int VLAN id in the range 1-4094, or None when empty."""
    if value in (None, "", "None"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Invalid VLAN (expected integer 1-4094): {value!r}")
    if not 1 <= n <= 4094:
        raise ValidationError(f"VLAN out of range 1-4094: {n}")
    return n


def validate_hostname(value):
    """Return a validated NetBIOS-safe hostname (first DNS label) or raise."""
    label = str(value).strip().split(".")[0]
    if not HOSTNAME_RE.match(label):
        raise ValidationError(
            f"Invalid hostname (1-15 chars, letters/digits/hyphen only): {value!r}"
        )
    return label


def validate_domain(value):
    """Return a lower-cased, validated AD/DNS domain name (e.g. corp.local)."""
    v = str(value or "").strip().lower().rstrip(".")
    if not DOMAIN_RE.match(v):
        raise ValidationError(f"Invalid domain name: {value!r}")
    return v


def validate_mac(value):
    """Return the MAC address unchanged if it is well-formed, else raise."""
    v = str(value).strip()
    if not MAC_RE.match(v):
        raise ValidationError(f"Invalid MAC address: {value!r}")
    return v


def validate_dns_servers(value, allow_ipv6=False):
    """Parse a comma-separated string into a list of validated IP strings.

    Empty/None input yields an empty list. Each non-empty entry must be a valid
    IPv4 address (and IPv6 when ``allow_ipv6`` is true).
    """
    if not value:
        return []
    servers = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        if allow_ipv6 and ':' in part:
            servers.append(validate_ipv6(part, field="DNS server"))
        else:
            servers.append(validate_ipv4(part, field="DNS server"))
    return servers


def validate_ipv6(value, field="IPv6 address"):
    """Return the canonical string form of an IPv6 address or raise."""
    try:
        return str(ipaddress.IPv6Address(str(value).strip()))
    except (ValueError, ipaddress.AddressValueError):
        raise ValidationError(f"Invalid {field}: {value!r}")


def validate_ipv6_prefix(value):
    """Return an int IPv6 prefix length in the range 1-128 or raise."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Invalid IPv6 prefix (expected integer 1-128): {value!r}")
    if not 1 <= n <= 128:
        raise ValidationError(f"IPv6 prefix out of range 1-128: {n}")
    return n


def validate_timezone(value):
    """Return a validated Windows timezone ID from the curated catalog."""
    from app.windows_identity import WINDOWS_TIMEZONES
    v = str(value or '').strip()
    if v not in WINDOWS_TIMEZONES:
        raise ValidationError(f"Unsupported timezone (pick a Windows Time Zone ID): {value!r}")
    return v


def validate_locale(value):
    """Return a validated Windows culture/locale ID from the curated catalog."""
    from app.windows_identity import WINDOWS_LOCALES
    v = str(value or '').strip()
    if v not in WINDOWS_LOCALES:
        raise ValidationError(f"Unsupported locale: {value!r}")
    return v


def validate_workgroup(value):
    """Return a NetBIOS-safe workgroup name (1-15 chars) or raise."""
    label = str(value or '').strip()
    if not HOSTNAME_RE.match(label):
        raise ValidationError(
            f"Invalid workgroup (1-15 chars, letters/digits/hyphen only): {value!r}"
        )
    return label


# Linux bridge / Proxmox SDN VNet iface names (no commas or '=' — those alter netN).
BRIDGE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


def validate_bridge(value, field='bridge'):
    """Return a safe Proxmox bridge / SDN VNet name, or None when empty.

    Rejects commas, spaces, and ``=`` so the value cannot inject extra QEMU
    ``netN`` key/value pairs when interpolated into ``bridge=…``.
    """
    if value in (None, '', 'None'):
        return None
    name = str(value).strip()
    if not BRIDGE_RE.match(name) or ',' in name or '=' in name:
        raise ValidationError(
            f"Invalid {field} (use a bridge or SDN VNet name; "
            f"letters/digits/._- only, no commas): {value!r}"
        )
    return name
