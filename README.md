# Acer-Aspire-A715-59G-Control

Reverse-engineered Linux bash control scripts, privileged root daemon, system tray indicator, and native GTK3 Control Center GUI for the **Acer Aspire A715-59G** (Board `RTH`, BIOS `V1.16`, Clevo Control Center 3.0 `v7.039` platform), modeled after the `Acer-Aspire-A715-79G-KeyBoard` project.

The entire suite operates **100% WMI-free** with **zero runtime WMI (`acer_rth_wmi`) or `acpi_call` dependency**: all EC reads/writes go directly through `/sys/kernel/debug/ec/ec0/io` (`ec_sys` using registers and mailbox commands verified from BIOS DSDT `EC81` / `RAM` / `RAM3` / `ECMD`: `0xEB`, `0xD8`, `0xD7`, `0xD9`, `0xE7`, `0xCB`, `0xB1`/`0xB2`, `0xB8`, `0xCA`, `0xD4`, `0x03`, `0xE2`, `0xEC`), paired with standard Linux sysfs (`intel-rapl:0`, `intel-rapl-mmio:0`, `nvidia-smi`, and PCI Runtime PM `/sys/bus/pci/devices/0000:01:00.0/power/control`).

> [!NOTE]
> Any archival ACPI/WMI reverse-engineering helper (`acer_rth_wmi`) is **completely unused** by this suite. Do not compile or load `acer_rth_wmi` to run `acer-aspire-a715-59g-linux-control`.

> [!WARNING]
> These scripts read and write directly to Embedded Controller (EC) RAM (`/sys/kernel/debug/ec/ec0/io`). Incorrect writes can cause unexpected hardware behavior.

---

## Files

| File | Purpose |
|---|---|
| [gui.py](gui.py) | Native GTK3 (`Gtk 3.0`) 5-tab Control Center GUI, status parsers, interactive SVG fan curve editor, daemon socket client (`Apply Config` / `Auto-apply`, config persistence & legacy-daemon fallback), and `--worker` JSON IPC helper |
| [gui.sh](gui.sh) | Launcher wrapper (`exec /usr/bin/python3 "$SCRIPT_DIR/gui.py" "$@"`) |
| [acer-a715-59g-control.desktop](acer-a715-59g-control.desktop) | Desktop entry (`Categories=Settings;HardwareSettings;`) pointing to `gui.sh` |
| [config_store.py](config_store.py) | Unified persistent config store (`~/.config/acer-a715-59g-control/config.json`, schema v1), legacy cache seeder, `flock` atomic recorder, deterministic boot/launch action planner, `--show`/`--plan`/`--self-test` |
| [daemon.py](daemon.py) | Privileged root EC daemon (`--boot-init` `ec_sys` loader & `config.json` restorer, `--config`, AF_UNIX `/run/acer-a715-59g-control.sock` IPC server with `SO_PEERCRED`, `EC_EXEC_LOCK`, `get_config`/`set_config`/`apply_config`, and `--self-test`) |
| [tray.py](tray.py) | PyQt5 `QSystemTrayIcon` indicator & quick-control menu (`org.kde.StatusNotifierWatcher` / `com.canonical.dbusmenu`) with live telemetry badge, `Auto-Apply Config on Launch` / `Reapply Saved Config`, `--config`/`--no-auto-apply`, and `fcntl.flock` single-instance lock |
| [service.sh](service.sh) | Lifecycle & installer script for user tray service, autostart/desktop entries, and root daemon systemd unit (`install` renders `--config` into unit, `install-user`, `status` incl. config check, `start`, `stop`, `restart`, `tray`, `uninstall`, `--dry-run`) |
| [acer-a715-59g-daemon.service](acer-a715-59g-daemon.service) | Root `systemd` service unit running `daemon.py --boot-init` on `/run/acer-a715-59g-control.sock` (`service.sh install` renders `--uid`, `--gid`, `--cache-dir`, and `--config`) |
| [acer-a715-59g-tray.service](acer-a715-59g-tray.service) | User `systemd` unit (`~/.config/systemd/user/`) running `tray.py` under `graphical-session.target` |
| [acer-a715-59g-tray.desktop](acer-a715-59g-tray.desktop) | XDG autostart desktop entry (`~/.config/autostart/`) launching `tray.py` |
| [power_profile.sh](power_profile.sh) | 100% WMI-free 4-profile controller (`0xEB` `APRD=1` + `0xD8` `0x00..0x03`), read-only EC PECI telemetry (`0xF0..0xF4`), static OEM PL1/PL2 RAPL sync (`intel-rapl:0` / `intel-rapl-mmio:0` + `--pl1`/`--pl2`), and dGPU `nvidia-smi -pl` + PCI Runtime PM (`D3cold` in Power Saving) |
| [fan.sh](fan.sh) | EC thermal telemetry, CPU/GPU fan duty/RPM (`0xCE..0xD3`), fan modes (`0xD7` incl. `0x80` `Silent-Auto` / `0x02` `EC[0xDC]=0x1A` `Eco Policy`, `0xD9` reset), offset (`0xE7`), 4-node CPU/GPU curves (`0xB1`/`0xB2` XRAM `0x038C..0x03A7`), `--dump-xram`/`--scan-xram` diagnostics, and D3cold-safe GPU status |
| [brightness.sh](brightness.sh) | Keyboard backlight brightness (`0xCA` subcommand `0x06`, write-only on Single-Zone `KBType=6` (`RGB15Color`) with local state cache) |
| [rgb.sh](rgb.sh) | Single-Zone `KBType=6` (`RGB15Color`) RGB color (`0xCA` sub `0x03`, `0xF9=0x03`), `--cc-scale`, breath (`0xCA` sub `0x0F`), and idle timeout (`0xD4`) |
| [battery.sh](battery.sh) | Battery telemetry (`0x10`, `0x38..0x39`, `0x94..0x95`) and Direct EC FlexiCharger (`0xCB` write / `ECMD 0xB1` XRAM `0x06D0..0x06D2` live readback + residual `0xF8..0xFB` snapshot check, zero local cache, zero WMI) |
| [peripherals.sh](peripherals.sh) | Webcam (`0xB8` `0xA1`), Trackpad (`ELAN0412` + `0xB8` `0xA5`), Lid State/Wake (`EC[0x03]` `LSTE`/`LWKE` + `PNP0C0D:00`), Clamshell Lid-Close Inhibit (`systemd-inhibit`), and Keyboard Locks (`EC[0xE2]`, `EC[0xEC]`) |
| [README.md](README.md) | Hardware register map, GUI/daemon/tray architecture, and CLI usage guide |

---

## Dependencies

- **Kernel EC access (`ec_sys`):** `ec_sys` with `write_support=Y` is the only kernel driver needed (`daemon.py --boot-init` loads it automatically, or on fresh installs):
  ```bash
  sudo modprobe ec_sys write_support=1
  ```
- **Standard Linux Power & GPU Tooling:**
  - Intel RAPL sysfs: `/sys/class/powercap/intel-rapl:0/` and `/sys/class/powercap/intel-rapl-mmio:0/`
  - NVIDIA & PCI Runtime PM: `nvidia-smi` (`-pl` for `25W`/`60W`/`75W` dGPU caps, `-pm 0` before RTD3 suspend) and `/sys/bus/pci/devices/0000:01:00.0/power/control` (plus `0000:01:00.1/power/control` for RTD3 `D3cold`), plus optional `powerprofilesctl` (`--no-os` skips it).
- **GTK3 GUI (`gui.py` / `gui.sh`):** `/usr/bin/python3` with PyGObject (`gi` for `Gtk 3.0`, `Gdk 3.0`, and `GdkPixbuf 2.0` SVG loader via `GdkPixbuf.PixbufLoader("svg")`), plus `pkexec` (polkit) as fallback when unlocking live EC writes without the system daemon. **`python3-cairo` is NOT required** — both the Fan Curve graph and RGB color swatches render pure-Python SVG XML via `GdkPixbuf` into `Gtk.Image` widgets (zero `Gtk.DrawingArea` or `cairo.Context`).
- **PyQt5 System Tray (`tray.py`):** `/usr/bin/python3` with `PyQt5` (`python3-pyqt5` — `QSystemTrayIcon`, `QMenu`, `QPainter`) so `tray.py` registers natively on COSMIC's top panel via `org.kde.StatusNotifierWatcher` and exports its quick-control menu over `com.canonical.dbusmenu`.
- **Peripherals (`peripherals.sh`):** `systemd-inhibit` and `setsid` (standard `systemd` / `util-linux`) for `lid-close ignore` (Clamshell mode).

