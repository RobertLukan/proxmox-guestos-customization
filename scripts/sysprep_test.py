#!/usr/bin/env python3
"""Legacy helper — in-place Sysprep is disabled.

Use the GuestOS UI or machine API ``POST /start_sysprep_workflow`` (template →
clone → Sysprep) instead. This script remains only to document the old entry
point and exits non-zero.
"""
import sys

print(
    'In-place Sysprep is disabled.\n'
    'Use Clone + Sysprep from a Windows template (UI or /start_sysprep_workflow).',
    file=sys.stderr,
)
sys.exit(2)
