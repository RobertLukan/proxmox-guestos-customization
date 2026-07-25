import os
import json
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Application version (VERSION file at repo root; overridable via APP_VERSION).
_VERSION_FILE = Path(__file__).resolve().parent / 'VERSION'
try:
    _file_version = _VERSION_FILE.read_text(encoding='utf-8').strip()
except OSError:
    _file_version = '0.0.0'
APP_VERSION = os.environ.get('APP_VERSION', _file_version) or _file_version

# Machine API tokens for PDM / integrators (Bearer or X-Api-Token).
# Prefer GUESTOS_API_TOKEN for a single token; API_TOKENS may be a comma list
# or a JSON array of strings.
_api_tokens = set()
_single = (os.environ.get('GUESTOS_API_TOKEN') or '').strip()
if _single:
    _api_tokens.add(_single)
_raw_tokens = (os.environ.get('API_TOKENS') or '').strip()
if _raw_tokens:
    try:
        if _raw_tokens.startswith('['):
            _parsed = json.loads(_raw_tokens)
            if isinstance(_parsed, list):
                _api_tokens.update(str(t).strip() for t in _parsed if str(t).strip())
            else:
                print('Warning: API_TOKENS JSON must be an array; ignoring.')
        else:
            _api_tokens.update(t.strip() for t in _raw_tokens.split(',') if t.strip())
    except json.JSONDecodeError:
        print('Warning: Could not decode API_TOKENS. Ignoring.')
API_TOKENS = frozenset(_api_tokens)

# Optional named Proxmox remotes for multi-cluster / PDM (JSON object).
# Example: {"lab": {"host": "pve.lab", "user": "api@pve", "password": "...", "verify_ssl": false}}
PVE_REMOTES_JSON = os.environ.get('PVE_REMOTES_JSON', '{}')
try:
    PVE_REMOTES = json.loads(PVE_REMOTES_JSON)
    if not isinstance(PVE_REMOTES, dict):
        print('Warning: PVE_REMOTES_JSON must be a JSON object. Using empty remotes.')
        PVE_REMOTES = {}
except json.JSONDecodeError:
    print('Warning: Could not decode PVE_REMOTES_JSON. Using empty remotes.')
    PVE_REMOTES = {}

# Helper for parsing boolean environment variables.
def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ('true', '1', 't', 'yes')


# Proxmox PVE API credentials
PROXMOX_HOST = os.environ.get('PROXMOX_HOST')
PROXMOX_USER = os.environ.get('PROXMOX_USER')
PROXMOX_PASSWORD = os.environ.get('PROXMOX_PASSWORD')

# Whether to verify the Proxmox API TLS certificate. Defaults to False to
# preserve the common homelab setup (self-signed PVE certs), but should be set
# to True when a trusted certificate is in place.
PROXMOX_VERIFY_SSL = _env_bool('PROXMOX_VERIFY_SSL', False)

# Domain Profiles - loaded from a JSON string in the environment variable
# See .env.example for the expected format.
DOMAIN_PROFILES_JSON = os.environ.get('DOMAIN_PROFILES_JSON', '{}')
try:
    DOMAIN_PROFILES = json.loads(DOMAIN_PROFILES_JSON)
except json.JSONDecodeError:
    print("Warning: Could not decode DOMAIN_PROFILES_JSON. Using empty profiles.")
    DOMAIN_PROFILES = {}

# WinRM Credentials
WINRM_USERNAME = os.environ.get('WINRM_USERNAME', 'Administrator')
WINRM_PASSWORD = os.environ.get('WINRM_PASSWORD')

# WinRM Subnet for finding the temporary IP
WINRM_SUBNET = os.environ.get('WINRM_SUBNET')

# Network Configuration
PRIMARY_BRIDGE = os.environ.get('PRIMARY_BRIDGE', 'vmbr0')
TEMP_BRIDGE = os.environ.get('TEMP_BRIDGE', 'vmbr1')

# Secret key for session management. Required: without it Flask sessions and
# CSRF tokens are insecure, so fail fast rather than starting up with None.
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Add it to your .env file "
        "(generate one with: python -c 'import secrets; print(secrets.token_hex())')."
    )

# Application Port
PORT = int(os.environ.get('PORT', 5001))

# Database (env-driven; defaults to a local SQLite file under instance/)
SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///site.db')
SQLALCHEMY_TRACK_MODIFICATIONS = False

# Celery configuration (env-driven; defaults to a local Redis instance)
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

# Reverse Proxy Setup
BEHIND_REVERSE_PROXY = os.environ.get('BEHIND_REVERSE_PROXY', 'False').lower() in ('true', '1', 't')