---

## GTK3 Control Center GUI

### Launching & CLI Options

```bash
./gui.sh                                            # Launch GTK3 GUI (auto-applies saved config if enabled & live)
./gui.sh --dry-run                                  # Start with Dry-Run mode enabled (no hardware writes or config edits)
./gui.sh --config /path/to/config.json              # Use an explicit config.json path (or set ACER_CONTROL_CONFIG)
./gui.sh --no-auto-apply                            # Skip applying saved config.json on startup
./gui.sh --ec-file /path/to/ec0_io.bin                # Explicit EC snapshot file for status fallback
./gui.sh --self-test                                # Run headless-safe parser, worker IPC, and GTK3 checks
./gui.sh --help                                     # Show all CLI flags
```

Full CLI options accepted by [`gui.py`](gui.py) / [`gui.sh`](gui.sh):
- `--dry-run`: Start with the **Dry-Run** switch active across all 6 scripts (skips startup auto-apply and never writes `config.json`).
- `--config CONFIG`: Explicit path to `config.json` (overrides `ACER_CONTROL_CONFIG` and `~/.config/acer-a715-59g-control/config.json`).
- `--no-auto-apply`: Skip auto-applying saved `config.json` settings on startup (manual **Apply Config** remains available).
- `--ec-file EC_FILE`: Explicit 256-byte EC snapshot file for status fallback (`power_profile.sh`, `fan.sh`, `battery.sh`, `peripherals.sh`).
- `--self-test`: Runs headless-safe verification (snapshot/dry-run dispatch, `--worker` JSON IPC round-trip, `pkexec` rc=126/127 error handling, SVG swatch/dual fan-curve `GdkPixbuf` rendering, node clamping & hit-testing, AST zero-`DrawingArea` check, and GTK3 5-tab window/widget verification when `DISPLAY`/`WAYLAND_DISPLAY` is set).
- `--worker`, `--cache-dir CACHE_DIR`, `--uid UID`, `--gid GID`: Internal privileged JSON `stdin`/`stdout` worker flags invoked automatically via `pkexec` when unlocking live EC access.

### Privilege & Execution Architecture

- **Unprivileged GTK3 Frontend:** The GUI (`gi.require_version("Gtk", "3.0")`) runs as your regular desktop user so the GTK interface never runs as `root`.
- **Automatic Daemon Socket Connection (Zero Repeated Prompts):**
  - When [`daemon.py`](daemon.py) is active on `/run/acer-a715-59g-control.sock` (`ACER_CONTROL_SOCK` override supported), both `gui.py` and [`tray.py`](tray.py) auto-connect via `ScriptRunner` (`Daemon Connected (Live EC)`) with zero `pkexec` prompts.
- **Snapshot / Dry-Run Fallback When Locked:**
  - When unprivileged and neither the daemon socket nor `--worker` is active, status queries for `power_profile.sh`, `fan.sh`, `battery.sh`, and `peripherals.sh` fall back to `--ec-file` (if provided or auto-detected), while `brightness.sh`/`rgb.sh` status and any setter button safely execute with `--dry-run` (printing the exact EC/RAPL/sysfs/`nvidia-smi` command without modifying hardware).
  - Turning on the **Dry-Run** HeaderBar switch (or passing `--dry-run`) forces `--dry-run` on all 6 scripts regardless of privilege.
  - If launched directly as `root` (`euid == 0`), scripts run directly against live hardware.
- **Fallback One-Time `Unlock Live EC (pkexec)` `--worker` Helper:**
  - When the system daemon is not installed, clicking **Unlock Live EC (pkexec)** in the HeaderBar prompts once via `pkexec` to spawn a single persistent root helper (`pkexec /usr/bin/python3 .../gui.py --worker --cache-dir <XDG_CACHE_HOME> --uid <uid> --gid <gid>`).
  - The GUI sends single-line JSON requests (`ping`, `script` + `args` restricted to the 6 allowlisted `.sh` scripts, and `shutdown`) over `stdin`/`stdout` pipes.
  - After every worker command, `--worker` preserves `XDG_CACHE_HOME` and recursively `chown`s `${XDG_CACHE_HOME}/acer-a715-59g/` (default `~/.cache/acer-a715-59g/`) back to your `uid:gid` so cached write-only/peripheral state files (`brightness_state`, `rgb_state`, `fan_curve_state`, `peripherals_state`) stay user-owned.

### Interface Overview (5 Tabs)

- **HeaderBar & Status Banner:**
  - Left: **Unlock Live EC (pkexec)** toggle button (updates to **Daemon Connected (Live EC)** when `/run/acer-a715-59g-control.sock` is active, **Live EC Unlocked** when the `--worker` subprocess is active, or **Live EC (Root)** when started as `root`).
  - Right: **Dry-Run** switch, **Auto-apply** switch (persists `auto_apply_on_launch` in `config.json`), **Apply Config** button (force-applies saved `config.json` settings immediately), **Refresh** button, and **Auto-Poll (2s)** switch (`GLib` 2-second telemetry poll — polls `power_profile.sh`, `fan.sh`, `brightness.sh`, `rgb.sh`, and `battery.sh`; skips `peripherals.sh` during background polls to avoid redundant `0xB8` mailbox traffic, refreshing it on startup, manual **Refresh**, and Tab 5 actions; `power_profile.sh status` and `fan.sh status` also skip `nvidia-smi` polling whenever `0000:01:00.0` is absent or `runtime_status == suspended` so `D3cold` stays asleep), above a live mode banner showing `Daemon Connected (Live EC)`, `Live Root EC`, `Dry-Run Mode`, `Snapshot Mode`, or `Unprivileged Locked Mode`.
