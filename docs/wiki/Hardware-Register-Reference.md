# Hardware Register Reference (`Board RTH` / BIOS `V1.16`)

Verified against `DSDT.dsl` (`EC81`, `RAM`, `RAM2`, `RAM3`, `ECMD`, `OGNS`, `APPM`) and live EC/XRAM hardware probes on **Acer Aspire A715-59G**.

---

## EC RAM Registers (`/sys/kernel/debug/ec/ec0/io`)

| Offset | Symbol | Purpose |
|---|---|---|
| `0x03` | `LSTE` / `LWKE` | Bit 2 (`0x04`): Lid wake enable |
| `0x07` | `TMP_` | CPU temperature (`°C`) |
| `0x08` | `AC1_` | Secondary thermal sensor (`°C`) |
| `0x0A` | `DPT1` | CPU VR temperature (`°C`) |
| `0x0B` | `DPT2` | dGPU board temperature (`°C`) |
| `0x10` | `BPC0` | Battery remaining capacity (`%`) |
| `0x38..0x39` | `BPV0` | Battery voltage (LE16) |
| `0x94..0x95` | `BPR0` | Battery current/rate |
| `0xCE` / `0xCF` | `DUT1` / `DUT2` | CPU / GPU fan duty raw (`0..255`) |
| `0xD0..0xD1` / `0xD2..0xD3` | `RPM1` / `RPM2` | CPU / GPU fan tach (BE16; `RPM = 2156250 / raw`) |
| `0xD9` | `AIRP` | EC thermal/profile state flags (`0x88` Quiet, `0x8C` Power Saving, `0x80` Entertainment, `0x84` Performance) |
| `0xDB` | — | Bit 0: Webcam status; Bit 2: Trackpad state |
| `0xDC` | — | Fan policy flag (`0x1A` Silent/Eco vs `0x03` Entertainment/Performance) |
| `0xE2` | `ECKS` | Bit 3: Fn/Win Swap; Bit 4: Win-Key Lock |
| `0xE7` | `GFOF` | Fan speed offset (`0..255`, scaled from `0..100%`) |
| `0xEB` | `APRD` | Bit 6 (`0x40`): Required before `0xD8` power profile commit |
| `0xEC` | `B2E9` | Bit 5: Fn-Lock |
| `0xF0..0xF4` | `PL1T`/`PL2T`/`TAUT` | EC hardware PECI PL1/PL2 telemetry (read-only) |
| `0xF8..0xFD` | `FCMD`, `FDAT`, `FBUF`, `FBF1..FBF3` | `ECMD` shared mailbox (`0xF8` written last) |

---

## EC Mailbox Commands (`0xF8 = FCMD`)

| `0xF8` | Name | Inputs (`0xF9..0xFD`) | Function |
|---|---|---|---|
| `0xB1` | XRAM Read | `0xF9=hi, 0xFA=lo, 0xFB=len` | Reads byte into `0xF9` (`len=1`) |
| `0xB2` | XRAM Write | `0xF9=hi, 0xFA=lo, 0xFB=len, 0xFC=val` | Writes fan curve node byte to XRAM |
| `0xB8` | Peripheral Toggle | `0xFA=0xA1` (cam) / `0xA5` (touchpad) | Hardware power toggle |
| `0xC0` | Telemetry Query | `0xF9=sub (0..15)` | Live thermal/duty/tach telemetry |
| `0xCA` | `KBType=6` Lighting | `0xF9=0x03` (RGB), `0x06` (brightness), `0x0F` (breath) | Single-zone `RGB15Color` control |
| `0xCB` | FlexiCharger Write | `0xF9=stop%, 0xFA=start%, 0xFB=0x02\|0x00` | Sets battery charge thresholds |
| `0xD4` | RGB Timeout | `0xFA..0xFB` | Keyboard backlight idle timeout |
| `0xD7` | Fan Mode | `0xF9=0x00` (read) or mode byte | `0x02` Auto, `0x04` Custom, `0x08` Perf-Auto, `0x10` Max, `0x80` Silent-Auto |
| `0xD8` | Power Profile | `0xF9=0x01, 0xFA=0x00..0x03` | `0x00` Ent, `0x01` Perf, `0x02` Quiet, `0x03` Power Saving |
| `0xD9` | Fan Reset | *(none)* | Resets EC fan curve to BIOS defaults |

---

## XRAM Addresses (`ECMD 0xB1` / `0xB2`)

| XRAM Range | Description |
|---|---|
| `0x038B` | `KBTY` / `KBTP` (`0x06` = Single-zone `RGB15Color`) |
| `0x038C..0x0393` | CPU Fan 1 (`F1`) 4-node curve (`T1..T4`, `D1..D4`) |
| `0x039A..0x03A1` | GPU Fan 2 (`F2`) 4-node curve (`T1..T4`, `D1..D4`) |
| `0x0500`, `0x05DF`, `0x067D` | Active EC fan policy (`0x0500`: `0x00` Quiet/Eco vs `0x80` Ent/Perf) & `0xD7` mirrors |
| `0x06D0..0x06D2` *(mirror `0x0DD0..0x0DD2`)* | Live FlexiCharger state: `flag` (`bit 1 = enabled`), `start%`, `stop%` |
