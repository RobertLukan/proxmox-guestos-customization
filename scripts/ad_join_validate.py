#!/usr/bin/env python3
"""Dry-run / live helpers for Active Directory join validation.

Default mode is non-destructive:
  * load DOMAIN_PROFILES from .env / environment
  * report profile shape (domain, DNS, VLAN, OU, credential presence)
  * optional DNS resolution checks for domain_name / dns_servers

Live clone+Sysprep with join is opt-in via --start-join (destructive).

Examples:
  python3 scripts/ad_join_validate.py
  python3 scripts/ad_join_validate.py --profile "Example Lab" --check-dns
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        os.environ.setdefault(key.strip(), val.strip())


def _profiles() -> dict:
    raw = os.environ.get('DOMAIN_PROFILES_JSON') or '{}'
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f'FAIL DOMAIN_PROFILES_JSON: {e}', file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print('FAIL DOMAIN_PROFILES_JSON must be an object', file=sys.stderr)
        sys.exit(2)
    return data


def _is_placeholder(domain: str) -> bool:
    d = (domain or '').lower()
    return d.endswith('.example.com') or d in ('lab.local', 'example.local', 'corp.local')


def check_dns(host: str) -> tuple[bool, str]:
    try:
        infos = socket.getaddrinfo(host, None)
        addrs = sorted({i[4][0] for i in infos})
        return True, ','.join(addrs[:4])
    except socket.gaierror as e:
        return False, str(e)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--env-file', default='.env')
    p.add_argument('--profile', default='', help='Validate a single profile name')
    p.add_argument('--check-dns', action='store_true', help='Resolve domain_name and dns_servers')
    p.add_argument(
        '--require-real-ad',
        action='store_true',
        help='Exit non-zero when only placeholder example profiles are present.',
    )
    args = p.parse_args()

    _load_dotenv(Path(args.env_file))
    profiles = _profiles()
    if not profiles:
        print('FAIL: no DOMAIN_PROFILES configured')
        return 1

    names = [args.profile] if args.profile else list(profiles)
    real_count = 0
    issues = 0

    for name in names:
        prof = profiles.get(name)
        if not prof:
            print(f'FAIL unknown profile {name!r}')
            return 1
        domain = (prof.get('domain_name') or '').strip()
        dns = (prof.get('dns_servers') or '').strip()
        user = (prof.get('domain_username') or '').strip()
        user_ok = bool(user)
        pass_ok = bool(prof.get('domain_password'))
        vlan = prof.get('vlan')
        ou = (prof.get('domain_ou') or '').strip()
        placeholder = _is_placeholder(domain)
        if not placeholder:
            real_count += 1

        print(f'--- profile {name!r}')
        print(f'    domain={domain or "(missing)"} placeholder={placeholder}')
        print(f'    dns_servers={dns or "(none)"} vlan={vlan} ou={ou or "(none)"}')
        print(f'    credentials: username={"set" if user_ok else "MISSING"} '
              f'password={"set" if pass_ok else "MISSING"}')
        if user_ok and '@' not in user and '\\' not in user and '/' not in user:
            print(
                '    WARN: username looks bare; GuestOS requires UPN '
                '(user@domain.tld) or DOMAIN\\user'
            )
            issues += 1

        if not domain or not user_ok or not pass_ok:
            print('    STATUS: incomplete profile')
            issues += 1
            continue

        if args.check_dns:
            ok, detail = check_dns(domain)
            print(f'    DNS domain_name: {"OK" if ok else "FAIL"} ({detail})')
            if not ok:
                issues += 1
            for server in [s.strip() for s in dns.split(',') if s.strip()]:
                # dns_servers are IPs usually — try resolve anyway
                ok_s, detail_s = check_dns(server)
                print(f'    DNS server {server}: {"OK" if ok_s else "FAIL"} ({detail_s})')

        if placeholder:
            print('    STATUS: placeholder (replace with real AD before live join test)')
        else:
            print('    STATUS: looks like a real AD profile (shape OK)')

    print('---')
    print(f'profiles_checked={len(names)} real_looking={real_count} issues={issues}')
    if args.require_real_ad and real_count == 0:
        print('FAIL: --require-real-ad set but only placeholder profiles found')
        return 3
    if issues:
        return 1
    print('OK dry-run validation complete')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