- **5 Control & Telemetry Tabs:**
  1. **`Power & RAPL`**:
     - Direct EC `0xD8` power profile switching (`0xEB |= 0x40` `APRD=1` followed by `0xF9=0x01, 0xFA=<mode>, 0xF8=0xD8`) across the 4 stock profiles only (zero WMI):
       - **`Quiet (0x02)`**: `EC Silent Auto Fan • 15W/30W OEM PL1/PL2 • 25W Quiet dGPU` (`nvidia-smi -pl 25` + PCI `0000:01:00.0/power/control = on`; EC sets `0xD7 = 0x80` `Silent-Auto` with `EC[0xD9]=0x88, EC[0xDC]=0x1A` using its internal ROM/Flash Silent policy, while Tab 2 keeps the graph & curve editor visible showing the read-only XRAM `0x038C`/`0x039A` auto curve with edits locked).
       - **`Power Saving (0x03)`**: `EC Eco Auto Fan • 15W/30W OEM PL1/PL2 • dGPU Disabled (D3cold)` (`nvidia-smi -pm 0` + PCI `power/control = auto` on `0000:01:00.0` & `0000:01:00.1`; `nvidia-smi` polling skipped once `0000:01:00.0` is missing or `runtime_status == suspended`; EC sets `0xD7 = 0x02` with `EC[0xDC]=0x1A, EC[0xD9]=0x8C` `Auto (0x02, Power Saving Eco Policy)` using its internal ROM/Flash Eco policy, while Tab 2 keeps the graph & curve editor visible showing the read-only XRAM `0x038C`/`0x039A` auto curve with edits locked).
       - **`Entertainment (0x00)`**: `Balanced Fan • 45W/115W OEM PL1/PL2 • 60W dGPU` (`nvidia-smi -pl 60` + `power/control = on`).
       - **`Performance (0x01)`**: `Auto Performance Fan • 90W/115W OEM PL1/PL2 • 75W dGPU` (`nvidia-smi -pl 75` + `power/control = on`).
     - **OS RAPL Ceiling & Sync Options**: explains that EC + BIOS control hardware PECI limits (`0xF0..0xF4`), while `power_profile.sh` applies the static OEM `PL1`/`PL2` table (`15/30W` Quiet, `15/30W` Power Saving, `45/115W` Entertainment, `90/115W` Performance) to `intel-rapl:0` (MSR) and `intel-rapl-mmio:0` (MMIO) unless overridden; includes a collapsed **`Advanced: Optional OS RAPL Software Cap (--pl1 / --pl2)`** expander (`Override PL1` `5..115` W, `Override PL2` `10..150` W), `Sync Linux RAPL` / `Sync OS powerprofilesctl` toggles, `Apply Selected / Custom RAPL`, and 6 live telemetry cards (`Active EC Profile (0xD8)`, `EC PL1T/PL2T Hardware PECI (0xF0..0xF4)`, `OS RAPL Ceiling (MSR)`, `OS RAPL Ceiling (MMIO)`, `dGPU Power & Runtime PM (nvidia-smi / PCI PM)`, and `OEM Table (Direct EC + RAPL)`).
  2. **`Fans & Thermals`**:
     - Thermal bars (`CPU Temp (0x07)`, `Sensor AC1 (0x08)`, `CPU VR (0x0A)`, `dGPU Board (0x0B)`), CPU/GPU fan duty & tach RPM cards (`0xCE`/`0xCF`, `0xD0..0xD3`), NVIDIA GPU (reporting `Suspended (D3cold)` / `0 W` without waking the dGPU when `runtime_status == suspended` or missing) & `Active Fan Mode (0xD7)` cards.
     - **Profile Fan Lock (`Quiet` / `Power Saving`):** Whenever **`Quiet`** (`0x02`) or **`Power Saving`** (`0x03`) is active, the EC executes its internal ROM/Flash Silent (`0xD7=0x80` `Silent-Auto`) or Eco (`0xD7=0x02`, `EC[0xDC]=0x1A` / `EC[0xD9]=0x8C` `Auto (0x02, Power Saving Eco Policy)`) policy: Tab 2 shows a warning banner (`Quiet / Power Saving Profile Active — Showing read-only EC auto fan curve (0x038C/0x039A; switch to Entertainment or Performance to edit Fan Modes & Custom Curve)`), **keeps the Custom Fan Curve editor (`self.fan_curve_sec`) and SVG curve graph visible in read-only mode** (displaying the `"Read-Only EC Auto"` badge, firmware reference envelope, dual CPU `F1` `0x038C` & GPU `F2` `0x039A` auto curves, node handles & `(T, D%)` values, and live temperature/RPM telemetry markers while omitting `0xE7` offset preview), clears active fan-mode highlights, and locks/disables graph dragging/clicks, Custom Curve spinbuttons & buttons, Fan Mode buttons, and Fan Speed Offset (`0xE7`) controls.
     - **Interactive Dual CPU (`F1`) & GPU (`F2`) Draggable 2-Middle-Node SVG Fan Curve Graph**: `30–100°C` vs `0–100%` duty SVG graph rendered via `GdkPixbuf.PixbufLoader("svg")` into a `640×260` `Gtk.Image` inside a `Gtk.EventBox` (zero `Gtk.DrawingArea` or `python3-cairo`). In unlocked profiles, drag **Node 2** or **Node 3** directly on **CPU F1** (cyan `#38bdf8`) or **GPU F2** (emerald `#34d399`) with live neighbor clamping (`T1 < T2 < T3 < 100`, `D1 < D2 < D3 <= 100`; `Node 1` and `Node 4 = 100°C / 100%` fixed), alongside Cool/Warm/Hot bands, active `0xD7` firmware reference envelopes, dashed `0xE7` Fan Offset preview curve, live CPU/GPU operating markers with RPM labels, and rolling 20-sample telemetry trails (in `Quiet`/`Power Saving`, the graph remains visible in read-only mode with node values & telemetry markers).
     - **Custom Fan Curve Spinbutton Editor**: paired `Node 2` / `Node 3` Temp & Duty spinbuttons for **CPU Fan (`F1 • 0x038C`)** and **GPU Fan (`F2 • 0x039A`)**, **Copy CPU -> GPU**, **Reset Default Nodes**, and **Apply Custom CPU & GPU Curve** (dispatches 8-arg `fan.sh custom ...`; unapplied drags/edits set `_curve_dirty` to protect against 2-second auto-poll overwrites until applied, reset, or manually refreshed; stays visible with controls disabled in read-only mode during `Quiet` / `Power Saving`).
     - **3 Stock Fan Modes + `Reset Factory` (`Fan Curve Modes (0xD7 / 0xD9)`):** `Auto` (`0x02`, EC-selected `Perf-Auto 0x08` when `Performance` power mode is active, `Auto (0x02, Power Saving Eco Policy)` with `EC[0xDC]=0x1A` in `Power Saving`, or EC-managed `Silent-Auto 0x80` in `Quiet` — no separate `Perf-Auto` button), `Max / Full Speed` (`0x10`), `Custom Curve` (`0x04`), and `Reset Factory` (`0xD9`), plus `Fan Speed Offset (0xE7 GFOF)` slider/spin (`0..100%`).
  3. **`Keyboard RGB`**:
     - Tailored strictly for this laptop's **Single-Zone `KBType=6` (`RGB15Color`)** keyboard: cached backlight/RGB state cards, `0xCA` sub `0x06` brightness slider (`0..255`) + 6 presets (`Off (0)` through `Full (255)`), `0xCA` sub `0x03` (`0xF9=0x03`) static color picker / `R`/`G`/`B` spinbuttons / `Control Center /2 Scale (--cc-scale)` toggle / 11 preset color chips (rendered with `14×14` SVG `Gtk.Image` swatches via `GdkPixbuf`), `Breath (KBType=6)` lighting effect button (`0xCA` sub `0x0F` `--effect breath`; applying a static color restores static mode), and `0xD4` backlight idle timeout (`5..3600` s or `Disable Timeout (Always On)`).
  4. **`Battery & FlexiCharger`**:
     - Linux `BAT0` sysfs and EC battery/AC telemetry (`0x10`, `0x38..0x39`, `0x94..0x95`), live Direct EC `Active FlexiCharger (Direct EC 0xCB / XRAM 0x06D0..0x06D2)` card queried via `ECMD 0xB1` from EC XRAM `0x06D0..0x06D2` (`Enabled (<start>%–<stop>%)`, `Disabled (100% Full Charge)`, or `Ready (Select 80% Limit or 100% Full)` when unprivileged/dry-run/snapshot without residual `0xCB` in `0xF8..0xFB`, with zero local cache file and zero WMI), 4 presets (`Conservation 60%–80%`, `Balanced 70%–85%`, `Travel / Full 100%`, `Disable FlexiCharger`), and custom `Start Charging Below (%)` (`20..95`) / `Stop Charging At (%)` (`30..100`) controls enforcing `Start < Stop` (`0xCB`).
  5. **`Peripherals & Lid`**:
     - **Webcam (`CCD Hardware USB Power Cut • 0xB8 0xA1`)**: live `0xB8 bit0` / `/dev/video0` card + `Enable Webcam` / `Cut Webcam Power`.
     - **Trackpad (`ELAN0412 + EC 0xB8 0xA5`)**: live `0xB8 bit4` / `/sys/class/input/input*/inhibited` card + `Enable Trackpad` / `Disable Trackpad`.
     - **Lid Controls (`EC[0x03] LSTE/LWKE & systemd-inhibit`)**: Live Lid State (`EC 0x03 LSTE`), Wake on Lid Open (`EC LWKE / PNP0C0D:00` ON/OFF), and Lid-Close Action (`Suspend on Close` vs `Ignore / Clamshell` via `systemd-inhibit --what=handle-lid-switch`).
     - **Keyboard Hardware Locks (`EC[0xE2] / EC[0xEC]`)**: `Win-Key Lock (0xE2 bit4)`, `Fn <-> Win Swap (0xE2 bit3)`, and `Fn-Lock (0xEC bit5)` ON/OFF toggles.
- **Collapsible `Command & Telemetry Log`:** Expandable bottom drawer (`Gtk.Expander`, collapsed by default) logging every executed or previewed command, timing, stdout/stderr output, and a **Clear Log** button.

### Optional Desktop Menu Installation

To add **Acer Aspire A715-59G Control Center** manually to your application launcher (update `Exec=` in [`acer-a715-59g-control.desktop`](acer-a715-59g-control.desktop) first if you relocate this directory; `./service.sh install` and `./service.sh install-user` also install this automatically):

```bash
cp Acer-Aspire-A715-59G-Control/acer-a715-59g-control.desktop ~/.local/share/applications/
desktop-file-validate ~/.local/share/applications/acer-a715-59g-control.desktop
```

---

## System Daemon & System Tray

### Privileged Root Daemon (`daemon.py` & `acer-a715-59g-daemon.service`)

