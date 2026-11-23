# Keyboard Lighting (`KBType=6` Single-Zone `RGB15Color`)

On the Acer Aspire A715-59G (`KBTY / KBTP == 6` in XRAM `0x038B`), the keyboard backlight is **single-zone `RGB15Color`**. Multi-zone (`0xC4`) commands and 3-zone effects (`Cycle`, `Wave`, `Flash`, `Random`, `Dance`, `Tempo`) are not supported by `KBType=6` hardware and are omitted from the suite.

---

## Backlight Brightness (`brightness.sh`)

- **EC Command:** `ECMD 0xCA`, subcommand `0x06` (`0xF9 = 0x06`, `0xFA = level 0..255`).
- Hardware brightness is **write-only** on `KBType=6`; `brightness.sh` caches the last applied level in `${XDG_CACHE_HOME:-~/.cache}/acer-a715-59g/brightness_state` and `config.json` (`keyboard.brightness`).

```bash
./brightness.sh status          # Show cached brightness & mailbox bytes
./brightness.sh off             # 0 (off)
./brightness.sh low             # 47 (or p1)
./brightness.sh med             # 95 (or p2)
./brightness.sh high            # 143 (or p3)
./brightness.sh max             # 191 (or p4; Control Center 3.0 default max)
./brightness.sh full            # 255 (raw EC maximum)
./brightness.sh 128             # Any integer 0..255
```

---

## Single-Zone RGB, Breath & Idle Timeout (`rgb.sh`)

- **Static Color (`ECMD 0xCA` sub `0x03`):** writes `0xF9=0x03`, `0xFA=R`, `0xFB=G`, `0xFC=B`, `0xFD=0x00`.
  - Pass `--cc-scale` to apply Control Center `FnKey` / `CC40` half-range scaling (`// 2`).
- **15-Color Breathing (`ECMD 0xCA` sub `0x0F`):** activates firmware 15-color breathing animation.
- **Idle Timeout (`ECMD 0xD4`):** automatically turns off keyboard backlight after `5..65535` seconds of inactivity (or `off` / `0` to disable timeout).

```bash
./rgb.sh status                 # Show cached RGB/effect/timeout + mailbox bytes
./rgb.sh 255 0 128              # Static RGB (0..255)
./rgb.sh --cc-scale cyan        # Preset with half-range scaling
./rgb.sh off                    # Turn RGB off
./rgb.sh --effect breath        # 15-color breathing effect
./rgb.sh --timeout 30           # Auto-off after 30s idle
./rgb.sh --timeout off          # Keep backlight on indefinitely
```

Available color presets: `blue`, `green`, `red`, `cyan`, `skyblue`, `yellow`, `orange`, `purple`, `pink`, `white`, `off`.
