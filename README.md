# Proxmox GuestOS Utility

A Flask web application to automate the cloning, configuration, and sysprepping of Windows virtual machines in a Proxmox VE environment.

## Features

-   Clone VMs from templates.
-   Reconfigure network settings of existing VMs.
-   Assign an IP address, default gateway, network mask, join VM to a domain.
-   Run Sysprep on new or existing VMs with custom unattended settings (work in progress).
-   Background task management with Celery.
-   Web-based UI for all operations.

## Project Status

**This project is currently under active development.**

-   The **WinRM-based reconfiguration** features (cloning and reconfiguring network settings) are considered stable and working.
-   The **Sysprep-based workflows** are still experimental and under development. In theory, they could offer a simpler approach to VM configuration, but they require more thorough testing. Use them with caution.

## Workflow Overview

The main workflow of the application is to reconfigure a cloned Windows VM. Here is a high-level overview of the steps:

1.  **Clone VM:** A new VM is cloned from a prepared Proxmox template.
2.  **Temporary Network:** A temporary network interface is attached to the new VM, on a network with a DHCP server. This interface is used for the initial configuration.
3.  **Get IP:** The application waits for the VM to boot and get an IP address from the DHCP server on the temporary network.
4.  **WinRM Connection:** The application connects to the VM using WinRM over the temporary network.
5.  **Reconfiguration:** The user provides the new network settings (static IP, hostname, etc.) through the web UI. The application then applies these settings to the primary network interface of the VM.
6.  **Final Reboot:** The VM is rebooted to apply the new settings.
7.  **Cleanup:** The temporary network interface is detached from the VM.

## Design Choices

### Why WinRM instead of Cloud-Init?

This project uses WinRM (Windows Remote Management) for the initial configuration of the guest VMs instead of a tool like Cloud-Init. The primary reason for this choice is that **WinRM is a native, built-in feature of modern Windows Server operating systems.**

This approach has several advantages:
-   **No Additional Software:** There is no need to install and configure any third-party software like Cloud-Init or an equivalent on the Windows template. This simplifies the template creation process.
-   **Robust and Native:** WinRM is Microsoft's standard protocol for remote management and is well-documented and robust.
-   **Firewall Friendly:** It requires only a single port (5985 for HTTP) to be opened on the firewall, which is easy to manage.

While Cloud-Init is a powerful tool and the standard for cloud environments, it is not a native part of Windows and requires a specific implementation (e.g., Cloudbase-Init) to be installed and configured on the base image. By leveraging the native WinRM capabilities, this project aims to keep the template requirements as minimal as possible.

## Prerequisites

### Proxmox VE
-   A working Proxmox VE environment.
-   An account with adequate privileges.
    
### Proxmox VM Template(Golden image)
You need a prepared Windows Server VM template in Proxmox. This template is crucial for the workflow to succeed. It must have the following:
-   **Windows Server:** A clean installation of your desired Windows Server version.
-   **QEMU Guest Agent:** The `qemu-guest-agent` must be installed and running. This is used for communication between the Proxmox host and the guest VM.
-   **VirtIO Drivers:** The VirtIO drivers for Windows must be installed.
-   **WinRM Configuration:** Windows Remote Management (WinRM) must be enabled and configured to allow plain text authentication and unencrypted traffic. This is because the initial connection is on a temporary, isolated network. You can configure this with the following PowerShell commands:
    ```powershell
    winrm quickconfig -q
    winrm set winrm/config/service/auth @{Basic="true"}
    winrm set winrm/config/service @{AllowUnencrypted="true"}
    ```
-   **Windows Firewall:** The Windows Firewall must be configured to allow incoming WinRM traffic on port 5985 (HTTP). You can use the following PowerShell command:
    ```powershell
    New-NetFirewallRule -Name "WinRM-HTTP" -DisplayName "WinRM-HTTP" -Protocol TCP -LocalPort 5985 -Action Allow -Enabled True
    ```
-   Make sure to run sysprep after everything is configured. I will provide a very simple example of sysprep unattend.xml file that configures Administrator password and accepts EULA.
-   VM shuts down it self once sysprep is done. Convert that VM to a PVE VM template.
### Network
-   **DHCP Server:** You need a DHCP server on the network that you will use for the temporary WinRM connection (the network of your `TEMP_BRIDGE`). The cloned VMs will rely on this to get their initial IP address.
-   **Application Host Network:** The server/VM where this Flask application is running must have a network interface in the same subnet as the temporary WinRM network. This is necessary for the application to be able to connect to the guest VMs. It can be hosted on a DHCP server, a server will need to have two network cards.

### Software
-   Python 3
-   Redis (for Celery)
-   A reverse proxy like Nginx or HAProxy (highly recommended as this software was configured to be behind reverse proxy). 

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
    The default password is `changeme`. You should log in and change it (this feature is not implemented yet, you can change it directly in the database or by creating a new user).

## Configuration

The application is configured using environment variables in the `.env` file. 
                                                                                                                              
1.  **Start the Web Server:**
    For development:
    ```bash
    flask run
    ```
    For production, use a WSGI server like Gunicorn, managed by a process manager like `systemd`.
    ```bash
    gunicorn --bind 0.0.0.0:8000 wsgi:app
    ```

2.  **Start the Celery Worker:**
    In a separate terminal, start the Celery worker.
    ```bash
    source venv/bin/activate
    celery -A app.celery worker --loglevel=info
    ```
    For production, you should run the Celery worker as a `systemd` service. See the `guestos-celery.service` file for an example.

## Usage

1.  Open your web browser and navigate to the application's URL.
2.  Follow the on-screen instructions to clone, configure, or sysprep your VMs.

## Acknowledgements

This project was developed in collaboration with Google's Gemini model. A significant portion of the code, configuration, and documentation was generated by Gemini in response to the user's requests and guidance.
