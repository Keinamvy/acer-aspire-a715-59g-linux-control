# Power Profiles and dGPU Runtime PM

`power_profile.sh` manages the EC thermal profile (`EC[0xEB]` `APRD=1`, `ECMD 0xD8`), Intel CPU RAPL (`intel-rapl:0` / `intel-rapl-mmio:0`), OS `powerprofilesctl`, and NVIDIA RTX 3050 power limits / PCI Runtime PM.

---

## OEM Profile Matrix

Verified against BIOS `V1.16` `DSDT.dsl` (`APPM` / `OGNS`):

| Profile | EC `0xD8` | OEM PL1 / PL2 (`intel-rapl`) | EC Fan Policy | dGPU Target (`0000:01:00.0`) |
|---|---|---|---|---|
| **Quiet** (`quiet`) | `0x02` | `15W` / `30W` | `Silent-Auto` (`0xD7=0x80`, `EC[0xDC]=0x1A`) | Active (`power/control=on`), `25W` (`nvidia-smi -pl 25`) |
| **Power Saving** (`powersaving`) | `0x03` | `15W` / `30W` | `Auto Eco` (`0xD7=0x02`, `EC[0xDC]=0x1A`) | RTD3 `D3cold` (`nvidia-smi -pm 0` + `power/control=auto` on `.0`/`.1`) |
| **Entertainment** (`entertainment`) | `0x00` | `45W` / `115W` | `Auto` (`0xD7=0x02`, `EC[0xDC]=0x03`) | Active (`power/control=on`), `60W` (`nvidia-smi -pl 60`) |
| **Performance** (`performance`) | `0x01` | `90W` / `115W` | `Perf-Auto` (`0xD7=0x08`, `EC[0xDC]=0x03`) | Active (`power/control=on`), `75W` (`nvidia-smi -pl 75`) |

> [!NOTE]
> `Turbo` (`0x04` / `0x42`) is **not** implemented on Board `RTH` and is excluded from the suite.

---

## How `Power Saving` vs `Quiet` Differs

Both **Quiet** and **Power Saving** apply the OEM `15W / 30W` CPU PL1/PL2 limits and lock manual fan curve editing in Tab 2 / Tray:
- **Quiet** keeps the NVIDIA dGPU powered (`power/control=on`) and caps it at **`25W`** for low-noise light GPU workloads.
- **Power Saving** releases persistence mode (`nvidia-smi -pm 0`) and switches both `0000:01:00.0` (GPU) and `0000:01:00.1` (GPU Audio) `power/control` to `auto` so the GPU can enter **RTD3 `D3cold`**.
- **Polling Protection:** Both `power_profile.sh status` and `fan.sh status` check `/sys/bus/pci/devices/0000:01:00.0/power/runtime_status` **before** running `nvidia-smi`. When the GPU is `suspended` (or absent), `nvidia-smi` is skipped so 2-second GUI/tray polling never wakes the dGPU from `D3cold`.
- If a display server (e.g., Xwayland/COSMIC running in dedicated `nvidia` graphics mode) pins `/dev/nvidia0`, `power_profile.sh status` reports `RTD3 Auto / D3cold Pending (Power Saving mode; display active)` until hybrid/integrated mode allows `D3cold`.

---

## CLI Usage & Flags

```bash
./power_profile.sh status
./power_profile.sh quiet                   # aliases: silent
./power_profile.sh powersaving             # aliases: power-saving, saving, saver, eco
./power_profile.sh entertainment           # aliases: balanced, ent
./power_profile.sh performance             # aliases: perf

# Optional OS RAPL software ceiling overrides & flags
./power_profile.sh --pl1 40 --pl2 85 performance
./power_profile.sh --no-rapl performance   # Skip intel-rapl writes (EC 0xD8 only)
./power_profile.sh --no-os quiet           # Skip powerprofilesctl sync
./power_profile.sh --dry-run powersaving   # Preview EC, RAPL & PCI commands
./power_profile.sh --ec-file /path/to/ec0_io.bin status
```

Live EC PECI registers `EC[0xF0..0xF4]` (`PL1T`, `PL2T`, `TAUT`) are displayed in `status` as read-only hardware telemetry.
