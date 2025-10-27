import os
import json
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Proxmox PVE API credentials
PROXMOX_HOST = os.environ.get('PROXMOX_HOST')
PROXMOX_USER = os.environ.get('PROXMOX_USER')
PROXMOX_PASSWORD = os.environ.get('PROXMOX_PASSWORD')

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

# Secret key for session management
SECRET_KEY = os.environ.get('SECRET_KEY')

# Application Port
PORT = int(os.environ.get('PORT', 5001))

# Celery configuration
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'