- **Boot Initialization (`--boot-init`):** [`acer-a715-59g-daemon.service`](acer-a715-59g-daemon.service) runs [`daemon.py`](daemon.py) as `root` with `--boot-init --socket /run/acer-a715-59g-control.sock` (plus `--uid`, `--gid`, `--cache-dir ~/.cache`, and `--config ~/.config/acer-a715-59g-control/config.json` rendered automatically by [`service.sh`](service.sh)).
  - Ensures only `ec_sys` is loaded with `write_support=1` (no WMI kernel modules).
  - Initializes `~/.config/acer-a715-59g-control/config.json` via [`config_store.py`](config_store.py) (`ensure_config_file`, seeding from legacy `${XDG_CACHE_HOME:-$HOME/.cache}/acer-a715-59g/` on first run) and applies saved settings in deterministic order (`plan_apply_actions`) when `auto_apply_on_launch` is `true`. FlexiCharger live hardware readback still queries EC XRAM `0x06D0..0x06D2` via `ECMD 0xB1` (with no local battery cache file), while `config.json` stores the user's saved threshold preference (`limit`/`full`). Pass `--no-restore` to skip boot-time config application.
- **AF_UNIX Socket Security, Config RPC & Serialization:**
  - Serves `/run/acer-a715-59g-control.sock` with `chmod 0660` (`root` and `sudo` group / target GID).
  - Verifies every connecting client's kernel credentials via `SO_PEERCRED` (`root` UID `0`, target `--uid`, and `sudo`/`adm` group members).
  - Restricts `script` execution to the 6 allowlisted `.sh` scripts, serializes all EC mailbox, XRAM, RAPL, and PCI/GPU execution across both GUI and Tray under a single global `EC_EXEC_LOCK` (`threading.Lock()`), recursively preserves target `uid:gid` ownership on the cache directory, records successful (`rc == 0`, non-dry-run/non-status) script calls to `config.json`, and handles `get_config`, `set_config` (deep-merge or `replace: true`), and `apply_config` (`force`, `dry_run`) JSON RPC operations.

### Automatic Client Connection (`gui.py` & `tray.py`)

- **Zero Repeated `pkexec` Prompts:** Both [`gui.py`](gui.py) and [`tray.py`](tray.py) automatically probe `/run/acer-a715-59g-control.sock` (overridable via `ACER_CONTROL_SOCK`) inside `ScriptRunner`. When the daemon responds, the GUI HeaderBar displays **`Daemon Connected (Live EC)`** and executes all reads/writes and config reapplies over the socket with zero `pkexec` prompts.
- **`pkexec --worker` Fallback & Legacy-Daemon Fallback:** When `acer-a715-59g-daemon.service` is not installed or stopped, `gui.py` retains **`Unlock Live EC (pkexec)`** (`--worker`) as a one-time fallback, while unprivileged status calls fall back to `--ec-file` snapshots and `--dry-run` previews. On startup, unprivileged GUI/Tray without daemon/worker skip auto-apply cleanly (avoiding repeated `pkexec` prompts). If connected to an older daemon that does not support `get_config`/`apply_config` (or uses a different `--config` file), `ScriptRunner.apply_saved_config` falls back to sequential `daemon-replay` over standard `script` RPCs and `set_auto_apply_on_launch` falls back to local `save_config`.

### COSMIC System Tray Indicator (`tray.py` & `acer-a715-59g-tray.service`)

- **COSMIC Panel Integration & Single-Instance Lock:** [`acer-a715-59g-tray.service`](acer-a715-59g-tray.service) runs [`tray.py`](tray.py) under `~/.config/systemd/user/` (`PartOf=graphical-session.target`), paired with XDG autostart ([`acer-a715-59g-tray.desktop`](acer-a715-59g-tray.desktop) in `~/.config/autostart/`). A non-blocking `fcntl.flock` lock (`${XDG_RUNTIME_DIR:-/tmp}/acer-a715-59g-tray.lock`) ensures only one tray instance runs per user session.
- **Live Telemetry Badge & Static `dbusmenu`:** Uses PyQt5 `QSystemTrayIcon` (`org.kde.StatusNotifierWatcher`) and a static in-place `QMenu` (`com.canonical.dbusmenu`) polled every 4s (`--poll-sec`):
  - **Live Badge & Tooltip:** Renders a 64×64 profile-colored icon displaying live CPU temperature (`--°` when unavailable) and a status dot (amber/orange for Live EC, gray for Snapshot/Locked), with CPU/GPU temps (`GPU: D3cold` when suspended) & RPMs, active profile, `CPU PL: ...` / tooltip `OEM PL1/PL2: ...`, dGPU state (`Quiet 25W` / `Entertainment 60W` / `Performance 75W` / `Disabled (D3cold)` / `RTD3 Auto (D3cold Pending)`), and battery `%` (`Limit <start>%–<stop>%` / `Full 100%` / `Ready (EC 0xCB / XRAM 0x06D0..0x06D2)` / `FlexiCharger Unavailable`) in the tooltip and header rows.
  - **Quick Menus:** **`Open Control Center`** (also triggered by clicking the tray icon; propagates `--config` and `--no-auto-apply`), **`Power Profile`** (`Quiet (15/30W • EC Silent Auto Fan • 25W dGPU)`, `Power Saving (15/30W • EC Eco Auto Fan • dGPU Off/D3cold)`, `Entertainment (45/115W • 60W dGPU)`, `Performance (90/115W • 75W dGPU)`), **`Fan Mode`** (the 3 stock modes `Auto`, `Max / Full Speed`, `Custom Curve` + `Reset Factory`; locked/disabled and unchecked with `Locked: EC Silent-Auto (0x80 • Read-Only)` in `Quiet` and `Locked: EC Eco-Auto (0x02/0x1A • Read-Only)` in `Power Saving`), **`Keyboard Backlight & RGB`** (`KBType=6` 1-zone brightness presets, 7 colors, and `Breathing Effect`), **`Battery FlexiCharger`** (`Conservation (60%–80%)`, `Balanced (70%–85%)`, and `Full Charge (100%)` checkmarks driven directly from `battery.sh status` / EC `0xCB` & live XRAM `0x06D0..0x06D2` via `ECMD 0xB1` with no local cache), **`Peripherals & Lid`** (Webcam, Trackpad, Wake on Lid Open, Lid Close Clamshell, Win-Key Lock, Fn-Lock), **`Auto-Apply Config on Launch`** (checkable toggle persisting `auto_apply_on_launch` in `config.json`), **`Reapply Saved Config`** (force-applies saved `config.json` immediately), **`Refresh Status`**, and **`Quit Tray`**.
  - **Tray CLI Options:** `--dry-run`, `--ec-file EC_FILE`, `--config CONFIG`, `--no-auto-apply`, `--poll-sec POLL_SEC` (`>= 1`, default `4`), and `--self-test`.

### Service Lifecycle Commands (`service.sh`)

```bash
sudo ./service.sh install       # Install/upgrade root daemon unit (with --config) + user tray service, autostart & desktop launcher
./service.sh install-user       # Install & enable/start user tray service, autostart & desktop launcher only (no root)
./service.sh status             # Show daemon unit, socket ping, ec_sys status, config path/auto_apply_on_launch, tray unit & SNI watcher
./service.sh start              # Start daemon (if installed) and user tray service
./service.sh stop               # Stop daemon (if installed) and user tray service
./service.sh restart            # Restart daemon (if installed) and user tray service
./service.sh tray               # Start/restart user tray service (or detached tray.py if unit is not installed)
./service.sh uninstall          # Stop/disable & remove user tray/desktop files and root daemon service (if installed)
./service.sh --dry-run install  # Preview planned filesystem & systemctl operations without making changes
```

> [!NOTE]
> If upgrading from an earlier installation whose `/etc/systemd/system/acer-a715-59g-daemon.service` did not include `--config`, re-run `sudo ./service.sh install` so the installed root systemd unit is updated with `--config <home>/.config/acer-a715-59g-control/config.json`.

---

## Persistent Configuration & Boot/Launch Reapply (`config_store.py`)

[`config_store.py`](config_store.py) provides a single validated JSON configuration file (`schema v1`) shared across [`daemon.py`](daemon.py), [`gui.py`](gui.py), [`tray.py`](tray.py), and [`service.sh`](service.sh):

- **Config Path Precedence (`resolve_config_path`):**
  1. Explicit `--config PATH` CLI argument
  2. `ACER_CONTROL_CONFIG` environment variable
  3. Target user's `~/.config/acer-a715-59g-control/config.json` (via `pwd.getpwuid(uid).pw_dir` when `--uid` resolves, else `XDG_CONFIG_HOME` / current user `pw_dir` / `<cache_dir>/../.config`)
