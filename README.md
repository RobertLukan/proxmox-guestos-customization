# Proxmox GuestOS Utility

A Flask web application to automate the cloning, configuration, and sysprepping of Windows virtual machines in a Proxmox VE environment.

## Recent Improvements

-   **Security hardening:** eliminated PowerShell command injection in the WinRM/Sysprep flows (all guest values are validated and passed via a Base64/JSON `$p` object), stopped leaking WinRM/domain credentials to the browser (secrets are now resolved server-side), and added CSRF protection to every form and JSON endpoint.
-   **Auth/config:** the app fails fast if `SECRET_KEY` is unset, session cookies are hardened, TLS verification for the Proxmox API is configurable, and an in-app change-password page was added.
-   **Packaging & CI:** added a `Dockerfile` + `docker-compose.yml`, pinned dependencies, a `pytest` test suite, and a GitHub Actions workflow.
-   WinRM is disabled centrally via Group Policy, so the previous in-guest WinRM-disable code was removed.

## Features

-   Clone VMs from templates.
-   Reconfigure network settings of existing VMs.
-   Assign an IP address, default gateway, and network mask. Join the VM to a domain.
-   Supports multiple VLANs/network configurations. Users can select a Domain Profile that has preconfigured DNS servers, VLAN, and user credentials to join a VM to a domain.
-   Run Sysprep on new or existing VMs with custom unattended settings (work in progress).
-   Background task management with Celery.
-   Web-based UI for all operations.

## Project Status

**This project is currently under active development.**

-   The **WinRM-based reconfiguration** features (cloning and reconfiguring network settings) are considered stable and working.
-   The **Sysprep-based workflows** now validate their inputs and verify the VM after Sysprep (wait for shutdown, power back on, confirm the guest agent), but are still considered experimental. Use them with caution.

## Workflow Overview

The main workflow of the application is to reconfigure a cloned Windows VM. The following is a high-level overview of the steps:

1.  **Clone VM:** A new VM is cloned from a prepared Proxmox template. The hostname provided at this stage is used for both the Proxmox VM name and the Windows hostname during reconfiguration.
2.  **Temporary Network:** A temporary network interface is attached to the new VM on a network with a DHCP server. This interface is used for the initial configuration.
3.  **Get IP:** The application waits for the VM to boot and get an IP address from the DHCP server on the temporary network.
4.  **WinRM Connection:** The application connects to the VM using WinRM over the temporary network.
5.  **Reconfiguration:** The user provides the new network settings (static IP, hostname, etc.) through the web UI. The application then applies these settings to the VM's primary network interface.
6.  **Final Reboot:** The VM is rebooted to apply the new settings.
7.  **Cleanup:** The temporary network interface is detached from the VM.

## Design Choices

### Why WinRM instead of Cloud-Init?

This project uses WinRM (Windows Remote Management) for the initial configuration of guest VMs instead of a tool like Cloud-Init. The primary reason for this choice is that **WinRM is a native, built-in feature of modern Windows Server operating systems.**

This approach has several advantages:
-   **No Additional Software:** There is no need to install and configure any third-party software, such as Cloud-Init or an equivalent, on the Windows template. This simplifies the template creation process.
-   **Robust and Native:** WinRM is Microsoft's standard protocol for remote management and is well-documented and robust.
-   **Firewall Friendly:** It requires only a single port (5985 for HTTP) to be opened on the firewall, which is easy to manage.

While Cloud-Init is a powerful tool and the standard for cloud environments, it is not a native part of Windows and requires a specific implementation (e.g., Cloudbase-Init) to be installed and configured on the base image. By leveraging native WinRM capabilities, this project aims to keep the template requirements as minimal as possible.

## Prerequisites

### Proxmox VE
-   A working Proxmox VE environment.
-   An account with adequate privileges.
    
