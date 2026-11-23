# Acer Aspire A715-59G Linux Control Suite

**`acer-aspire-a715-59g-linux-control`** is a WMI-free Linux hardware control suite for the **Acer Aspire A715-59G** (Board `RTH`, BIOS `V1.16`, Clevo Control Center 3.0 `v7.039` platform), providing:

- **Direct EC + Sysfs Bash Controllers** (`power_profile.sh`, `fan.sh`, `brightness.sh`, `rgb.sh`, `battery.sh`, `peripherals.sh`)
- **Privileged Root EC Daemon** (`daemon.py`) with `SO_PEERCRED` AF_UNIX socket IPC and `--boot-init` hardware restoration
- **Unified Persistent Config** (`config_store.py`, `~/.config/acer-a715-59g-control/config.json`)
- **PyQt5 System Tray Indicator** (`tray.py`) using `org.kde.StatusNotifierWatcher` & `com.canonical.dbusmenu` (COSMIC / KDE / GNOME AppIndicator)
- **Native GTK3 5-Tab Control Center GUI** (`gui.py` / `gui.sh`) with zero `python3-cairo` dependency

---

## Key Design Principles

1. **100% WMI-Free Runtime:** Zero dependency on custom WMI kernel modules (`acer_rth_wmi`) or `acpi_call`. All Embedded Controller reads and writes go through standard Linux `ec_sys` (`/sys/kernel/debug/ec/ec0/io`), paired with `intel-rapl:0`, `intel-rapl-mmio:0`, `nvidia-smi`, and PCI Runtime PM (`/sys/bus/pci/devices/0000:01:00.0/power/control`).
2. **Unprivileged Desktop UI:** Both the GTK3 GUI and PyQt5 tray run as your regular desktop user and talk to `daemon.py` over `/run/acer-a715-59g-control.sock` without repeated password prompts (with a `pkexec --worker` fallback if the daemon is not installed).
3. **Serialized Hardware Access:** `daemon.py` serializes every EC mailbox (`0xF8..0xFD`), XRAM (`0xB1`/`0xB2`), RAPL, and GPU operation under a single global `EC_EXEC_LOCK` (`threading.Lock()`).
4. **Automatic Persistence & Launch Reapply:** Every applied setting updates `~/.config/acer-a715-59g-control/config.json`, which is automatically reapplied on boot (`daemon.py --boot-init`) and app launch (`gui.py` / `tray.py`) when `auto_apply_on_launch` is `true`.

---

## Feature Overview

| Subsystem | Capabilities | Wiki Guide |
|---|---|---|
| **Power & dGPU** | `Quiet` (`15/30W`, `25W` dGPU), `Power Saving` (`15/30W`, RTD3 `D3cold`), `Entertainment` (`45/115W`, `60W`), `Performance` (`90/115W`, `75W`), `--pl1`/`--pl2` | [Power Profiles and dGPU](Power-Profiles-and-dGPU) |
| **Fans & Thermals** | Stock 3 modes (`Auto`, `Max`, `Custom Curve` + `Reset`), dual 4-node CPU/GPU XRAM curves, `Fan Offset (0xE7)`, read-only curve plotting in Quiet/Eco | [Fans and Thermal Curves](Fans-and-Thermal-Curves) |
| **Keyboard (`KBType=6`)** | 1-zone brightness (`0..255`), RGB color + `--cc-scale`, 15-color breathing (`0xCA sub 0x0F`), idle timeout (`0xD4`) | [Keyboard Lighting (KBType=6)](Keyboard-Lighting-KBType6) |
| **Battery** | EC telemetry + Direct EC `0xCB` FlexiCharger (`80%` / `100%` / custom thresholds) + live XRAM `0x06D0..0x06D2` readback | [Battery FlexiCharger](Battery-FlexiCharger) |
| **Peripherals & Lid** | Webcam (`0xB8 0xA1`), Trackpad (`0xB8 0xA5` + `ELAN`), Lid Wake, Clamshell `systemd-inhibit`, Win-Key / Fn-Swap / Fn-Lock | [Peripherals and Clamshell](Peripherals-and-Clamshell) |

---

## Quick Start

```bash
git clone https://github.com/Keinamvy/acer-aspire-a715-59g-linux-control.git
cd acer-aspire-a715-59g-linux-control
./service.sh install
./gui.sh
```

See [Installation and Setup](Installation-and-Setup) for Debian `.deb` installation, dependencies, and user-only setup.