- **Opt-Out Flags:** Pass `--no-auto-apply` to [`gui.sh`](gui.sh) / [`gui.py`](gui.py) or [`tray.py`](tray.py) to skip launch auto-apply for that session, `--no-restore` to [`daemon.py`](daemon.py) `--boot-init`, or toggle **`Auto-apply`** (GUI HeaderBar) / **`Auto-Apply Config on Launch`** (Tray menu) to set `auto_apply_on_launch: false` persistently.
- **Automatic Persistence & Legacy Cache Seeding:**
  - Every successful (`rc == 0`) live, non-dry-run control action executed through `daemon.py`, `gui.py` (`ControlCenterWindow._execute_action`), or `tray.py` (`TrayController.dispatch_action`) updates `config.json` atomically via `fcntl.flock` (`config.json.lock`), temp-file `fsync` + `os.replace`, and `chown` back to target `uid:gid` when running as `root`.
  - Read-only / diagnostic invocations (`--dry-run`, `--help`/`-h`, `--dump-xram`, `--scan-xram`, `--ec-file`, `status`, `get`) never mutate `config.json`. Malformed JSON is automatically backed up to `config.json.bak` and reset to defaults.
  - **First-Run Migration (`seed_from_legacy_cache`):** When `config.json` does not exist yet, `ensure_config_file` / `record_action_file` seeds initial values from legacy `${XDG_CACHE_HOME:-~/.cache}/acer-a715-59g/` files (`brightness_state`, `rgb_state`, `fan_curve_state`, `peripherals_state`), setting `fan.mode = "custom"` only when `fan_curve_state` explicitly contains `active=custom`, `mode=custom`, or `enabled=1`.

### Canonical `config.json` Schema (`version: 1`)

```json
{
  "version": 1,
  "auto_apply_on_launch": true,
  "power": {
    "profile": "entertainment",
    "pl1_w": null,
    "pl2_w": null,
    "no_rapl": false,
    "no_os": false
  },
  "fan": {
    "mode": "auto",
    "offset_pct": 0,
    "cpu_curve": [60, 50, 80, 66],
    "gpu_curve": [60, 58, 80, 66]
  },
  "keyboard": {
    "brightness": 191,
    "mode": "effect",
    "color": null,
    "cc_scale": false,
    "effect": "breath",
    "timeout_s": null
  },
  "battery": {
    "mode": "limit",
    "start_pct": 70,
    "stop_pct": 85
  },
  "peripherals": {
    "webcam": "on",
    "touchpad": "on",
    "lid_wake": "on",
    "lid_close": "suspend",
    "win_lock": "on",
    "fn_swap": "off",
    "fn_lock": "off"
  }
}
```

| Section | Field | Valid Values & Normalization |
|---|---|---|
| Root | `version` | `1` (integer) |
| Root | `auto_apply_on_launch` | `true` \| `false` (default `true`) |
| `power` | `profile` | `null` \| `"quiet"` (`silent`) \| `"powersaving"` (`power-saving`, `power_saving`, `saving`, `saver`, `eco`) \| `"entertainment"` (`balanced`, `ent`) \| `"performance"` (`perf`) |
| `power` | `pl1_w`, `pl2_w` | `null` \| integer `1..250` (cleared to `null` when `--no-rapl` is recorded) |
| `power` | `no_rapl`, `no_os` | `bool` (default `false`; `--no-rapl` cannot be combined with `pl1_w`/`pl2_w`) |
| `fan` | `mode` | `null` \| `"auto"` \| `"mode2"` (`perf-auto`, `perf_auto`) \| `"max"` (`full`) \| `"maxq"` (`max-q`) \| `"custom"` (`curve`) \| `"silent"` (`slow`, `noiseless`) \| `"mode7"` (`fan.sh reset` records `"auto"` + `[60, 60, 80, 70]`) |
| `fan` | `offset_pct` | `null` \| integer `0..100` |
| `fan` | `cpu_curve`, `gpu_curve` | `null` \| 4-int `[t2, d2, t3, d3]` (`0 < t2 < t3 < 100`, `0 < d2 < d3 <= 100`; validator also accepts 4/8-int `fan.curve` or 8-int full curve `[t1, d1, t2, d2, t3, d3, 100, d4]`) |
| `keyboard` | `brightness` | `null` \| integer `0..255` (presets: `"off"`=`0`, `"low"`/`"p1"`=`47`, `"med"`/`"p2"`=`95`, `"high"`/`"p3"`=`143`, `"max"`/`"p4"`=`191`, `"full"`=`255`) |
| `keyboard` | `mode` | `null` \| `"static"` \| `"effect"` (tracked independently of `timeout_s`) |
| `keyboard` | `color` | `null` \| `[R, G, B]` (`0..255` each, or preset name: `blue`, `green`, `red`, `cyan`, `skyblue`, `yellow`, `orange`, `purple`, `pink`, `white`, `off`; preserved when switching to `effect`) |
| `keyboard` | `cc_scale` | `bool` (default `false`; applies `--cc-scale` when reapplying static `color`) |
| `keyboard` | `effect` | `null` \| `"breath"` |
| `keyboard` | `timeout_s` | `null` \| `0` (`"off"`) \| integer `5..65535` |
| `battery` | `mode` | `null` \| `"limit"` (`enable`) \| `"full"` (`disable`) |
| `battery` | `start_pct`, `stop_pct` | Both `null` or integers `20 <= start_pct < stop_pct <= 100` (`mode="limit"` with both omitted defaults to `60`/`80`) |
| `peripherals` | `webcam`, `touchpad`, `lid_wake`, `win_lock`, `fn_swap`, `fn_lock` | `null` \| `"on"` \| `"off"` |
| `peripherals` | `lid_close` | `null` \| `"suspend"` \| `"ignore"` |

### Deterministic Boot & Launch Reapply Order (`plan_apply_actions`)

When `auto_apply_on_launch` is `true` (or when forced via GUI **`Apply Config`**, Tray **`Reapply Saved Config`**, or `apply_config` `force: true`), `plan_apply_actions()` emits script invocations in this deterministic order:

1. **`power_profile.sh`**: `[--no-rapl | --pl1 <W> --pl2 <W>] [--no-os] <profile>` (when `power.profile` is non-null).
2. **`fan.sh`** *(with EC profile guards)*:
   - **`quiet` / `powersaving` Fan-Skip Guard:** All `fan.sh` commands (`offset` and `mode`/`custom`) are skipped completely when `power.profile` is `"quiet"` or `"powersaving"`, preserving the EC's internal Silent-Auto (`0x80`) and Eco Policy (`0x02`/`0x1A`) firmware control.
   - **`performance` Explicit-`auto` Guard:** Under `power.profile == "performance"`, `fan.sh offset <offset_pct>` is still applied if set, while an explicit `fan.sh auto` call is skipped so the EC retains its automatic `Perf-Auto (0x08)` selection.
   - Otherwise, emits `fan.sh offset <offset_pct>` (if set) followed by `fan.sh custom ...` (emitting 8-node CPU+GPU args when both are set, 4-node CPU args if only `cpu_curve` is set, `[60, 60, 80, 70]` + `gpu_curve` if only `gpu_curve` is set, or bare `custom`) or `fan.sh <mode>`.
3. **`brightness.sh`**: `<brightness>` (when non-null).
4. **`rgb.sh`**: `--timeout <timeout_s|off>` first (when `timeout_s` is non-null), followed by `--effect breath` (when `mode == "effect"`) or `[--cc-scale] <r> <g> <b>` / `off` (when `mode == "static"` and `color` is non-null).
5. **`battery.sh`**: `limit <start_pct> <stop_pct>` (when `mode == "limit"`) or `full` (when `mode == "full"`).
6. **`peripherals.sh`**: Subcommands in fixed order (`webcam`, `touchpad`, `lid-wake`, `lid-close`, `win-lock`, `fn-swap`, `fn-lock`) for any non-null entry.

---

## Verified Hardware Support on Aspire A715-59G

Confirmed against Aspire A715-59G BIOS `V1.16` `DSDT.dsl` / `SSDT1.dsl` / `SSDT12.dsl` / `SSDT13.dsl` and live hardware / `ec0_io.bin` probes:

