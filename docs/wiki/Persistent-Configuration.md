# Persistent Configuration & Auto-Reapply on Launch

`config_store.py` maintains a unified configuration file at `~/.config/acer-a715-59g-control/config.json` (overridable via `--config <path>` or `ACER_CONTROL_CONFIG`) shared by `daemon.py`, `gui.py`, and `tray.py`.

---

## How Persistence & Launch Reapply Work

1. **First-Run Legacy Migration:** When `config.json` does not exist yet, `ensure_config_file()` seeds initial values from legacy `${XDG_CACHE_HOME:-~/.cache}/acer-a715-59g/{brightness_state,rgb_state,fan_curve_state,peripherals_state}` if present.
2. **Automatic Recording:** Every successful hardware write (`rc == 0`, excluding `--dry-run`, `--ec-file`, `status`, `--dump-xram`, `--scan-xram`) updates `config.json` via `fcntl.flock` + atomic temp replace (with `.bak` backup recovery if malformed JSON is ever encountered).
3. **Auto-Reapply on Boot & App Launch:** When `auto_apply_on_launch` is `true`:
   - `daemon.py --boot-init` reapplies `config.json` at system boot (unless `--no-restore` is passed).
   - `gui.py` and `tray.py` reapply `config.json` on startup (unless `--dry-run` or `--no-auto-apply` is passed).
   - **Manual Controls:** GUI HeaderBar **Auto-apply** switch & **Apply Config** button; Tray **Auto-Apply Config on Launch** toggle & **Reapply Saved Config**.

---

## Configuration Schema (`version: 1`)

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

---

## Deterministic Apply Order & Safety Guards

`plan_apply_actions()` outputs commands in safe hardware order:
1. `power_profile.sh` (`[--no-rapl | --pl1 W --pl2 W] [--no-os] <profile>`)
2. `fan.sh`:
   - **Skipped** whenever `power.profile` is `"quiet"` or `"powersaving"` so Silent/Eco EC auto fan policies are never overridden.
   - Applies `offset` first (if set), then `custom` / `max` / `auto` (skipping explicit `fan.sh auto` under `"performance"` so `0x08` Perf-Auto stays intact).
3. `brightness.sh` (`<0..255>`)
4. `rgb.sh` (`--timeout <val>` first if set, then `off` / `--effect breath` / `[--cc-scale] <color>`)
5. `battery.sh` (`limit <start> <stop>` or `full`)
6. `peripherals.sh` (each non-null peripheral setting)

---

## CLI Inspection

```bash
./config_store.py --show                   # Print current validated config JSON
./config_store.py --plan                   # Preview planned launch commands
./config_store.py --plan --force           # Preview commands even if auto_apply_on_launch=false
./config_store.py --self-test              # Run isolated config store unit tests
```