### Proxmox VM Template(Golden image)
You need a prepared Windows Server VM template in Proxmox. This template is crucial for the workflow to succeed and must have the following:
-   **Windows Server:** A clean installation of your desired Windows Server version.
-   **QEMU Guest Agent:** The `qemu-guest-agent` must be installed and running. This is used for communication between the Proxmox host and the guest VM.
-   **VirtIO Drivers:** The VirtIO drivers for Windows must be installed.
-   **WinRM Configuration:** Windows Remote Management (WinRM) must be enabled and configured to allow plain text authentication and unencrypted traffic because the initial connection is on a temporary, isolated network. You can configure this with the following PowerShell commands:
    ```powershell
    winrm quickconfig -q
    winrm set winrm/config/service/auth @{Basic="true"}
    winrm set winrm/config/service @{AllowUnencrypted="true"}
    ```
-   **Windows Firewall:** The Windows Firewall must be configured to allow incoming WinRM traffic on port 5985 (HTTP). You can use the following PowerShell command:
    ```powershell
    New-NetFirewallRule -Name "WinRM-HTTP" -DisplayName "WinRM-HTTP" -Protocol TCP -LocalPort 5985 -Action Allow -Enabled True
    ```
-   Make sure to run sysprep after everything is configured. A very simple example of a sysprep unattend.xml file that configures the Administrator password and accepts the EULA will be provided.
-   The VM shuts down itself once sysprep is done. Convert that VM to a PVE VM template.
### Network
-   **DHCP Server:** You need a DHCP server on the network that you will use for the temporary WinRM connection (the network of your `TEMP_BRIDGE`). The cloned VMs will rely on this to get their initial IP address.
-   **Application Host Network:** The server/VM where this Flask application is running must have a network interface in the same subnet as the temporary WinRM network. This is necessary for the application to be able to connect to the guest VMs. It can be hosted on a DHCP server, but the server will need to have two network cards.
-   **WinRM network:** Use a dedicated L2 network (VLAN) that is not routed due to security considerations. 
-   **Bridge Configuration:** The application assumes that your `TEMP_BRIDGE` (for WinRM) is a standard access port on a single VLAN, while the `PRIMARY_BRIDGE` (for the VMs) is a trunk port that can carry multiple VLANs. This is the most common setup, but it can be modified in the code if your environment is different. I am open to feedback on how to make this more flexible. 

### Software
-   Python 3
-   Redis (for Celery)
-   A reverse proxy like Nginx or HAProxy is highly recommended, as this software was configured to be behind a reverse proxy. 

## Installation and Setup

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/RobertLukan/proxmox-guestos-customization/
    cd proxmox-guestos-customization
    ```

2.  **Create a virtual environment and install dependencies:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Configure the application:**
    Copy the example environment file and edit it with your settings.
    ```bash
    cp .env.example .env
    nano .env
    ```
    See the Configuration section below for details on the variables.

4.  **Initialize the database:**
    Run this command once to create the database tables and a default user.
    ```bash
    python3 init_db.py
    ```
    The default password is `changeme`. Log in and change it immediately using the **Change Password** link on the home page.

## Running with Docker (recommended)

A `Dockerfile` and `docker-compose.yml` are provided that run the web server, the Celery worker, and Redis together.

1.  Copy and edit the environment file (make sure `SECRET_KEY` is set and `BEHIND_REVERSE_PROXY=False` for direct HTTP access):
    ```bash
    cp .env.example .env
    nano .env
    ```
2.  Build and start the stack:
    ```bash
    docker compose up -d --build
    ```
    The web service runs `init_db.py` automatically on startup, points Celery at the bundled Redis service, and persists the SQLite database in a named volume. The app is available at `http://127.0.0.1:5001`.

## Running the Application (manual)

To run the application manually, you need to start both the Flask web server and the Celery worker.

### 1. Start the Web Server

You can run the web server in two modes:

#### Without a Reverse Proxy (for development)