- **Keyboard (Tailored Strictly for Single-Zone `KBType=6` / `RGB15Color`):**
  - This suite targets Aspire A715-59G's **Single-Zone `KBType=6` (`RGB15Color`)** keyboard exclusively (`rgb.sh` and Tab 3 accept only single-zone RGB and `breath` — multi-zone arguments and unsupported effects are rejected).
  - **Static RGB (`0xCA` sub `0x03`):** Writes `0xF9=0x03` (global single-zone target), `0xFA=Blue`, `0xFB=Red`, `0xFC=Green`, `0xFD=0x00` (`0xFD` is unwritten by DSDT `ECMD` `len=5`, written `0x00` by `rgb.sh`). Use `--cc-scale` in `rgb.sh` (or `Control Center /2 Scale` in Tab 3) to match Windows `FnKey`/`CC40` `KBType=6` channel halving (`R/2, G/2, B/2`, clamping `(0,254,129)`/`(0,255,127)` `B` to `70` prior to halving).
  - **15-Color Breathing (`0xCA` sub `0x0F`):** `rgb.sh --effect breath` writes `0xF9=0x0F, 0xFA=0x07, 0xFB=0x10, 0xFC=0x00, 0xFD=0x00, 0xF8=0xCA` (DSDT `PK04 Case 0x3F` / `_DSM 4 Sub 63`). Applying a static color via `0xCA` sub `0x03` (`0xF9=0x03`) restores static mode.
  - **Brightness (`0xCA` sub `0x06`, write-only on `KBType=6`):** `brightness.sh` sets brightness via `0xF9=0x06, 0xFA=level, 0xF8=0xCA` (`_DSM 103 Sub 0xF4`, CC steps `0/47/95/143/191`, raw `0..255`) and tracks applied values in `${XDG_CACHE_HOME:-$HOME/.cache}/acer-a715-59g/brightness_state`.
  - **Backlight Idle Timeout (`0xD4`):** `rgb.sh --timeout <5..65535|off|0>` sets or disables the backlight sleep timer (`0xF9=0xFF|0x00`, `0xFA=sec_hi`, `0xFB=sec_lo`, `0xF8=0xD4`).
- **Fans (`FanCount=2`) & Custom 4-Node Curves (`0x038C..0x03A7`):**
  - Dual fans (CPU `DUT1`/`RPM1` and GPU `DUT2`/`RPM2`), `0xD7` fan modes (`0x01`, `0x02` incl. `EC[0xDC]=0x1A` `Eco Policy` in `Power Saving`, `0x04`, `0x08`, `0x10`, `0x20`, `0x40`, plus EC read-only `0x80` `Silent-Auto` in `Quiet`), `0xD9` fan table reset (`0xF8=0xD9`), and `0xE7` fan offset (`GFOF`).
  - GUI Tab 2 and Tray present the **3 stock Fan Modes** (`Auto`, `Max / Full Speed`, `Custom Curve`) + `Reset Factory` (`0xD9`). Switching to `Performance` (`0xD8=0x01`) causes the EC to select **`Perf-Auto (0x08)`** automatically under `Auto` (no separate `Perf-Auto` button; `fan.sh` CLI retains `perf-auto`/`mode2` and `maxq`/`silent`/`mode7` if invoked directly, plus `--dump-xram` / `--scan-xram` read-only EC/XRAM diagnostics). In `Quiet` (`0x02`: `0xD7=0x80` `Silent-Auto`, `EC[0xD9]=0x88`, `EC[0xDC]=0x1A`) and `Power Saving` (`0x03`: `0xD7=0x02` `Eco Policy`, `EC[0xD9]=0x8C`, `EC[0xDC]=0x1A`), the EC executes its internal ROM/Flash Silent / Eco auto fan policy while `gui.py` keeps `self.fan_curve_sec` and the SVG graph visible in read-only mode showing the XRAM `0x038C`/`0x039A` auto curve & node values, and GUI/Tray lock Fan Mode, Custom Curve, and `Fan Offset` controls.
  - `DSDT.dsl` `RAM3` (`0xFE0B0300`) exposes the 4-node CPU (`F1`, EC XRAM `0x038C..0x0399`) and GPU (`F2`, EC XRAM `0x039A..0x03A7`) curves directly in EC XRAM (the sole curve tables in `XRAM 0x0000..0x1FFF`), which `fan.sh` reads (`0xB1`) and programs (`0xB2`, nodes + BE16 slopes) before activating `0xD7=0x04`.
- **Peripherals (`0xB8`), Lid (`0x03`), Keyboard Locks (`0xE2`, `0xEC`) & Anti-Dust (`0xB8(0xA3)` Unsupported):**
  - `0xB8(0xA1)` capability/state query (`DCHU.GCMD 0x05/0x06/0x09/0x0A` & `DCHU.CPKG`) returns a bitmask in `0xF9`: **bit 0 (`0x01`) = Webcam (`CCD`)**, bit 1 (`0x02`) = WLAN, bit 2 (`0x04`) = Bluetooth, bit 3 (`0x08`) = 3G/WWAN, **bit 4 (`0x10`) = Trackpad**. Toggles write `0xF9=0xC2 (ON) | 0xC3 (OFF)`, `0xFA=0xA1` (Webcam) / `0xA5` (Trackpad), `0xF8=0xB8`.
  - `EC[0x03]` holds live lid switch state (`LSTE` bit 0) and Wake-on-Lid (`LWKE` bit 2, paired with `/sys/bus/platform/devices/PNP0C0D:00/power/wakeup`); `EC[0xE2]` (`ECKS`) holds `Fn/Win Swap` (bit 3) and `Win-Key Lock` (bit 4) while bit 7 is `SetAirplaneOsd`; `EC[0xEC]` (`B2E9`) holds `Fn-Lock` (bit 5).
  - **`0xB8(0xA3)` Anti-Dust is unsupported:** `DSDT.dsl` `_DSM 121` lacks `Sub 40/41` (`0x28`/`0x29`).
- **Battery FlexiCharger (Direct EC `0xCB` / Live XRAM `0x06D0..0x06D2` via `ECMD 0xB1`, Zero WMI):**
  - `battery.sh` reads battery telemetry from `0x10`, `0x38..0x39`, and `0x94..0x95`, writes charge thresholds directly via `FCMD 0xCB` (`0xF9=stop%`, `0xFA=start%`, `0xFB=0x02|0x00`, `0xF8=0xCB`), and reads persistent live thresholds via `ECMD 0xB1` (`0xF9=addr_hi, 0xFA=addr_lo, 0xFB=0x01, 0xFC=0x00, 0xFD=0x00, 0xF8=0xB1` -> `0xF9`) from EC XRAM `0x06D0..0x06D2` (mirrored at `0x0DD0..0x0DD2`: `0x06D0` bit 1 `0x02` = FlexiCharger enable (`0x18` disabled vs `0x1A`/`0x1B` enabled with bit 0 charge-inhibit latch), `0x06D1` = `start%` (`0x3C=60` / `0x5F=95`), `0x06D2` = `stop%` (`0x50=80` / `0x64=100`)) with zero WMI and no local cache file. (`ECMD 0xF1 sub 0x04` was replaced because it returns non-threshold `00 47 93 A8 FF 00` Fan 2 duty bytes on this EC and mailbox `0xF8..0xFD` is overwritten by `0xD8`/`0xD7`/`0xB1` polling; `--ec-file` / non-writable snapshots still inspect residual `0xF8..0xFB` before reporting `Ready`.)
