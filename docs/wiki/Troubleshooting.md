# Troubleshooting

## 1. `/sys/kernel/debug/ec/ec0/io` Missing or Permission Denied
- **Cause:** `ec_sys` is not loaded with `write_support=1`, or UEFI **Secure Boot** kernel lockdown is blocking debugfs EC access.
- **Fix:**
  ```bash
  sudo modprobe -r ec_sys 2>/dev/null || true
  sudo modprobe ec_sys write_support=1
  cat /sys/module/ec_sys/parameters/write_support   # must output Y
  ```
  If `Operation not permitted` persists as `root`, check `mokutil --sb-state` and disable Secure Boot in UEFI setup.

---

## 2. `Power Saving` Shows `RTD3 Auto / D3cold Pending` Instead of `Suspended (D3cold)`
- **Cause:** The NVIDIA GPU is in `power/control=auto`, but the desktop session (e.g., X11/COSMIC/Xwayland running in dedicated `nvidia` graphics mode) currently holds `/dev/nvidia0` open (`runtime_usage > 0`).
- **Fix:** Switch graphics mode to **Hybrid** / **Compute** / **Integrated** (e.g., `system76-power graphics hybrid`) and log out/in so the display server uses the Intel iGPU, allowing the RTX 3050 to enter `D3cold` automatically when idle.

---

## 3. Fan Mode Buttons & Custom Curve Are Locked in `Quiet` or `Power Saving`
- **Expected Behavior:** In `Quiet` (`0x02`) and `Power Saving` (`0x03`), the EC manages cooling using its internal Silent/Eco auto fan curve (`0xD7=0x80` / `0x02, EC[0xDC]=0x1A`). `gui.py` plots the read-only `0x038C`/`0x039A` curve for visibility while disabling manual overrides.
- **Fix:** Switch to **Entertainment** or **Performance** in Tab 1 or the Tray menu to unlock **Auto**, **Max / Full Speed**, **Custom Curve**, and **Fan Offset (0xE7)**.

---

## 4. Tray Icon Missing on COSMIC / GNOME
- Ensure `python3-pyqt5` is installed and check `./service.sh status` for `RegisteredStatusNotifierItems`.
- Restart the tray cleanly:
  ```bash
  ./service.sh tray
  ```
- On GNOME, install/enable the **AppIndicator and KStatusNotifierItem Support** extension (`gnome-shell-extension-appindicator`).

---

## 5. `daemon.py` Socket Offline or Old `--config` Flag After Upgrade
- Check daemon status:
  ```bash
  ./service.sh status
  sudo journalctl -u acer-a715-59g-daemon.service -n 50 --no-pager
  ```
- Re-run `./service.sh install` after updating the repository so `/etc/systemd/system/acer-a715-59g-daemon.service` picks up the latest `ExecStart` (`--config`).

---

## 6. Running Safe Offline Diagnostics
```bash
./gui.sh --dry-run
./power_profile.sh --dry-run powersaving
./fan.sh --dump-xram
./config_store.py --plan
```
