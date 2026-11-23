# Peripherals, Lid Control, and Clamshell Mode

`peripherals.sh` (and Tab 5 in `gui.py`) controls hardware switches in EC RAM/mailbox alongside Linux input/ACPI sysfs.

---

## Supported Controls

| Feature | Mechanism | Values |
|---|---|---|
| **Webcam** | `ECMD 0xB8` (`0xFA=0xA1`) + `EC[0xDB]` bit 0 | `on`, `off`, `toggle` |
| **Trackpad** | `ECMD 0xB8` (`0xFA=0xA5`) + `EC[0xDB] \|= 0x04` + `/sys/class/input/input*/inhibited` (`*ELAN*`/`*Touchpad*`) | `on`, `off`, `toggle` |
| **Wake on Lid Open** | `EC[0x03]` bit 2 (`LWKE`) + `/sys/devices/LNXSYSTM:00/.../PNP0C0D:00/power/wakeup` | `on`, `off`, `toggle` |
| **Lid-Close Action** | Normal suspend vs detached `systemd-inhibit --what=handle-lid-switch` (PID in `$XDG_CACHE_HOME/acer-a715-59g/lid_inhibit.pid`) | `suspend`, `ignore` (Clamshell), `toggle` |
| **Win-Key Lock** | `EC[0xE2]` (`ECKS`) bit 4 | `on`, `off`, `toggle` |
| **Fn / Win Swap** | `EC[0xE2]` (`ECKS`) bit 3 | `on`, `off`, `toggle` |
| **Fn-Lock** | `EC[0xEC]` (`B2E9`) bit 5 | `on`, `off`, `toggle` |

---

## Background Polling Behavior

In `gui.py`, the 2-second **Auto-Poll** timer refreshes `power_profile.sh`, `fan.sh`, `brightness.sh`, `rgb.sh`, and `battery.sh` — it intentionally **skips `peripherals.sh`** during background polls to avoid unnecessary `0xB8` EC mailbox traffic. Peripherals are refreshed on app launch, manual **Refresh**, and after any Tab 5 toggle.

---

## CLI Usage

```bash
./peripherals.sh status
./peripherals.sh webcam <on|off|toggle>
./peripherals.sh touchpad <on|off|toggle>
./peripherals.sh lid-wake <on|off|toggle>
./peripherals.sh lid-close <suspend|ignore|toggle>
./peripherals.sh win-lock <on|off|toggle>
./peripherals.sh fn-swap <on|off|toggle>
./peripherals.sh fn-lock <on|off|toggle>
```