- **Direct EC Power Profiles (`0xEB` + `0xD8`), OS RAPL (`intel-rapl:0`, `intel-rapl-mmio:0`), & dGPU `nvidia-smi` / PCI Runtime PM:**
  - Switching profiles writes `EC[0xEB] |= 0x40` (`APRD=1`) and `FCMD 0xD8` (`0xF9=0x01, 0xFA=<0x00..0x03>, 0xF8=0xD8`), letting EC hardware PECI limits (`0xF0..0xF4` `PL1T`/`PL2T`/`TAUT`, read-only) govern firmware thermal/power behavior while `power_profile.sh` applies the static OEM `PL1 / PL2` table to `intel-rapl:0` and `intel-rapl-mmio:0` (unless `--pl1`/`--pl2` or `--no-rapl`), updates `powerprofilesctl` (unless `--no-os`), and configures the NVIDIA dGPU via `nvidia-smi` + PCI Runtime PM (`0000:01:00.0/power/control` & `0000:01:00.1/power/control`).
  - **Quiet (`0x02`) vs Power Saving (`0x03`):** Both share `15W / 30W` OEM `PL1/PL2` and run their internal ROM/Flash Silent (`Quiet`: `0xD7=0x80` `Silent-Auto`, `EC[0xD9]=0x88`, `EC[0xDC]=0x1A`) / Eco (`Power Saving`: `0xD7=0x02` `Eco Policy`, `EC[0xD9]=0x8C`, `EC[0xDC]=0x1A`) auto policies (with `gui.py` Tab 2 keeping the read-only XRAM `0x038C`/`0x039A` auto curve & `self.fan_curve_sec` editor visible while locking Fan Mode, Custom Curve, and `Fan Offset` edits), but **`Quiet`** keeps the dGPU active at a **`25W`** cap (`nvidia-smi -pl 25` + `power/control = on`), whereas **`Power Saving`** disables Persistence Mode (`nvidia-smi -pm 0`) and sets `power/control = auto` on `0000:01:00.0` / `0000:01:00.1` so RTD3 transitions the dGPU to **`D3cold`**. Both `power_profile.sh` and `fan.sh` skip `nvidia-smi` polling whenever `0000:01:00.0` is absent or `runtime_status == suspended`.
  - **`power_profile.sh` Profile Targets (4 Stock Profiles, 100% WMI-Free):**

    | Profile | EC `0xD8` | Fan Behavior | OEM OS RAPL (`PL1 / PL2`) | OS Profile / dGPU Power & PCI Runtime PM |
    |---|---|---|---|---|
    | `quiet` | `0x02` | EC `Silent-Auto` (`0xD7=0x80`, `EC[0xD9]=0x88`, `EC[0xDC]=0x1A`; read-only `0x038C`/`0x039A` curve shown in app, Tab 2/Tray Locked) | `15W / 30W` | `power-saver` / **`25W` Quiet dGPU** (`nvidia-smi -pl 25`, `0000:01:00.0/power/control = on`) |
    | `powersaving` | `0x03` | EC `Eco Policy` (`0xD7=0x02`, `EC[0xD9]=0x8C`, `EC[0xDC]=0x1A`; read-only `0x038C`/`0x039A` curve shown in app, Tab 2/Tray Locked) | `15W / 30W` | `power-saver` / **dGPU Disabled / RTD3 `D3cold`** (`nvidia-smi -pm 0`, `power/control = auto` on `0000:01:00.{0,1}`; polling skipped when `suspended`/missing) |
    | `entertainment` | `0x00` | Balanced (`Auto 0x02`, `Max 0x10`, `Custom 0x04`) | `45W / 115W` | `balanced` / **`60W` dGPU** (`nvidia-smi -pl 60`, `power/control = on`) |
    | `performance` | `0x01` | Perf-Auto (`0x08` under `Auto`), `Max 0x10`, `Custom 0x04` | `90W / 115W` | `performance` / **`75W` dGPU** (`nvidia-smi -pl 75`, `power/control = on`) |

---

## Verified EC Register & Mailbox Map (`Aspire A715-59G` BIOS `V1.16`)

### Static EC RAM (`0x00..0xF7`) & EC XRAM (`0x038C..0x03A7`, `0x06D0..0x06D2`)

| Offset | DSDT Field | Meaning |
|---|---|---|
| `0x03` | `LSTE` (bit 0), `LWKE` (bit 2) | Live Lid State (`1`=Open, `0`=Closed) and Wake-on-Lid (`1`=Enabled, `0`=Disabled) |
| `0x07` | `TMP` (`TMP_`) | CPU Temperature (`°C`) |
| `0x08` | `AC1` (`AC1_`) | Thermal Sensor 1 (`°C`) |
| `0x0A`, `0x0B` | `DPT1`, `DPT2` | CPU VR / dGPU Board Temperatures (`°C`) |
| `0x10` | `ADP` (`ADP_`), `BAT0` | Bit 0 = AC plugged, Bit 2 = Battery present |
| `0x38..0x39` | `BRS0` (LE16) | Battery Remaining `%` |
| `0x94..0x95` | `CYCN` (LE16) | Battery Cycle Count |
| `0xCE`, `0xCF` | `DUT1`, `DUT2` | CPU / GPU Fan Duty (`0..255`) |
| `0xD0..0xD1` | `RPM1` (BE16) | CPU Fan Raw Tach (`RPM = 2156250 / raw`) |
| `0xD2..0xD3` | `RPM2` (BE16) | GPU Fan Raw Tach (`RPM = 2156250 / raw`) |
| `0xD9`, `0xDC` | `AIRP`, *(EC policy byte)* | Per-profile mode state (`Quiet`: `88, 1A`; `Power Saving`: `8C, 1A`; `Entertainment`: `80, 03`; `Performance`: `84, 03`) |
| `0xDB` | `RINF` (bit 2) | Touchpad EC hotkey/handoff enable flag (`\|= 0x04`) |
| `0xE2` | `ECKS` | Bit 3 (`0x08`) = Fn/Win Swap, Bit 4 (`0x10`) = Win-Key Lock, Bit 6 (`0x40`) = Modern Standby, Bit 7 (`0x80`) = `SetAirplaneOsd` (`_DSM 19`) |
| `0xE7` | `GFOF` | Fan Speed Offset (`0..255`) |
| `0xEB` | `APRD` (bit 6) | `APRD=1` (`\|= 0x40`) set before `0xD8` power profile write |
| `0xEC` | `B2E9` | Bit 5 (`0x20`) = Fn-Lock (`PK04 0x18/0x19`) |
| `0xF0..0xF1` | `PL1T` (LE16) | **Read-only** EC PECI PL1 telemetry (`(val & 0x7FFF) / 8` Watts; not host-writable) |
| `0xF2..0xF3` | `PL2T` (LE16) | **Read-only** EC PECI PL2 telemetry (`(val & 0x7FFF) / 8` Watts; not host-writable) |
| `0xF4` | `TAUT` (8-bit) | **Read-only** EC PECI Tau telemetry (not host-writable) |
| `0x038C..0x0399` | `RAM3` `F1T1..F1R3` | CPU Fan (`F1`) 4-node custom curve in XRAM (`T1..T4` `0x038C..8F`, `D1..D4` `0x0390..93`, BE16 slopes `R12..R34` `0x0394..99`) |
| `0x039A..0x03A7` | `RAM3` `F2T1..F2R3` | GPU Fan (`F2`) 4-node custom curve in XRAM (`T1..T4` `0x039A..9D`, `D1..D4` `0x039E..A1`, BE16 slopes `R12..R34` `0x03A2..A7`) |
| `0x06D0..0x06D2` *(mirror `0x0DD0..0x0DD2`)* | EC XRAM (via `0xB1`) | Persistent live FlexiCharger state: `0x06D0` bit 1 (`0x02`) = enable (`0x18` disabled vs `0x1A`/`0x1B` enabled with bit 0 charge-inhibit latch), `0x06D1` = `start%` (`0x3C=60` / `0x5F=95`), `0x06D2` = `stop%` (`0x50=80` / `0x64=100`) |

### EC Mailbox (`0xF8=FCMD`, `0xF9=FDAT`, `0xFA=FBUF`, `0xFB=FBF1`, `0xFC=FBF2`, `0xFD=FBF3`)
Always write `0xF9..0xFD` **before** writing the command byte to `0xF8` (`FCMD`). Wait for `0xF8` to return to `0x00` before reading responses from `0xFA..0xFD` (or `0xF9..0xFC` for `0xB1`/`0xB8`).

