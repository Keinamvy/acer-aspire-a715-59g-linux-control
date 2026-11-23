#!/bin/bash
# Fan Control & Thermal Telemetry for Acer Aspire A715-59G (Direct ec_sys)
#
# Verified from Aspire A715-59G BIOS V1.16 DSDT & Clevo FanSpeedSetting (FAN.cs / Interface_Fan.cs):
#   Static EC RAM (0xFE0B0100):
#     0x07 (TMP_) = CPU Temp (C)       0x08 (AC1_) = Sensor 1 (C)
#     0x0A (DPT1) = CPU VR Temp (C)    0x0B (DPT2) = dGPU Board Temp (C)
#     0xCE (DUT1) = CPU Fan Duty 0..255  0xCF (DUT2) = GPU Fan Duty 0..255
#     0xD0..0xD1 (RPM1 BE16), 0xD2..0xD3 (RPM2 BE16): RPM = 2156250 / raw_tach (0 if raw == 0)
#     0xE7 (GFOF) = Fan Speed Offset raw 0..255 (_DSM 121 Sub 14 writes raw byte; scaled round(pct*255/100))
#   EC Mailbox (0xF8=FCMD, 0xF9=FDAT, 0xFA=FBUF):
#     0xD7 Read  : 0xF9=0x00, 0xF8=0xD7 -> read 0xFA (active bitmask)
#     0xD7 Write : 0xF9=<bit>, 0xF8=0xD7 (_DSM 121 Sub 1 cases 0..8; 3 & 4 are no-ops):
#       0x01 = MaxQ (case 5)           0x02 = Auto (case 0)
#       0x04 = Custom (case 6, FanCount>=2)
#       0x08 = Auto (Performance Profile) / mode2 (case 2; observed live 0xD7(0x00) in Performance mode)
#       0x10 = Max / Full (case 1)     0x20 = Mode7 / x7 (case 7)
#       0x40 = Silent / Noiseless / Slow (case 8)
#       0x80 = Silent-Auto (Quiet Profile; EC-managed read-only 0xD7 state)
#     0xD9 Reset : 0xF8=0xD9 (_DSM 121 Sub 34 -> reset EC fan table to BIOS default)
#     0xB1 Read XRAM : 0xF9=addr_hi, 0xFA=addr_lo, 0xFB=len(1..4), 0xFC=0, 0xFD=0, 0xF8=0xB1 -> read 0xF9..
#     0xB2 Write XRAM: 0xF9=addr_hi, 0xFA=addr_lo, 0xFB=0x01, 0xFC=byte0, 0xFD=byte1, 0xF8=0xB2
#   Custom Fan Curve XRAM Tables (4-node piecewise linear; temps C, raw duty 0..255, BE16 slopes):
#     CPU F1 (0x038C..0x0399): T1..T4 (0x8C..0x8F), D1..D4 (0x90..0x93), R12/R23/R34 BE16 (0x94..0x99)
#     GPU F2 (0x039A..0x03A7): T1..T4 (0x9A..0x9D), D1..D4 (0x9E..0xA1), R12/R23/R34 BE16 (0xA2..0xA7)

set -euo pipefail

EC_IO="/sys/kernel/debug/ec/ec0/io"
CACHE_DIR="${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/acer-a715-59g"
CURVE_STATE_FILE="$CACHE_DIR/fan_curve_state"
DEFAULT_CURVE=(40 50 60 60 80 70 100 100)
EC_FILE=""
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: fan.sh [options] <command> [args...]

Commands:
  status                    Read EC temps, fan duties, fan RPMs, offset, fan mode & curves
  auto                      Automatic fan curve               (FCMD 0xD7, FDAT=0x02)
  mode2                     Auto fan mode 2 (Perf Profile)    (FCMD 0xD7, FDAT=0x08)
  max | full                Maximum / full-speed fan        (FCMD 0xD7, FDAT=0x10)
  maxq                      Max-Q acoustic fan mode         (FCMD 0xD7, FDAT=0x01)
  custom | curve [args...]  Set custom mode (0xD7=0x04), or program 4-node CPU/GPU curves
                            Args: CPU_T2 CPU_D2 CPU_T3 CPU_D3 [GPU_T2 GPU_D2 GPU_T3 GPU_D3]
  silent | slow | noiseless Slow / noiseless fan mode       (FCMD 0xD7, FDAT=0x40)
  mode7                     BIOS mode 7 (DSDT arg=7)        (FCMD 0xD7, FDAT=0x20)
  offset <0..100>           Set fan speed offset at 0xE7    (raw = round(pct*255/100))
  reset                     Reset EC fan table to default   (FCMD 0xD9)
  --dump-xram               Read-only dump of EC RAM 0x00..0xFF, XRAM 0x0300..0x03CF, and ECMD 0xC0 (0..15)
  --scan-xram <start> <len> Read-only XRAM hex scan via ECMD 0xB1 (hex start & byte length)

Options:
  --ec-file <path>          Read static EC snapshot file (for status testing only)
  --dry-run                 Print exact EC writes without touching hardware
  -h, --help                Show this help message
EOF
}

check_ec_sys() {
    if [[ ! -e /sys/module/ec_sys ]]; then
        echo "Error: ec_sys module is not loaded. Run: sudo modprobe ec_sys write_support=1" >&2
        exit 1
    fi
    if [[ "$(cat /sys/module/ec_sys/parameters/write_support 2>/dev/null || echo N)" != "Y" ]]; then
        echo "Error: ec_sys write_support is not enabled." >&2
        exit 1
    fi
}

ec_read_byte() {
    local off="$1"
    local src="${EC_FILE:-$EC_IO}"
    local val
    if [[ -n "$EC_FILE" ]] || (( EUID == 0 )); then
        val=$(dd if="$src" bs=1 skip="$((off))" count=1 status=none | od -An -tu1 | tr -d ' \t\n')
    else
        val=$(sudo dd if="$src" bs=1 skip="$((off))" count=1 status=none | od -An -tu1 | tr -d ' \t\n')
    fi
    if [[ ! "$val" =~ ^[0-9]+$ ]]; then
        echo "Error: failed to read EC byte at offset $(printf '0x%02X' "$((off))") from $src." >&2
        return 1
    fi
    printf '%s' "$val"
}

