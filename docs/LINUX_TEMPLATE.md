# Linux cloud-init templates (GuestOS 2.7+)

GuestOS clones a **Proxmox Linux template** (`ostype` `l24` / `l26`) and applies
**Proxmox cloud-init** (hostname, IPv4/IPv6, DNS, `ciuser` / SSH keys / password),
then verifies via the **QEMU Guest Agent**.

Default disk posture is **Simple**: one large root (`/`). Advanced multi-disk
layouts are opt-in via `manage_disks` (API).

## Build a golden image

1. Download a cloud image (Ubuntu 24.04 or Debian 12 amd64 `.img` / `.qcow2`).
2. Create a VM (UEFI + `efidisk` is fine) and **import the disk** (not as CD-ROM):

   ```bash
   qm importdisk <VMID> /path/to/image.img <STORAGE> --format qcow2
   ```

   Attach the unused disk as `scsi0`, boot order `scsi0` first.
3. Add a **Cloud-Init** drive (IDE/SCSI CD-ROM).
4. Enable the guest agent in the VM options (`agent: 1`).
5. First boot: set a temporary static IP (or DHCP if your lab bridge has a server),
   SSH/console in, then:

   ```bash
   sudo apt update && sudo apt install -y qemu-guest-agent
   sudo systemctl enable --now qemu-guest-agent
   sudo cloud-init clean --logs
   # optional: clear machine-id for unique clones
   sudo truncate -s 0 /etc/machine-id
   sudo rm -f /var/lib/dbus/machine-id
   sudo ln -s /etc/machine-id /var/lib/dbus/machine-id
   ```

6. Clear baked-in `ipconfig0` / passwords you do not want in the template.
7. Shut down cleanly and convert:

   ```bash
   qm template <VMID>
   ```

8. Tag optionally (`guestos-linux`) — not required; GuestOS filters by `ostype`.

## API

`POST /start_linux_cloudinit_workflow` (Bearer / session+CSRF)

```json
{
  "template_vmid": 137,
  "hostname": "linux-app-01",
  "cores": 2,
  "ram": 4096,
  "bridge": "vmbr0",
  "network_mode": "static",
  "ip_address": "192.168.123.50",
  "netmask": 24,
  "gateway": "192.168.123.1",
  "dns_servers": "192.168.123.191",
  "ciuser": "ubuntu",
  "sshkeys": "ssh-ed25519 AAAA…",
  "manage_disks": false
}
```

Use `network_mode: "dhcp"` when a DHCP server exists on the guest bridge.
`cipassword` is stashed off the Celery payload (same path as Windows admin password).

### Optional: grow OS disk

```json
"os_disk_gb": 40
```

Resizes the boot disk on Proxmox before power-on; Ubuntu/Debian cloud-init
**growpart** expands `/` on first boot.

### Optional: freeze after success

```json
"detach_cloudinit_after_ready": true
```

After verify succeeds, GuestOS **powers off** the clone, detaches the Cloud-Init
CDROM (and clears PVE `ipconfig*` / `ciuser` fields), then **powers on** again.
Hot-unplug while running is unreliable on Proxmox. Guest settings already on disk
stay stable. Leave this **false** for golden-image templates.

## Lab notes

- Host-only bridges without a DHCP server need **static** cloud-init config.
- QGA must be running in the template before clones will verify.
- Growing the OS disk (`manage_disks` + `role=os` + `grow_to_gb`) relies on
  cloud-init **growpart** inside the guest on first boot.