| `FCMD` (`0xF8`) | Purpose | Input (`0xF9..0xFD`) | Output / Values |
|---|---|---|---|
| `0xB1` | XRAM Read | `0xF9=addr_hi, 0xFA=addr_lo, 0xFB=len(1..4), 0xFC=0, 0xFD=0` | `0xF9..`: `len` bytes read from EC XRAM (e.g. `0x038C..0x03A7` fan curves, `0x06D0..0x06D2` FlexiCharger state) |
| `0xB2` | XRAM Write | `0xF9=addr_hi, 0xFA=addr_lo, 0xFB=0x01, 0xFC=byte0, 0xFD=byte1` | Writes 2 bytes to EC XRAM (used for 4-node fan curves) |
| `0xB8` | Peripheral Query | `0xF9=0xA1` | `0xF9` bitmask: bit 0 = Webcam (`CCD`), bit 1 = WLAN, bit 2 = BT, bit 3 = 3G, bit 4 = Trackpad |
| `0xB8` | Peripheral Power / Enable | `0xF9=0xC2(ON)\|0xC3(OFF), 0xFA=<0xA1\|0xA5\|...>` | `0xA1` = Webcam (`CCD`), `0xA2` = WLAN, `0xA3` = BT, `0xA4` = 3G, `0xA5` = Trackpad |
| `0xCA` | KB Brightness (`KBType=6` Write-Only) | `0xF9=0x06, 0xFA=level` | Sets brightness (`0..255`; CC `KBType=6` steps `0/47/95/143/191`, max=`191/0xBF`) |
| `0xCA` | Single-Zone `KBType=6` Static RGB | `0xF9=0x03, 0xFA=Blue, 0xFB=Red, 0xFC=Green, 0xFD=0x00` (`0xFD` unwritten by DSDT `len=5`, `0x00` in `rgb.sh`) | Sets Single-Zone `KBType=6` (`RGB15Color`) static RGB color (`0xF9=0x03`) |
| `0xCA` | `KBType=6` Breathing | `0xF9=0x0F, 0xFA=0x07, 0xFB=0x10, 0xFC=0x00, 0xFD=0x00` | Triggers 15-color breathing (`DSDT` `PK04 0x3F` / `_DSM 4 Sub 63`); static `0xCA` sub `0x03` restores static mode |
| `0xD4` | KB Sleep Timer | `0xF9=0xFF\|0x00, 0xFA=sec_hi, 0xFB=sec_lo` | Backlight idle auto-off timeout in seconds (`0x00`=disable, `0xFF`=enable `5..65535`s) |
| `0xD8` | Power Profile Read | `0xF9=0x00` | `0xFA`: 4 stock modes `0x00`=Entertainment, `0x01`=Performance, `0x02`=Quiet, `0x03`=PowerSaving |
| `0xD8` | Power Profile Write | `0xEB\|=0x40`, then `0xF9=0x01, 0xFA=<0x00..0x03>` | Switches active EC power/thermal profile across the 4 stock modes; `power_profile.sh` applies static OEM `PL1/PL2` (`15/30W`, `15/30W`, `45/115W`, `90/115W`) to OS RAPL + manages dGPU `nvidia-smi`/`power/control` |
| `0xD7` | Fan Mode Read | `0xF9=0x00` | `0xFA`: `0x01`=MaxQ, `0x02`=Auto (`EC[0xDC]=0x1A`/`0xD9=0x8C` in Power Saving Eco Policy), `0x04`=Custom, `0x08`=Perf-Auto/Mode2, `0x10`=Max, `0x20`=Mode7, `0x40`=Silent, `0x80`=Silent-Auto (EC read-only in Quiet) |
| `0xD7` | Fan Mode Write | `0xF9=<0x01\|0x02\|0x04\|0x08\|0x10\|0x20\|0x40>` | Sets active EC fan mode (`0x80` is EC-managed read-only) |
| `0xD9` | Fan Table Reset | *(none)* | Resets EC fan curve to BIOS default (`_DSM 121 Sub 34`) |
| `0xF1` | FlexiCharger Read *(unused here)* | `0xF9=0x04, 0xFA=0x00, 0xFB=0x00, 0xFC=0x00, 0xFD=0x00` | Returns non-threshold `00 47 93 A8 FF 00` (Fan 2 duty bytes) on this EC while `0xF8..0xFD` is overwritten by `0xD8`/`0xD7`/`0xB1` polling; replaced by `0xB1` on XRAM `0x06D0..0x06D2` |
| `0xCB` | FlexiCharger Write | `0xF9=stop%, 0xFA=start%, 0xFB=en(0x02\|0x00)` | Sets battery start/stop charge limits (`0x02`=enable e.g. `80%`, `0x00`=disable `100%`) |

---

## Usage

```bash
# Power Profile, OS RAPL & dGPU Runtime PM (100% WMI-Free)
./power_profile.sh status                  # also supports: --ec-file /path/to/ec0_io.bin status
./power_profile.sh quiet                   # 0x02: 15/30W OEM RAPL, EC Silent-Auto (0xD7=0x80; read-only curve shown, edits locked), 25W Quiet dGPU (nvidia-smi -pl 25, power/control=on)
./power_profile.sh powersaving             # 0x03: 15/30W OEM RAPL, EC Eco Policy (0xD7=0x02/0xDC=0x1A; read-only curve shown, edits locked), dGPU RTD3 D3cold (-pm 0 + power/control=auto)
./power_profile.sh entertainment           # 0x00: 45/115W OEM RAPL, 60W dGPU (nvidia-smi -pl 60, power/control=on)
./power_profile.sh performance             # 0x01: 90/115W OEM RAPL, 75W dGPU (nvidia-smi -pl 75, power/control=on)
./power_profile.sh --pl1 40 --pl2 85 performance  # optional OS RAPL software ceiling override (--no-rapl / --no-os also supported)

# Fans, Thermals & Custom 4-Node Curves
./fan.sh status                            # live temps, RPMs, duties, offset, fan mode & F1/F2 curves (skips nvidia-smi in D3cold; supports --ec-file)
./fan.sh --dump-xram                       # read-only dump of EC RAM 0x00..0xFF, XRAM 0x0300..0x03CF (RAM2/RAM3), and ECMD 0xC0 (0..15)
./fan.sh --scan-xram 0300 00d0             # read-only XRAM hex scan via ECMD 0xB1 (<start_hex> <len_hex>) + monotonic curve hits
./fan.sh auto                              # or: max | custom | reset (CLI also retains perf-auto | mode2 | maxq | silent | mode7)
./fan.sh custom 55 58 75 78                # apply middle nodes (T2=55C 58%, T3=75C 78%) to both CPU F1 & GPU F2
./fan.sh custom 55 58 75 78 62 60 82 85    # independent CPU (55/58, 75/78) & GPU (62/60, 82/85) curves -> XRAM 0x038C..0x03A7 + 0xD7=0x04
./fan.sh offset 20                         # +20% fan speed offset at 0xE7

# Keyboard Backlight & RGB (Single-Zone KBType=6 / RGB15Color)
./brightness.sh status                     # show cached KBType=6 brightness state (write-only 0xCA sub 0x06)
./brightness.sh max                        # off | low | med | high | max | full | 0..255 (raw; CC max=191)
./rgb.sh 255 0 128                         # R G B -> 0xCA sub 0x03 (0xF9=0x03, raw 0..255; add --cc-scale for /2 scaling)
./rgb.sh --cc-scale cyan                   # palette preset with FnKey/CC40 half-range scaling
./rgb.sh --effect breath                   # KBType=6 15-color breathing via 0xCA sub 0x0F
./rgb.sh --timeout 30                      # backlight idle timeout in seconds (5..65535 or 'off'/'0' via 0xD4)
./rgb.sh status                            # show cached color/effect + mailbox bytes

# Battery FlexiCharger (Direct EC 0xCB / XRAM 0x06D0..0x06D2 via ECMD 0xB1, Zero WMI & Zero Cache)
./battery.sh status                        # EC/sysfs telemetry + live XRAM 0x06D0..0x06D2 via 0xB1 (supports --ec-file /path/to/ec0_io.bin status)
./battery.sh enable                        # enable 80% FlexiCharger limit (defaults to 60 80 via EC 0xCB)
./battery.sh limit 70 85                   # start charging at 70%, stop at 85% (EC 0xCB)
./battery.sh full                          # or 'disable' -> allow 100% charging (0xF9=100, 0xFA=80, 0xFB=0x00)

# Peripherals, Lid & Keyboard Hardware Locks
./peripherals.sh status                    # also supports: --ec-file /path/to/ec0_io.bin status
./peripherals.sh webcam <on|off|toggle>    # cut/restore webcam power via EC 0xB8 (0xFA=0xA1)
./peripherals.sh touchpad <on|off|toggle>  # toggle trackpad via EC 0xB8 (0xFA=0xA5), EC[0xDB]|=0x04, & ELAN0412 input*/inhibited
./peripherals.sh lid-wake <on|off|toggle>  # EC[0x03] bit 2 (LWKE) & PNP0C0D:00 power/wakeup
./peripherals.sh lid-close <suspend|ignore|toggle>  # normal suspend vs detached systemd-inhibit clamshell mode
./peripherals.sh win-lock <on|off|toggle>  # Win-Key Lock via EC[0xE2] bit 4 (ECKS)
./peripherals.sh fn-swap <on|off|toggle>   # Fn/Win Swap via EC[0xE2] bit 3 (ECKS)
./peripherals.sh fn-lock <on|off|toggle>   # Fn-Lock via EC[0xEC] bit 5 (B2E9)

# Persistent Configuration & Self-Tests (config_store.py)
./config_store.py --show                   # Print validated config JSON (~/.config/acer-a715-59g-control/config.json)
./config_store.py --plan                   # Preview ordered boot/launch apply commands (--force ignores auto_apply_on_launch=false)
./config_store.py --self-test              # Run isolated config store unit tests
```