ec_write_byte() {
    local off="$1" val="$2"
    if (( EUID == 0 )); then
        printf "\\x$(printf '%02x' "$((val & 0xFF))")" | dd of="$EC_IO" bs=1 seek="$((off))" conv=notrunc status=none
    else
        printf "\\x$(printf '%02x' "$((val & 0xFF))")" | sudo dd of="$EC_IO" bs=1 seek="$((off))" conv=notrunc status=none
    fi
}

ec_wait_cmd_done() {
    local strict="${1:-0}"
    local i val
    for ((i = 0; i < 50; i++)); do
        if val=$(ec_read_byte 0xF8 2>/dev/null) && [[ "$val" =~ ^[0-9]+$ ]] && (( 10#$val == 0 )); then
            return 0
        fi
        sleep 0.02
    done
    if (( strict )); then
        return 1
    fi
    echo "Warning: EC FCMD (0xF8) did not clear to 0x00 within timeout." >&2
    return 0
}

ec_read_xram_bytes() {
    local addr_hi="$1" addr_lo="$2" len="$3"
    if [[ ! "$len" =~ ^[0-9]+$ ]] || (( 10#$len < 1 || 10#$len > 4 )); then
        echo "Error: ec_read_xram_bytes len must be 1..4 (got '$len')." >&2
        return 1
    fi
    len=$((10#$len))

    # Zero 0xFC/0xFD and trigger 0xB1 LAST via 0xF8
    ec_write_byte 0xF9 "$((addr_hi))" || return 1
    ec_write_byte 0xFA "$((addr_lo))" || return 1
    ec_write_byte 0xFB "$len" || return 1
    ec_write_byte 0xFC 0x00 || return 1
    ec_write_byte 0xFD 0x00 || return 1
    ec_write_byte 0xF8 0xB1 || return 1

    if ! ec_wait_cmd_done 1; then
        return 1
    fi

    local -a out=()
    local i val
    for ((i = 0; i < len; i++)); do
        val=$(ec_read_byte $((0xF9 + i)) 2>/dev/null) || return 1
        out+=("$((10#$val))")
    done
    printf '%s\n' "${out[*]}"
}

ec_write_xram_word() {
    local addr_hi="$1" addr_lo="$2" byte0="$3" byte1="$4"

    # Populate parameters before triggering 0xB2 LAST via 0xF8
    ec_write_byte 0xF9 "$((addr_hi))"
    ec_write_byte 0xFA "$((addr_lo))"
    ec_write_byte 0xFB 0x01
    ec_write_byte 0xFC "$((byte0))"
    ec_write_byte 0xFD "$((byte1))"
    ec_write_byte 0xF8 0xB2

    if ! ec_wait_cmd_done 1; then
        printf "Error: EC XRAM write (0xB2 at 0x%02X%02X) timed out.\n" \
            "$((addr_hi & 0xFF))" "$((addr_lo & 0xFF))" >&2
        return 1
    fi
}

validate_curve_nodes() {
    local -a vals=("$@")
    if (( ${#vals[@]} != 8 )); then
        return 1
    fi
    local v
    for v in "${vals[@]}"; do
        [[ "$v" =~ ^[0-9]+$ ]] || return 1
    done
    local t1=$((10#${vals[0]})) d1=$((10#${vals[1]}))
    local t2=$((10#${vals[2]})) d2=$((10#${vals[3]}))
    local t3=$((10#${vals[4]})) d3=$((10#${vals[5]}))
    local t4=$((10#${vals[6]})) d4=$((10#${vals[7]}))

    (( t1 >= 30 && t1 < t2 && t2 < t3 && t3 < t4 && t4 <= 100 )) || return 1
    (( d1 >= 0 && d1 <= d2 && d2 <= d3 && d3 <= d4 && d4 > 0 && d4 <= 100 )) || return 1
    return 0
}

raw_duty_to_pct() {
    local raw=$((10#$1))
    printf '%d' $(( (raw * 100 + 127) / 255 ))
}

pct_to_raw_duty() {
    local pct=$((10#$1))
    printf '%d' $(( (pct * 255 + 50) / 100 ))
}

format_curve_line() {
    local label="$1" addr="$2"
    local t1=$((10#$3)) d1=$((10#$4)) t2=$((10#$5)) d2=$((10#$6))
    local t3=$((10#$7)) d3=$((10#$8)) t4=$((10#$9)) d4=$((10#${10}))
    printf "  %s (%s): (%dC, %d%%) -> (%dC, %d%%) -> (%dC, %d%%) -> (%dC, %d%%)\n" \
        "$label" "$addr" "$t1" "$d1" "$t2" "$d2" "$t3" "$d3" "$t4" "$d4"
}

load_cached_curve() {
    local which="$1"
    [[ -r "$CURVE_STATE_FILE" ]] || return 1

    local line key val raw_spec="" fallback_spec=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line//$'\r'/}"
        [[ -n "$line" && ! "$line" =~ ^[[:space:]]*# ]] || continue
        if [[ "$line" == *=* ]]; then
            key="${line%%=*}"
            val="${line#*=}"
            key="${key//[[:space:]]/}"
            if [[ "$key" == "$which" ]]; then
                raw_spec="$val"
                break
            fi
        elif [[ -z "$fallback_spec" ]]; then
            fallback_spec="$line"
        fi
    done < "$CURVE_STATE_FILE"

    local spec="${raw_spec:-$fallback_spec}"
    [[ -n "$spec" ]] || return 1
    spec="${spec//,/ }"
    local -a nums=()
    read -r -a nums <<< "$spec"
    validate_curve_nodes "${nums[@]}" || return 1
    printf '%s\n' "$((10#${nums[0]})) $((10#${nums[1]})) $((10#${nums[2]})) $((10#${nums[3]})) $((10#${nums[4]})) $((10#${nums[5]})) $((10#${nums[6]})) $((10#${nums[7]}))"
}

save_curve_state() {
    local cpu_csv="$1" gpu_csv="$2"
    mkdir -p "$CACHE_DIR"
    printf 'cpu=%s\ngpu=%s\n' "$cpu_csv" "$gpu_csv" > "$CURVE_STATE_FILE"
}

read_live_curve_xram() {
    local temp_lo="$1" duty_lo="$2"
    local t_raw d_raw
    t_raw=$(ec_read_xram_bytes 0x03 "$temp_lo" 4 2>/dev/null) || return 1
    d_raw=$(ec_read_xram_bytes 0x03 "$duty_lo" 4 2>/dev/null) || return 1

    local -a ts=() ds=()
    read -r -a ts <<< "$t_raw"
    read -r -a ds <<< "$d_raw"
    (( ${#ts[@]} == 4 && ${#ds[@]} == 4 )) || return 1

    local d1 d2 d3 d4
    d1=$(raw_duty_to_pct "${ds[0]}")
    d2=$(raw_duty_to_pct "${ds[1]}")
    d3=$(raw_duty_to_pct "${ds[2]}")
    d4=$(raw_duty_to_pct "${ds[3]}")

    local -a nodes=("${ts[0]}" "$d1" "${ts[1]}" "$d2" "${ts[2]}" "$d3" "${ts[3]}" "$d4")
    validate_curve_nodes "${nodes[@]}" || return 1
    printf '%s\n' "${nodes[*]}"
}

resolve_curve_for_status() {
    local which="$1" temp_lo="$2" duty_lo="$3"
    local curve=""
    if [[ -z "$EC_FILE" ]] && (( DRY_RUN == 0 )); then
        if curve=$(read_live_curve_xram "$temp_lo" "$duty_lo"); then
            printf '%s\n' "$curve"
            return 0
        fi
    fi
    if curve=$(load_cached_curve "$which"); then
        printf '%s\n' "$curve"
        return 0
    fi
    printf '%s\n' "${DEFAULT_CURVE[*]}"
}

decode_fan_mode() {
    local v="${1:-}"
    local ec_d9="${2:-}"
    local ec_dc="${3:-}"
    if [[ ! "$v" =~ ^[0-9]+$ ]]; then
        printf "unknown (invalid: '%s')\n" "$v"
        return 0
    fi
    v=$((10#$v))
    case "$v" in
        0)   echo "default / unset (0x00)" ;;
        1)   echo "maxq (0x01)" ;;
        2)
            if { [[ "$ec_dc" =~ ^[0-9]+$ ]] && (( 10#$ec_dc == 0x1A )); } || \
               { [[ "$ec_d9" =~ ^[0-9]+$ ]] && (( (10#$ec_d9 & 0x0C) == 0x0C )); }; then
                echo "Auto (0x02, Power Saving Eco Policy)"
            else
                echo "Auto (0x02)"
            fi
            ;;
        4)   echo "custom (0x04)" ;;
        8)   echo "Auto (0x08, Performance Profile)" ;;
        16)  echo "max / full (0x10)" ;;
        32)  echo "mode7 (0x20)" ;;
        64)  echo "silent / noiseless (0x40)" ;;
        128) echo "Silent-Auto (0x80, Quiet Profile)" ;;
        *)
            local -a flags=()
            if (( v & 0x01 )); then flags+=("maxq"); fi
            if (( v & 0x02 )); then flags+=("auto"); fi
            if (( v & 0x04 )); then flags+=("custom"); fi
            if (( v & 0x08 )); then flags+=("auto(perf)"); fi
            if (( v & 0x10 )); then flags+=("max"); fi
            if (( v & 0x20 )); then flags+=("mode7"); fi
            if (( v & 0x40 )); then flags+=("silent"); fi
            if (( v & 0x80 )); then flags+=("silent-auto"); fi
            local joined
            joined=$(IFS='|'; echo "${flags[*]}")
            printf "flags: %s (0x%02X)\n" "${joined:-unknown}" "$v"
            ;;
    esac
}

show_status() {
    local tmp ac1 dpt1 dpt2 dut1 dut2 r1_hi r1_lo r2_hi r2_lo gfof
    local src="${EC_FILE:-$EC_IO}"
    local -a cpu_curve=() gpu_curve=()

    if [[ -z "$EC_FILE" ]]; then
        if (( DRY_RUN )); then
            echo "[dry-run] ec[0xF9]=0x00, ec[0xF8]=0xD7 -> read ec[0xFA], static EC regs (0x07..0xE7), and XRAM F1/F2 (0xB1: 0x038C..0x0393, 0x039A..0x03A1)"
            read -r -a cpu_curve <<< "$(resolve_curve_for_status cpu 0x8C 0x90)"
            read -r -a gpu_curve <<< "$(resolve_curve_for_status gpu 0x9A 0x9E)"
            format_curve_line "CPU Curve" "0x038C" "${cpu_curve[@]}"
            format_curve_line "GPU Curve" "0x039A" "${gpu_curve[@]}"
            return 0
        fi
        check_ec_sys
    elif [[ ! -r "$EC_FILE" ]]; then
        echo "Error: EC snapshot file not readable: $EC_FILE" >&2
        exit 1
    fi

    local raw_dump
    if [[ -n "$EC_FILE" ]]; then
        raw_dump=$(od -An -v -tu1 -w1 -N256 "$src" | tr -d ' \t')
    elif (( EUID == 0 )); then
        raw_dump=$(dd if="$src" bs=1 count=256 status=none | od -An -v -tu1 -w1 | tr -d ' \t')
    else
        raw_dump=$(sudo dd if="$src" bs=1 count=256 status=none | od -An -v -tu1 -w1 | tr -d ' \t')
    fi

    local -a ec=()
    if [[ -n "$raw_dump" ]]; then
        mapfile -t ec <<< "$raw_dump"
    fi
    if (( ${#ec[@]} < 256 )); then
        echo "Error: expected at least 256 bytes from $src (got ${#ec[@]})." >&2
        exit 1
    fi

    tmp=${ec[0x07]}
    ac1=${ec[0x08]}
    dpt1=${ec[0x0A]}
    dpt2=${ec[0x0B]}
    dut1=${ec[0xCE]}
    dut2=${ec[0xCF]}
    r1_hi=${ec[0xD0]}; r1_lo=${ec[0xD1]}
    r2_hi=${ec[0xD2]}; r2_lo=${ec[0xD3]}
    gfof=${ec[0xE7]}

    local raw1=$(( (r1_hi << 8) | r1_lo ))
    local raw2=$(( (r2_hi << 8) | r2_lo ))
    local rpm1=0 rpm2=0
    if (( raw1 > 0 )); then
        rpm1=$(( (2156250 + raw1 / 2) / raw1 ))
    fi
    if (( raw2 > 0 )); then
        rpm2=$(( (2156250 + raw2 / 2) / raw2 ))
    fi
    local pct1=$(( (dut1 * 100 + 127) / 255 ))
    local pct2=$(( (dut2 * 100 + 127) / 255 ))
    local off_pct=$(( (gfof * 100 + 127) / 255 ))

    echo "=== EC Thermal & Fan Telemetry ($src) ==="
    printf "  CPU Temp  (0x07) : %d C  (Sensor 0x08: %d C)\n" "$tmp" "$ac1"
    printf "  CPU VR    (0x0A) : %d C  (dGPU 0x0B  : %d C)\n" "$dpt1" "$dpt2"
    printf "  CPU Fan   (0xCE) : %4d RPM  (duty=%3d/255, %3d%%, raw_tach=%d)\n" "$rpm1" "$dut1" "$pct1" "$raw1"
    printf "  GPU Fan   (0xCF) : %4d RPM  (duty=%3d/255, %3d%%, raw_tach=%d)\n" "$rpm2" "$dut2" "$pct2" "$raw2"
    printf "  Fan Offset(0xE7) : %3d/255 (%d%%)\n" "$gfof" "$off_pct"

    if [[ -z "$EC_FILE" ]]; then
        ec_write_byte 0xF9 0x00
        ec_write_byte 0xF8 0xD7
        ec_wait_cmd_done
        local fmode
        fmode=$(ec_read_byte 0xFA)
        echo "  Fan Mode  (0xD7) : $(decode_fan_mode "$fmode" "${ec[0xD9]}" "${ec[0xDC]}")"
    fi

    read -r -a cpu_curve <<< "$(resolve_curve_for_status cpu 0x8C 0x90)"
    read -r -a gpu_curve <<< "$(resolve_curve_for_status gpu 0x9A 0x9E)"
    format_curve_line "CPU Curve" "0x038C" "${cpu_curve[@]}"
    format_curve_line "GPU Curve" "0x039A" "${gpu_curve[@]}"

    local gpu_rt_file="/sys/bus/pci/devices/0000:01:00.0/power/runtime_status"
    local gpu_rt=""
    if [[ -r "$gpu_rt_file" ]]; then
        gpu_rt=$(<"$gpu_rt_file")
    fi
    echo "=== NVIDIA GPU ==="
    if [[ ! -r "$gpu_rt_file" ]]; then
        echo "  NVIDIA dGPU, N/A (D3cold), 0 W"
    elif [[ "$gpu_rt" == "suspended" ]]; then
        echo "  NVIDIA dGPU, Suspended (D3cold), 0 W"
    elif command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=name,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null | sed 's/^/  /' || true
    else
        echo "  NVIDIA dGPU, N/A (nvidia-smi absent), N/A"
    fi
}

POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --ec-file)
            [[ $# -ge 2 ]] || { echo "Error: --ec-file requires a path." >&2; exit 1; }
            EC_FILE="$2"; shift 2 ;;
        --dump-xram)
            POSITIONAL+=("dump-xram"); shift ;;
        --scan-xram)
            [[ $# -ge 3 ]] || { echo "Error: --scan-xram requires <start_hex> <len_hex>." >&2; exit 1; }
            POSITIONAL+=("scan-xram" "$2" "$3"); shift 3 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done

CMD="${POSITIONAL[0]:-status}"
EXTRA_ARGS=()
if (( ${#POSITIONAL[@]} > 1 )); then
    EXTRA_ARGS=("${POSITIONAL[@]:1}")
fi

if [[ -n "$EC_FILE" && "${CMD,,}" != "status" ]]; then
    echo "Error: --ec-file is only supported with the 'status' command." >&2
    exit 1
fi

case "${CMD,,}" in
    offset)
        if (( ${#EXTRA_ARGS[@]} != 1 )); then
            echo "Error: 'offset' requires exactly 1 percentage argument (0..100)." >&2
            usage >&2
            exit 1
        fi
        ;;
    custom|curve)
        if (( ${#EXTRA_ARGS[@]} != 0 && ${#EXTRA_ARGS[@]} != 4 && ${#EXTRA_ARGS[@]} != 8 )); then
            echo "Error: '$CMD' expects 0, 4, or 8 arguments (got ${#EXTRA_ARGS[@]})." >&2
            usage >&2
            exit 1
        fi
        ;;
    scan-xram)
        if (( ${#EXTRA_ARGS[@]} != 2 )); then
            echo "Error: 'scan-xram' expects <start_hex> <len_hex> (got ${#EXTRA_ARGS[@]})." >&2
            usage >&2
            exit 1
        fi
        ;;
    *)
        if (( ${#EXTRA_ARGS[@]} > 0 )); then
            echo "Error: unexpected argument '${EXTRA_ARGS[0]}' for command '$CMD'." >&2
            usage >&2
            exit 1
        fi
        ;;
esac

send_fan_mode() {
    local bit="$1" label="$2"
    if (( DRY_RUN )); then
        printf "[dry-run] ec[0xF9]=0x%02X, ec[0xF8]=0xD7 (%s) -> %s\n" $((bit)) "$label" "$EC_IO"
        return 0
    fi
    check_ec_sys
    ec_write_byte 0xF9 "$((bit))"
    ec_write_byte 0xF8 0xD7
    ec_wait_cmd_done
    echo "Fan mode set to $label."
}

read_base_node() {
    local temp_lo="$1" duty_lo="$2"
    local live=""
    if (( DRY_RUN == 0 )); then
        if live=$(read_live_curve_xram "$temp_lo" "$duty_lo"); then
            local -a arr=()
            read -r -a arr <<< "$live"
            printf '%s\n' "${arr[0]} ${arr[1]}"
            return 0
        fi
    fi
    printf '%s\n' "40 50"
}

validate_middle_nodes() {
    local label="$1"
    local t1=$((10#$2)) d1=$((10#$3))
    local t2=$((10#$4)) d2=$((10#$5))
    local t3=$((10#$6)) d3=$((10#$7))

    if (( t2 <= t1 || t2 >= t3 || t3 >= 100 )); then
        printf "Error: %s temps must satisfy T1 (%dC) < T2 < T3 < 100C (got T2=%d, T3=%d).\n" \
            "$label" "$t1" "$t2" "$t3" >&2
        exit 1
    fi
    if (( d2 <= d1 || d2 >= d3 || d3 > 100 )); then
        printf "Error: %s duties must satisfy D1 (%d%%) < D2 < D3 <= 100%% (got D2=%d, D3=%d).\n" \
            "$label" "$d1" "$d2" "$d3" >&2
        exit 1
    fi
}

program_fan_curve() {
    local label="$1" base_lo=$(($2))
    local t1=$((10#$3)) d1=$((10#$4))
    local t2=$((10#$5)) d2=$((10#$6))
    local t3=$((10#$7)) d3=$((10#$8))
    local t4=100 d4=100

    local d1_raw d2_raw d3_raw d4_raw=255
    d1_raw=$(pct_to_raw_duty "$d1")
    d2_raw=$(pct_to_raw_duty "$d2")
    d3_raw=$(pct_to_raw_duty "$d3")

    local r12=$(( ((d2 - d1) * 408 + (t2 - t1) / 2) / (t2 - t1) ))
    local r23=$(( ((d3 - d2) * 408 + (t3 - t2) / 2) / (t3 - t2) ))
    local r34=$(( ((d4 - d3) * 408 + (t4 - t3) / 2) / (t4 - t3) ))

    local -a words=(
        "$((base_lo + 0))"  "$t1"                  "$t2"
        "$((base_lo + 2))"  "$t3"                  "$t4"
        "$((base_lo + 4))"  "$d1_raw"              "$d2_raw"
        "$((base_lo + 6))"  "$d3_raw"              "$d4_raw"
        "$((base_lo + 8))"  "$(((r12 >> 8) & 0xFF))" "$((r12 & 0xFF))"
        "$((base_lo + 10))" "$(((r23 >> 8) & 0xFF))" "$((r23 & 0xFF))"
        "$((base_lo + 12))" "$(((r34 >> 8) & 0xFF))" "$((r34 & 0xFF))"
    )

    local i off b0 b1
    for ((i = 0; i < ${#words[@]}; i += 3)); do
        off="${words[i]}"
        b0="${words[i + 1]}"
        b1="${words[i + 2]}"
        if (( DRY_RUN )); then
            printf "[dry-run] %s XRAM 0x03%02X: ec[0xF9]=0x03, ec[0xFA]=0x%02X, ec[0xFB]=0x01, ec[0xFC]=0x%02X, ec[0xFD]=0x%02X, ec[0xF8]=0xB2\n" \
                "$label" "$off" "$off" "$b0" "$b1"
        else
            ec_write_xram_word 0x03 "$off" "$b0" "$b1"
        fi
    done
}

cmd_custom() {
    if (( ${#EXTRA_ARGS[@]} == 0 )); then
        send_fan_mode 0x04 "custom"
        return 0
    fi

    local a
    for a in "${EXTRA_ARGS[@]}"; do
        if [[ ! "$a" =~ ^[0-9]+$ ]]; then
            echo "Error: custom curve arguments must be non-negative integers (got '$a')." >&2
            exit 1
        fi
    done

    local c_t2=$((10#${EXTRA_ARGS[0]})) c_d2=$((10#${EXTRA_ARGS[1]}))
    local c_t3=$((10#${EXTRA_ARGS[2]})) c_d3=$((10#${EXTRA_ARGS[3]}))
    local g_t2="$c_t2" g_d2="$c_d2" g_t3="$c_t3" g_d3="$c_d3"
    if (( ${#EXTRA_ARGS[@]} == 8 )); then
        g_t2=$((10#${EXTRA_ARGS[4]}))
        g_d2=$((10#${EXTRA_ARGS[5]}))
        g_t3=$((10#${EXTRA_ARGS[6]}))
        g_d3=$((10#${EXTRA_ARGS[7]}))
    fi

    # Pre-check structural monotonicity before touching EC hardware
    validate_middle_nodes "CPU" 0 0 "$c_t2" "$c_d2" "$c_t3" "$c_d3"
    validate_middle_nodes "GPU" 0 0 "$g_t2" "$g_d2" "$g_t3" "$g_d3"

    if (( DRY_RUN == 0 )); then
        check_ec_sys
    fi

    local -a cpu_base=() gpu_base=()
    read -r -a cpu_base <<< "$(read_base_node 0x8C 0x90)"
    read -r -a gpu_base <<< "$(read_base_node 0x9A 0x9E)"
    local c_t1="${cpu_base[0]}" c_d1="${cpu_base[1]}"
    local g_t1="${gpu_base[0]}" g_d1="${gpu_base[1]}"

    validate_middle_nodes "CPU" "$c_t1" "$c_d1" "$c_t2" "$c_d2" "$c_t3" "$c_d3"
    validate_middle_nodes "GPU" "$g_t1" "$g_d1" "$g_t2" "$g_d2" "$g_t3" "$g_d3"

    program_fan_curve "CPU" 0x8C "$c_t1" "$c_d1" "$c_t2" "$c_d2" "$c_t3" "$c_d3"
    program_fan_curve "GPU" 0x9A "$g_t1" "$g_d1" "$g_t2" "$g_d2" "$g_t3" "$g_d3"

    if (( DRY_RUN )); then
        printf "[dry-run] ec[0xF9]=0x04, ec[0xF8]=0xD7 (custom) -> %s\n" "$EC_IO"
        format_curve_line "CPU Curve" "0x038C" "$c_t1" "$c_d1" "$c_t2" "$c_d2" "$c_t3" "$c_d3" 100 100
        format_curve_line "GPU Curve" "0x039A" "$g_t1" "$g_d1" "$g_t2" "$g_d2" "$g_t3" "$g_d3" 100 100
        return 0
    fi

    ec_write_byte 0xF9 0x04
    ec_write_byte 0xF8 0xD7
    ec_wait_cmd_done
    save_curve_state \
        "${c_t1},${c_d1},${c_t2},${c_d2},${c_t3},${c_d3},100,100" \
        "${g_t1},${g_d1},${g_t2},${g_d2},${g_t3},${g_d3},100,100"
    echo "Custom fan curves applied and activated (0xD7 = 0x04)."
    format_curve_line "CPU Curve" "0x038C" "$c_t1" "$c_d1" "$c_t2" "$c_d2" "$c_t3" "$c_d3" 100 100
    format_curve_line "GPU Curve" "0x039A" "$g_t1" "$g_d1" "$g_t2" "$g_d2" "$g_t3" "$g_d3" 100 100
}

parse_hex_u16() {
    local raw="$1"
    if [[ ! "$raw" =~ ^(0[xX])?([0-9a-fA-F]+)$ ]]; then
        return 1
    fi
    local val=$(( 16#${BASH_REMATCH[2]} ))
    printf '%d' "$val"
}

xram_read_range_bytes() {
    local start=$((10#$1)) len=$((10#$2))
    if (( EUID == 0 )) && command -v python3 >/dev/null 2>&1; then
        python3 - "$EC_IO" "$start" "$len" <<'PY'
import os, sys, time

ec_io, start, length = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
fd = os.open(ec_io, os.O_RDWR)
try:
    def rb(off):
        os.lseek(fd, off, os.SEEK_SET)
        b = os.read(fd, 1)
        if len(b) != 1:
            raise RuntimeError(f"short read at 0x{off:02X}")
        return b[0]

    def wb(off, val):
        os.lseek(fd, off, os.SEEK_SET)
        os.write(fd, bytes([val & 0xFF]))

    def wait_done():
        for _ in range(500):
            if rb(0xF8) == 0:
                return
            time.sleep(0.002)
        raise TimeoutError("EC FCMD 0xF8 timeout during 0xB1")

    out = []
    addr = start
    rem = length
    while rem > 0:
        n = 4 if rem >= 4 else rem
        wb(0xF9, (addr >> 8) & 0xFF)
        wb(0xFA, addr & 0xFF)
        wb(0xFB, n)
        wb(0xFC, 0x00)
        wb(0xFD, 0x00)
        wb(0xF8, 0xB1)
        wait_done()
        for i in range(n):
            out.append(str(rb(0xF9 + i)))
        addr += n
        rem -= n
    sys.stdout.write("\n".join(out) + "\n")
finally:
    os.close(fd)
PY
        return $?
    fi

    local addr="$start" rem="$len" n chunk
    while (( rem > 0 )); do
        n=$(( rem >= 4 ? 4 : rem ))
        chunk=$(ec_read_xram_bytes "$(( (addr >> 8) & 0xFF ))" "$(( addr & 0xFF ))" "$n") || return 1
        printf '%s\n' $chunk
        addr=$(( addr + n ))
        rem=$(( rem - n ))
    done
}

ec_c0_dump_rows() {
    local count=$((10#$1))
    if (( EUID == 0 )) && command -v python3 >/dev/null 2>&1; then
        python3 - "$EC_IO" "$count" <<'PY'
import os, sys, time

ec_io, count = sys.argv[1], int(sys.argv[2])
fd = os.open(ec_io, os.O_RDWR)
try:
    def rb(off):
        os.lseek(fd, off, os.SEEK_SET)
        b = os.read(fd, 1)
        if len(b) != 1:
            raise RuntimeError(f"short read at 0x{off:02X}")
        return b[0]

    def wb(off, val):
        os.lseek(fd, off, os.SEEK_SET)
        os.write(fd, bytes([val & 0xFF]))

    def wait_c0_done():
        for _ in range(250):
            if rb(0xF8) != 0xC0:
                return
            time.sleep(0.002)
        return

    for sub in range(count):
        wb(0xF9, sub)
        wb(0xFA, 0x00)
        wb(0xFB, 0x00)
        wb(0xFC, 0x00)
        wb(0xFD, 0x00)
        wb(0xF8, 0xC0)
        wait_c0_done()
        vals = [rb(off) for off in range(0xF8, 0xFE)]
        # Match DSDT PK0C/GCMD: clear mailbox doorbell 0xF8 after 0xC0 (which returns 0xFA in 0xF8).
        wb(0xF8, 0x00)
        print(sub, *vals)
finally:
    os.close(fd)
PY
        return $?
    fi

    local sub off i val
    for ((sub = 0; sub < count; sub++)); do
        ec_write_byte 0xF9 "$sub" || return 1
        ec_write_byte 0xFA 0x00 || return 1
        ec_write_byte 0xFB 0x00 || return 1
        ec_write_byte 0xFC 0x00 || return 1
        ec_write_byte 0xFD 0x00 || return 1
        ec_write_byte 0xF8 0xC0 || return 1
        for ((i = 0; i < 50; i++)); do
            val=$(ec_read_byte 0xF8 2>/dev/null) && [[ "$val" =~ ^[0-9]+$ ]] && (( 10#$val != 0xC0 )) && break
            sleep 0.02
        done
        local -a vals=()
        for ((off = 0xF8; off <= 0xFD; off++)); do
            vals+=("$(ec_read_byte "$off")")
        done
        ec_write_byte 0xF8 0x00 || return 1
        printf '%d %s\n' "$sub" "${vals[*]}"
    done
}

print_hex_block() {
    local base=$((10#$1))
    shift
    local -a data=("$@")
    local total=${#data[@]}
    local i j b hex_part asc_part ch
    for ((i = 0; i < total; i += 16)); do
        hex_part=""
        asc_part=""
        for ((j = 0; j < 16; j++)); do
            if (( i + j < total )); then
                b=$((10#${data[i + j]}))
                hex_part+=$(printf '%02X ' "$b")
                if (( b >= 32 && b <= 126 )); then
                    printf -v ch '\\x%02x' "$b"
                    asc_part+=$(printf '%b' "$ch")
                else
                    asc_part+="."
                fi
            else
                hex_part+="   "
                asc_part+=" "
            fi
        done
        printf "  %04X: %s |%s|\n" "$((base + i))" "$hex_part" "$asc_part"
    done
}

decode_xram_fan_table() {
    local label="$1" base_addr="$2" off="$3"
    shift 3
    local -a x=("$@")
    local t1=${x[off]} t2=${x[off+1]} t3=${x[off+2]} t4=${x[off+3]}
    local d1=${x[off+4]} d2=${x[off+5]} d3=${x[off+6]} d4=${x[off+7]}
    local r12=$(( (x[off+8] << 8) | x[off+9] ))
    local r23=$(( (x[off+10] << 8) | x[off+11] ))
    local r34=$(( (x[off+12] << 8) | x[off+13] ))
    local p1 p2 p3 p4
    p1=$(raw_duty_to_pct "$d1")
    p2=$(raw_duty_to_pct "$d2")
    p3=$(raw_duty_to_pct "$d3")
    p4=$(raw_duty_to_pct "$d4")
    printf "  %-4s (%s): T=[%3d,%3d,%3d,%3d]C  D_raw=[%3d,%3d,%3d,%3d] (%d%%,%d%%,%d%%,%d%%)  R=[0x%04X,0x%04X,0x%04X]\n" \
        "$label" "$base_addr" "$t1" "$t2" "$t3" "$t4" "$d1" "$d2" "$d3" "$d4" "$p1" "$p2" "$p3" "$p4" "$r12" "$r23" "$r34"
}

cmd_dump_xram() {
    if (( DRY_RUN )); then
        echo "[dry-run] ec[0xF9]=0x00, ec[0xF8]=0xD8 -> read ec[0xFA] (power mode)"
        echo "[dry-run] ec[0xF9]=0x00, ec[0xF8]=0xD7 -> read ec[0xFA] (fan mode)"
        echo "[dry-run] read full EC RAM 0x00..0xFF (256 bytes) from $EC_IO"
        echo "[dry-run] read EC XRAM 0x0300..0x03CF (208 bytes via ECMD 0xB1, 4-byte chunks)"
        echo "[dry-run] query ECMD 0xC0 subcommands 0..15 (0xF9=sub, 0xFA..0xFD=0x00, 0xF8=0xC0 -> read 0xF8..0xFD)"
        return 0
    fi
    check_ec_sys

    ec_write_byte 0xF9 0x00
    ec_write_byte 0xF8 0xD8
    ec_wait_cmd_done
    local pmode fmode
    pmode=$(ec_read_byte 0xFA)

    ec_write_byte 0xF9 0x00
    ec_write_byte 0xF8 0xD7
    ec_wait_cmd_done
    fmode=$(ec_read_byte 0xFA)

    local raw_dump
    if (( EUID == 0 )); then
        raw_dump=$(dd if="$EC_IO" bs=1 count=256 status=none | od -An -v -tu1 -w1 | tr -d ' \t')
    else
        raw_dump=$(sudo dd if="$EC_IO" bs=1 count=256 status=none | od -An -v -tu1 -w1 | tr -d ' \t')
    fi
    local -a ec=()
    mapfile -t ec <<< "$raw_dump"

    local xram_raw c0_raw
    xram_raw=$(xram_read_range_bytes $((0x0300)) 208)
    local -a xram=()
    mapfile -t xram <<< "$xram_raw"

    c0_raw=$(ec_c0_dump_rows 16)

    printf "=== EC Mailbox Modes ===\n"
    printf "  Power Mode (0xD8): 0x%02X (%d)\n" "$pmode" "$pmode"
    printf "  Fan Mode   (0xD7): 0x%02X (%s)\n" "$fmode" "$(decode_fan_mode "$fmode" "${ec[0xD9]}" "${ec[0xDC]}")"

    printf "=== EC RAM 0x00..0xFF (%s) ===\n" "$EC_IO"
    print_hex_block 0 "${ec[@]}"

    printf "=== EC XRAM 0x0300..0x03CF (RAM2/RAM3, 208 bytes via 0xB1) ===\n"
    print_hex_block $((0x0300)) "${xram[@]}"

    local -a fanb_nz=()
    local idx
    for ((idx = 0; idx < 0x70; idx++)); do
        if (( xram[idx] != 0 )); then
            fanb_nz+=("$(printf '0x%04X=0x%02X' "$((0x0300 + idx))" "${xram[idx]}")")
        fi
    done
    printf "=== Decoded RAM2/RAM3 Sections ===\n"
    if (( ${#fanb_nz[@]} == 0 )); then
        echo "  FANB (0x0300..0x036F): (all zeros)"
    else
        echo "  FANB (0x0300..0x036F) non-zero: ${fanb_nz[*]}"
    fi
    printf "  0x0370..0x0374: F4CR=0x%02X%02X SF4R=0x%02X%02X F4ID=0x%02X\n" \
        "${xram[0x70]}" "${xram[0x71]}" "${xram[0x72]}" "${xram[0x73]}" "${xram[0x74]}"
    printf "  0x037D..0x037F: LOW1=0x%02X (%d)  LOW2=0x%02X (%d)  LFG=0x%02X\n" \
        "${xram[0x7D]}" "${xram[0x7D]}" "${xram[0x7E]}" "${xram[0x7E]}" "${xram[0x7F]}"
    printf "  0x0380..0x038B: KL=%02X:%02X:%02X KM=%02X:%02X:%02X KR=%02X:%02X:%02X KBBH=0x%02X FANQ=0x%02X KBTP/KBTY=0x%02X\n" \
        "${xram[0x80]}" "${xram[0x81]}" "${xram[0x82]}" "${xram[0x83]}" "${xram[0x84]}" "${xram[0x85]}" \
        "${xram[0x86]}" "${xram[0x87]}" "${xram[0x88]}" "${xram[0x89]}" "${xram[0x8A]}" "${xram[0x8B]}"
    decode_xram_fan_table "F1" "0x038C" $((0x8C)) "${xram[@]}"
    decode_xram_fan_table "F2" "0x039A" $((0x9A)) "${xram[@]}"
    decode_xram_fan_table "F3" "0x03A8" $((0xA8)) "${xram[@]}"
    printf "  0x03B6..0x03BB: KF=%02X:%02X:%02X KB=%02X:%02X:%02X\n" \
        "${xram[0xB6]}" "${xram[0xB7]}" "${xram[0xB8]}" "${xram[0xB9]}" "${xram[0xBA]}" "${xram[0xBB]}"
    printf "  0x03BC..0x03CF: SF1..3=[%02X%02X,%02X%02X,%02X%02X] ID1..3=[%02X,%02X,%02X] PPPW=%02X%02X F1..4IO=[%02X,%02X,%02X,%02X] VER1=0x%02X\n" \
        "${xram[0xBC]}" "${xram[0xBD]}" "${xram[0xBE]}" "${xram[0xBF]}" "${xram[0xC0]}" "${xram[0xC1]}" \
        "${xram[0xC2]}" "${xram[0xC3]}" "${xram[0xC4]}" "${xram[0xC5]}" "${xram[0xC6]}" \
        "${xram[0xC9]}" "${xram[0xCA]}" "${xram[0xCB]}" "${xram[0xCC]}" "${xram[0xCF]}"

    printf "=== ECMD 0xC0 Subcommands 0x00..0x0F (F8..FD) ===\n"
    local sub f8 f9 fa fb fc fd
    while read -r sub f8 f9 fa fb fc fd; do
        printf "  0xC0 sub 0x%02X: F8=%02X F9=%02X FA=%02X FB=%02X FC=%02X FD=%02X\n" \
            "$sub" "$f8" "$f9" "$fa" "$fb" "$fc" "$fd"
    done <<< "$c0_raw"
}

cmd_scan_xram() {
    local start_hex="${EXTRA_ARGS[0]}" len_hex="${EXTRA_ARGS[1]}"
    local start len
    if ! start=$(parse_hex_u16 "$start_hex") || ! len=$(parse_hex_u16 "$len_hex"); then
        echo "Error: --scan-xram expects hex <start_hex> <len_hex> (got '$start_hex' '$len_hex')." >&2
        exit 1
    fi
    if (( start < 0 || start > 0xFFFF || len < 1 || start + len > 0x10000 )); then
        printf "Error: scan range 0x%04X..0x%04X out of 16-bit XRAM bounds.\n" "$start" "$((start + len - 1))" >&2
        exit 1
    fi
    if (( DRY_RUN )); then
        printf "[dry-run] read EC XRAM 0x%04X..0x%04X (%d bytes via ECMD 0xB1, 4-byte stride)\n" \
            "$start" "$((start + len - 1))" "$len"
        return 0
    fi
    check_ec_sys
    local raw
    raw=$(xram_read_range_bytes "$start" "$len")
    local -a bytes=()
    mapfile -t bytes <<< "$raw"

    printf "=== EC XRAM Scan 0x%04X..0x%04X (%d bytes via 0xB1) ===\n" \
        "$start" "$((start + len - 1))" "${#bytes[@]}"
    print_hex_block "$start" "${bytes[@]}"

    printf "=== Monotonic Curve-Like Hits (T=[30..100], D=[0..255]) ===\n"
    local i t1 t2 t3 t4 d1 d2 d3 d4 hits=0
    for ((i = 0; i + 7 < ${#bytes[@]}; i++)); do
        t1=${bytes[i]} t2=${bytes[i+1]} t3=${bytes[i+2]} t4=${bytes[i+3]}
        d1=${bytes[i+4]} d2=${bytes[i+5]} d3=${bytes[i+6]} d4=${bytes[i+7]}
        if (( t1 >= 30 && t1 < t2 && t2 < t3 && t3 < t4 && t4 <= 100 && d1 <= d2 && d2 <= d3 && d3 <= d4 && d4 > 0 && d4 <= 255 )); then
            printf "  0x%04X: T=[%d,%d,%d,%d] D_raw=[%d,%d,%d,%d] (%d%%,%d%%,%d%%,%d%%)\n" \
                "$((start + i))" "$t1" "$t2" "$t3" "$t4" "$d1" "$d2" "$d3" "$d4" \
                "$(raw_duty_to_pct "$d1")" "$(raw_duty_to_pct "$d2")" "$(raw_duty_to_pct "$d3")" "$(raw_duty_to_pct "$d4")"
            hits=$((hits + 1))
        fi
    done
    if (( hits == 0 )); then
        echo "  (none)"
    fi
}

case "${CMD,,}" in
    status)                 show_status ;;
    dump-xram)              cmd_dump_xram ;;
    scan-xram)              cmd_scan_xram ;;
    auto)                   send_fan_mode 0x02 "auto" ;;
    perf-auto|mode2)        send_fan_mode 0x08 "Auto (0x08, Performance Profile)" ;;
    max|full)               send_fan_mode 0x10 "max / full" ;;
    maxq)                   send_fan_mode 0x01 "maxq" ;;
    custom|curve)           cmd_custom ;;
    silent|slow|noiseless)  send_fan_mode 0x40 "silent / noiseless" ;;
    mode7)                  send_fan_mode 0x20 "mode7" ;;
    offset)
        PCT="${EXTRA_ARGS[0]}"
        if [[ ! "$PCT" =~ ^[0-9]+$ ]] || (( 10#$PCT < 0 || 10#$PCT > 100 )); then
            echo "Error: offset requires percentage 0..100 (got '${PCT:-missing}')." >&2
            exit 1
        fi
        PCT=$((10#$PCT))
        RAW=$(( (PCT * 255 + 50) / 100 ))
        if (( DRY_RUN )); then
            printf "[dry-run] ec[0xE7]=0x%02X (%d%%) -> %s\n" "$RAW" "$PCT" "$EC_IO"
            exit 0
        fi
        check_ec_sys
        ec_write_byte 0xE7 "$RAW"
        printf "Fan speed offset set to %d%% (0x%02X at 0xE7).\n" "$PCT" "$RAW"
        ;;
    reset)
        if (( DRY_RUN )); then
            echo "[dry-run] ec[0xF8]=0xD9 (reset fan table) -> $EC_IO"
            echo "[dry-run] reset curve cache -> $CURVE_STATE_FILE (40,50,60,60,80,70,100,100)"
            exit 0
        fi
        check_ec_sys
        ec_write_byte 0xF8 0xD9
        ec_wait_cmd_done
        save_curve_state "40,50,60,60,80,70,100,100" "40,50,60,60,80,70,100,100"
        echo "EC fan table reset to default."
        ;;
    *)
        echo "Error: unknown command '$CMD'." >&2
        usage >&2
        exit 1 ;;
esac
