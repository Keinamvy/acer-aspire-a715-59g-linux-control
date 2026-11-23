# Battery Telemetry and FlexiCharger

`battery.sh` reads battery registers directly from EC RAM and controls **FlexiCharger** (charge start/stop thresholds) directly via `ECMD 0xCB` + live XRAM readback via `ECMD 0xB1`.

---

## Direct EC FlexiCharger Architecture (Zero WMI, Zero Stale Cache)

- **Write (`ECMD 0xCB`):**
  - `0xF9 = stop%` (e.g., `80` or `100`)
  - `0xFA = start%` (e.g., `60` or `80`)
  - `0xFB = enable flag` (`0x02` = enable limit, `0x00` = disable / full 100% charge)
  - `0xF8 = 0xCB`
- **Live Hardware Readback (`ECMD 0xB1` on XRAM `0x06D0..0x06D2`):**
  - `XRAM[0x06D0]` (mirrored at `0x0DD0`): **bit 1 (`0x02`)** = FlexiCharger enabled (`0x18` disabled; `0x1A`/`0x1B` enabled, with bit 0 acting as the active charge-inhibit latch).
  - `XRAM[0x06D1]` (mirrored at `0x0DD1`): active **Start %** threshold (e.g., `0x3C = 60%`, `0x5F = 95%` when disabled).
  - `XRAM[0x06D2]` (mirrored at `0x0DD2`): active **Stop %** threshold (e.g., `0x50 = 80%`, `0x64 = 100%` when disabled).

> [!NOTE]
> Why `ECMD 0xF1 sub 0x04` is not used: on this EC firmware, `0xF1 sub 0x04` returns non-threshold bytes (`00 47 93 A8 FF 00`) and shared mailbox `0xF8..0xFD` is overwritten by `0xD8`/`0xD7`/`0xB1` polling. Reading `XRAM 0x06D0..0x06D2` via `ECMD 0xB1` provides authoritative live hardware state.

---

## CLI Usage

```bash
./battery.sh status             # Telemetry (BAT0 %, AC, cycles) + live XRAM 0x06D0..0x06D2 status
./battery.sh enable             # Enable 60%–80% FlexiCharger limit
./battery.sh limit 70 85        # Custom thresholds: start at 70%, stop at 85% (20 <= start < stop <= 100)
./battery.sh full               # Disable limit (allow 100% full charge; alias: disable)
./battery.sh --dry-run enable   # Preview EC 0xCB mailbox writes
./battery.sh --ec-file /path/to/ec0_io.bin status
```
