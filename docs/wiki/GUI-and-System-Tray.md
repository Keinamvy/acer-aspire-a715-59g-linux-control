# GUI and System Tray

## Native GTK3 Control Center (`gui.py` / `gui.sh`)

Launch the Control Center from your application menu (**Acer Aspire A715-59G Control Center**) or via terminal:

```bash
./gui.sh                                   # Normal launch (auto-applies config.json if enabled)
./gui.sh --dry-run                         # Preview commands without hardware/config writes
./gui.sh --config /path/to/config.json     # Use custom config path
./gui.sh --no-auto-apply                   # Skip applying saved config on startup
./gui.sh --ec-file /path/to/ec0_io.bin     # Offline 256-byte EC snapshot fallback
```

### HeaderBar & Global Controls
- **Left (`Daemon Connected (Live EC)` / `Unlock Live EC (pkexec)`):**
  - Automatically displays **`Daemon Connected (Live EC)`** when `/run/acer-a715-59g-control.sock` responds.
  - Falls back to **`Unlock Live EC (pkexec)`** (`--worker` privileged JSON helper) if the daemon is not running.
- **Right:**
  - **Dry-Run** switch: routes all actions through `--dry-run` without touching EC hardware or `config.json`.
  - **Auto-apply** switch: toggles `auto_apply_on_launch` in `config.json`.
  - **Apply Config** button: immediately reapplies all saved settings in `config.json` and refreshes the UI.
  - **Refresh** button: forces an immediate full refresh across all 5 tabs (including `peripherals.sh`).
  - **Auto-Poll (2s)** switch: periodically polls `power_profile.sh`, `fan.sh`, `brightness.sh`, `rgb.sh`, and `battery.sh` every 2 seconds (skipping `peripherals.sh` during background polls to minimize EC `0xB8` mailbox traffic).
- **Bottom:** Collapsible **Activity Log** displaying executed commands, stdout/stderr, and execution latency.

### The 5 Tabs
1. **Power Profile:** 4 profile cards (`Quiet`, `Power Saving`, `Entertainment`, `Performance`), optional OS RAPL PL1/PL2 ceiling overrides (`--pl1`, `--pl2`, `--no-rapl`, `--no-os`), live EC `PL1T`/`PL2T` PECI card, and dGPU/RTD3 telemetry.
2. **Fan & Thermal:** Live temperatures, CPU/GPU RPM & duty bars, dGPU temperature/power, interactive `640×260` pure-SVG dual fan curve plot (`GdkPixbuf.PixbufLoader("svg")` — no `python3-cairo` required), CPU/GPU 4-node spinboxes, `Fan Offset (0xE7)`, and stock 3-mode selector (`Auto`, `Max / Full Speed`, `Custom Curve`, `Reset Factory`).
3. **Keyboard RGB:** `KBType=6` single-zone brightness (`0..255` + presets), RGB sliders & hex entry, 11 SVG color swatches, `--cc-scale` toggle, 15-color `breath` effect, and backlight idle timeout (`0xD4`).
4. **Battery & FlexiCharger:** Battery %/status/cycle telemetry, quick presets (`60%–80%`, `70%–85%`, `100%`), custom Start/Stop percentage spinboxes, and live EC XRAM `0x06D0..0x06D2` state highlight.
5. **Peripherals & Lid:** Webcam (`0xB8 0xA1`), Trackpad (`0xB8 0xA5` + `ELAN`), Lid State (`EC[0x03]`), Wake-on-Lid, Lid-Close behavior (`Suspend` vs `Ignore / Clamshell`), Win-Key Lock, Fn/Win Swap, and Fn-Lock.

---

## System Tray Indicator (`tray.py`)

`tray.py` provides a `PyQt5` `QSystemTrayIcon` indicator exporting `org.kde.StatusNotifierWatcher` and `com.canonical.dbusmenu` (tested on Pop!_OS COSMIC).

### Tray Features
- **Dynamic Icon & Tooltip:** Color-coded icon reflecting the active power profile (`Quiet`, `Power Saving`, `Entertainment`, `Performance`) plus live CPU/GPU temperatures, RPMs, and battery status in the tooltip.
- **Single-Instance Lock:** Uses a non-blocking `fcntl.flock` lock (`${XDG_RUNTIME_DIR:-/tmp}/acer-a715-59g-tray.lock`) so duplicate tray launches exit cleanly.
- **Quick Submenus:**
  - **Power Profile** (`Quiet`, `Power Saving`, `Entertainment`, `Performance`)
  - **Fan Mode** (`Auto`, `Max / Full Speed`, `Custom Curve`, `Reset Factory` — automatically disabled with read-only status when `Quiet` or `Power Saving` is active)
  - **Keyboard RGB** (color presets + `Breath Effect` + `Off`)
  - **Battery FlexiCharger** (`60%–80%`, `70%–85%`, `100%`)
  - **Peripherals & Lid** (quick toggles)
  - **Auto-Apply Config on Launch** & **Reapply Saved Config**
  - **Open Control Center** (launches `gui.sh`)
