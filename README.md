# ◈ GENESIS — Virtual Linux Laboratory

A local-first **FastAPI + SQLite + libvirt** app for defining Linux learning labs, planning QEMU/KVM virtual machines, viewing a relationship map, recording actions, and tracking hands-on engineering challenges.

**Honest feature status:** Real VM provisioning uses `virt-install` on the **host machine**. VM creation has not been tested on a real Fedora/libvirt host in this build environment; test with a disposable ISO and confirm permissions. Browser VM consoles, arbitrary drag-and-drop networking, automatic grading, snapshots/rollback, and AI instructors are **not implemented** in v1. The visualization is an inventory graph, not a network emulator. The VM provisioning path uses the libvirt default network (typically NAT) and may create disks and boot a VM when you explicitly confirm. There are no VM delete operations.

## Fedora requirements

Install the underlying virtualization stack first:

```bash
sudo dnf install -y python3 python3-pip qemu-kvm libvirt virt-install virt-viewer
sudo systemctl enable --now libvirtd
virsh -c qemu:///system list --all
```

Some Fedora installations use modular libvirt daemons instead of `libvirtd`; configure the relevant services for your version. You need **authorized access to the system libvirt instance** via polkit or an appropriate administration arrangement. Avoid running this web server as root.

## Run the app

```bash
cd genesis
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GENESIS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn app.main:app --host 127.0.0.1 --port 8484
```

Open http://127.0.0.1:8484 and click **API Token**. Enter the exact token from your shell. API documentation is at http://127.0.0.1:8484/docs (supply token header for requests).

## How to use

1. Create a **Lab**.
2. Click **Plan VM** and enter the path to an existing **Linux installer ISO on the same Fedora host** (`/var/lib/libvirt/images/...iso` or `/home/...iso`). Planning alone creates only a database record.
3. Click **Provision VM** and confirm to call `virt-install` with a new qcow2 disk under `/var/lib/libvirt/images/`. Requires a working libvirt default network, enough disk/RAM, and permission to access the ISO.
4. Complete installation using a **local console viewer** such as `virt-viewer`; a browser VNC console is not yet included. Use `virt-viewer <vm-name>` as appropriate for your system.
5. Use Start/Graceful Shutdown for GENESIS-managed VMs only. Graceful shutdown relies on the guest cooperating.
6. Create challenges and mark them done yourself. The app does not verify correctness.

**Never expose this app to the public Internet.** It can control virtual machines with your libvirt permissions. Bind to loopback, keep GENESIS_TOKEN secret, and use SSH tunnels for remote access. This version is a single-operator lab prototype with no multi-user authorization or TLS termination. The same API token protects all API operations, including provisioning; static HTML is publicly served if you bind to an exposed interface. Intended for your own trusted workstation only. Keep VM installer images from trusted sources.

## API

- `GET /api/overview` — libvirt availability and counts
- `GET/POST /api/labs` — labs
- `GET/POST /api/machines` — machine inventory and planning
- `POST /api/machines/{id}/provision?confirm=true` — create a new VM
- `POST /api/machines/{id}/power/start?confirm=true` — start VM
- `POST /api/machines/{id}/power/shutdown?confirm=true` — graceful shutdown
- `GET/POST /api/challenges` — challenges
- `POST /api/challenges/{id}/complete` — self-report completion
- `GET /api/events` — activity timeline

Set `GENESIS_DB` and `GENESIS_LIBVIRT_URI` to override defaults.

## Tests

```bash
pytest -q
```

## Technology

Python / FastAPI / Pydantic / SQLite / libvirt CLI / QEMU-KVM / HTML / CSS / JavaScript. No Node build step. MIT licensed.