1.  Make sure `BEHIND_REVERSE_PROXY` is set to `False` in your `.env` file.
2.  Run the application directly:

    ```bash
    python3 run.py
    ```

    The application will be accessible at `http://127.0.0.1:5001`.

#### With a Reverse Proxy (for production)

1.  Make sure `BEHIND_REVERSE_PROXY` is set to `True` in your `.env` file.
2.  Configure your reverse proxy to forward requests to the application.
3.  Use a WSGI server like Gunicorn to run the application:

    ```bash
    gunicorn --bind 0.0.0.0:5001 wsgi:app
    ```

### 2. Start the Celery Worker

In a separate terminal, start the Celery worker. Make sure to activate the virtual environment first.

```bash
source venv/bin/activate
celery -A app.celery worker --loglevel=info
```

For production, you should run the Celery worker as a `systemd` service. See the `guestos-celery.service` file for an example.

## Configuration


The application is configured using environment variables in the `.env` file. Below is a description of the key variables:

-   `PROXMOX_HOST`, `PROXMOX_USER`, `PROXMOX_PASSWORD`: Your Proxmox server details.
-   `PROXMOX_VERIFY_SSL`: Verify the Proxmox API TLS certificate (`False` by default for self-signed homelab certs; set `True` with a trusted cert).
-   `WINRM_USERNAME`, `WINRM_PASSWORD`: Default credentials for connecting to guest VMs (resolved server-side, never sent to the browser).
-   `WINRM_SUBNET`, `PRIMARY_BRIDGE`, `TEMP_BRIDGE`: Network configuration (see comments in `.env.example`).
-   `SECRET_KEY`: **Required.** A long, random string to secure sessions and CSRF tokens; the app refuses to start without it.
-   `BEHIND_REVERSE_PROXY`: Set `True` when running behind a TLS-terminating reverse proxy (enables ProxyFix and `Secure` session cookies).
-   `DATABASE_URL`: SQLAlchemy database URL (defaults to a local SQLite file under `instance/`).
-   `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`: Redis connection strings for Celery (default to local Redis).
-   `PORT`: The port on which the web application will listen (defaults to `5001`).
-   `DOMAIN_PROFILES_JSON`: A JSON string defining profiles for domain joining, including DNS servers, domain names, and credentials.



## Usage

1.  Open your web browser and navigate to the application's URL.
2.  Follow the on-screen instructions to clone, configure, or sysprep your VMs.

## Screenshots

*Note: These screenshots are from a slightly older version of the application and are for illustrative purposes only.*

### Initial Page
![Initial Page](screenshots/Initial.png)

### Clone VM
![Clone VM](screenshots/Clone.png)

### Reconfigure VM
![Reconfigure VM](screenshots/Reconfigure.png)

### Progress
![Progress](screenshots/Progress.png)

## Development / Testing

Install the dev dependencies and run the test suite:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The tests cover the input validators, the injection-safe PowerShell parameter helper, WinRM IP selection, VM tag handling, and CSRF/auth flows. They mock Proxmox/WinRM, so no live environment is required. The same suite runs in CI via GitHub Actions.

## Security considerations
-   Always run behind a reverse proxy with TLS, and set `BEHIND_REVERSE_PROXY=True`.
-   Do not expose this software outside of your admin network.
-   WinRM communication itself is not encrypted, so keep the temporary WinRM network isolated (a dedicated, non-routed L2/VLAN).
-   All values sent to guests are validated and passed via a Base64/JSON parameter object rather than string interpolation, preventing PowerShell injection.
-   WinRM and domain-join credentials are resolved server-side and are never rendered into pages or accepted as secrets from the browser by default.
-   CSRF protection is enabled for all forms and JSON endpoints; keep `SECRET_KEY` secret and unique per deployment.
-   Change the default `changeme` password immediately after first login.

## Acknowledgements

This project was developed in collaboration with Google's Gemini model. A significant portion of the code, configuration, and documentation was generated by Gemini in response to the user's requests and guidance.
