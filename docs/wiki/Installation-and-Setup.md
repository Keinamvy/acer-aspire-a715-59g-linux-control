# Installation and Setup

## Prerequisites

### 1. Kernel `ec_sys` Driver
The suite reads and writes EC RAM through `/sys/kernel/debug/ec/ec0/io`, which requires `ec_sys` loaded with `write_support=1`:

```bash
sudo modprobe ec_sys write_support=1
```

When the root daemon (`acer-a715-59g-daemon.service`) is installed, `daemon.py --boot-init` loads `ec_sys write_support=1` automatically at startup.

> [!IMPORTANT]
> **UEFI Secure Boot:** Linux kernel lockdown under Secure Boot blocks `ec_sys` write access (`write_support=1` / `/sys/kernel/debug`). Disable Secure Boot in BIOS if `/sys/kernel/debug/ec/ec0/io` is unavailable or read-only as `root`.

### 2. System Packages (Ubuntu / Pop!_OS / Debian)
```bash
sudo apt update
sudo apt install -y \
  python3 python3-gi gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 \
  python3-pyqt5 policykit-1
```

Optional runtime tools (already standard on most systems):
- `nvidia-smi` (from NVIDIA driver package) for GPU power caps (`25W`/`60W`/`75W`) and RTD3 persistence release (`-pm 0`)
- `systemd-inhibit` (`systemd`) for Clamshell `lid-close ignore` mode
- `powerprofilesctl` (`power-profiles-daemon`) for OS power-profile sync (skip with `--no-os`)

---

## Option A: Install via `.deb` Release Package

Download `acer-aspire-a715-59g-linux-control_1.0.0_all.deb` from the [GitHub Releases](https://github.com/Keinamvy/acer-aspire-a715-59g-linux-control/releases) page:

```bash
sudo apt install ./acer-aspire-a715-59g-linux-control_1.0.0_all.deb
```

The package installs:
- Application files to `/opt/acer-aspire-a715-59g-linux-control/`
- CLI wrappers `acer-control-gui` and `acer-control-service` in `/usr/bin/`
- Desktop entry `/usr/share/applications/acer-a715-59g-control.desktop`
- Autostart entry `/etc/xdg/autostart/acer-a715-59g-tray.desktop`

Then run as your normal desktop user (not `root`) to install and enable the root daemon + user tray systemd services for your UID/GID and `~/.config/acer-a715-59g-control/config.json`:

```bash
acer-control-service install
```

---

## Option B: Install from Source (`git clone`)

```bash
git clone https://github.com/Keinamvy/acer-aspire-a715-59g-linux-control.git
cd acer-aspire-a715-59g-linux-control
./service.sh install
```

`./service.sh install` automatically:
1. Renders `WorkingDirectory`, `--uid`, `--gid`, `--cache-dir`, and `--config` into `/etc/systemd/system/acer-a715-59g-daemon.service` using your current repository directory (`$SCRIPT_DIR`) and home directory (`$TARGET_HOME`), then enables and starts the root daemon.
2. Renders `$SCRIPT_DIR` paths into:
   - `~/.config/systemd/user/acer-a715-59g-tray.service`
   - `~/.config/autostart/acer-a715-59g-tray.desktop`
   - `~/.local/share/applications/acer-a715-59g-control.desktop`
3. Enables and starts `acer-a715-59g-tray.service`.

---

## Managing Services (`service.sh`)

```bash
./service.sh status          # Check daemon unit, socket ping, ec_sys, config.json, tray unit & SNI
./service.sh install-user    # Install only user tray, autostart & desktop launcher (no root daemon)
./service.sh tray            # Start/restart only the tray indicator
./service.sh restart         # Restart both daemon (if installed) and tray services
./service.sh stop            # Stop daemon and tray services
./service.sh uninstall       # Disable & remove installed systemd units and desktop entries
./service.sh --dry-run install
```

---

## Verifying Your Installation

```bash
./config_store.py --self-test
./daemon.py --self-test
./gui.sh --self-test
./tray.py --self-test
```

All self-tests run in isolated temporary directories (`tempfile.TemporaryDirectory`) and never mutate your real `~/.config/acer-a715-59g-control/config.json` or live EC hardware.
