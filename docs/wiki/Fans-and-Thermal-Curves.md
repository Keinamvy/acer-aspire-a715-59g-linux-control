# Fans and Thermal Curves

`fan.sh` and Tab 2 of `gui.py` provide live thermal monitoring, stock fan mode selection, fan speed offset (`0xE7`), and dual 4-node CPU/GPU fan curve editing (`XRAM 0x038C..0x03A7`).

---

## Stock Fan Modes (`ECMD 0xD7`)

The UI exposes the **3 stock modes** plus **Reset Factory** (`ECMD 0xD9`):

| UI Button | EC `0xD7` Byte | Behavior |
|---|---|---|
| **Auto** | `0x02` (`Auto`), `0x08` (`Perf-Auto`), `0x80` (`Silent-Auto`) | EC automatic fan control. EC auto-selects `0x08` in Performance and `0x80` in Quiet; both highlight **Auto** in the UI. |
| **Max / Full Speed** | `0x10` | Full-speed cooling override (100%). |
| **Custom Curve** | `0x04` | Activates the user-defined 4-node CPU & GPU curves in XRAM `0x038C..0x03A7`. |
| **Reset Factory** | `ECMD 0xD9` | Resets EC fan tables to BIOS defaults. |

Additional CLI aliases retained in `fan.sh` for completeness: `perf-auto` / `mode2` (`0x08`), `maxq` (`0x01`), `silent` / `slow` / `noiseless` (`0x40`), `mode7` (`0x20`).

---

## Silent (`Quiet`) & Eco (`Power Saving`) Read-Only Fan Curves

Live EC XRAM probes (`0x0000..0x1FFF`) across all 4 power profiles established that:
- `RAM2`/`RAM3` (`0x0300..0x03CF`, with `FANB` `0x0300..0x036F` zeroed) is byte-identical across all profiles; `0x038C..0x03A7` holds the active 4-node F1/F2 XRAM curve while internal per-profile Silent/Eco policy resides in EC ROM/Flash via mode/policy registers:
  - **`Quiet` (`0x02`):** `0xD7 = 0x80` (`Silent-Auto`, mirrored at `XRAM[0x05DF/0x067D]`), `EC[0xD9] = 0x88`, `EC[0xDC] = 0x1A`, `XRAM[0x0500] = 0x00`.
  - **`Power Saving` (`0x03`):** `0xD7 = 0x02` (`Auto Eco Policy`), `EC[0xD9] = 0x8C`, `EC[0xDC] = 0x1A`, `XRAM[0x0500] = 0x00`.
- When **`Quiet`** or **`Power Saving`** is active:
  - `gui.py` Tab 2 **keeps the SVG curve plot and 4-node values visible (read-only)** so you can inspect the active EC auto curve (`0x038C`/`0x039A`), while locking Fan Mode buttons, `Fan Offset (0xE7)`, curve spinboxes, and graph dragging (`set_sensitive(False)`).
  - `tray.py` disables the **Fan Mode** submenu and displays `Locked: EC Silent-Auto (0x80 • Read-Only)` or `Locked: EC Eco-Auto (0x02/0x1A • Read-Only)`.
  - Switch to **`Entertainment`** or **`Performance`** to unlock manual Fan Modes and Custom Curve editing.

---

## Dual 4-Node Custom Fan Curve (`0xB1` / `0xB2`)

- **XRAM Layout (`RAM3`, base `0xFE0B0300`):**
  - **CPU Fan 1 (`F1`):** `0x038C..0x0393` (`T1..T4` `°C`, `D1..D4` raw `0..255`), default `(45°C, 28%) → (60°C, 50%) → (80°C, 66%) → (100°C, 100%)`
  - **GPU Fan 2 (`F2`):** `0x039A..0x03A1`, default `(45°C, 28%) → (60°C, 58%) → (80°C, 66%) → (99°C, 100%)`
- Endpoints (`Node 1` and `Node 4`) are read-only firmware anchors; the middle two nodes (`Node 2` and `Node 3`) are editable per fan via `ECMD 0xB2`.
- In `gui.py`, unapplied node drags/edits set `_curve_dirty` so 2-second auto-polling never overwrites your in-progress edits before clicking **Apply Custom Curve**.

---

## CLI Usage & Diagnostics

```bash
./fan.sh status                            # Temps, RPMs, duty %, offset, fan mode & F1/F2 curves
./fan.sh auto                              # auto | max | custom | reset
./fan.sh custom 55 58 75 78                # Apply middle nodes (T2=55C 58%, T3=75C 78%) to both CPU & GPU
./fan.sh custom 55 58 75 78 62 60 82 85    # Independent CPU (4 args) & GPU (4 args) curves
./fan.sh offset 20                         # +20% speed offset via EC[0xE7] (0..100%)
./fan.sh --dump-xram                       # Read-only dump: EC RAM 0x00..0xFF, XRAM 0x0300..0x03CF, ECMD 0xC0
./fan.sh --scan-xram 0300 00d0             # Read-only XRAM hex scan via ECMD 0xB1
```
