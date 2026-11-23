#!/bin/bash
# Power Profile, RAPL PL1/PL2 & dGPU Runtime PM Control for Acer Aspire A715-59G (Direct ec_sys + intel_rapl + PCI PM / nvidia-smi)
#
# Verified from Aspire A715-59G BIOS V1.16 DSDT.dsl (EC81 / ECMD / APPM / APRD):
#   EC Mailbox: 0xF8=FCMD, 0xF9=FDAT, 0xFA=FBUF (FDAT/FBUF must be written BEFORE FCMD)
#     Read  0xD8: 0xF9=0x00, 0xF8=0xD8 -> read 0xFA
#     Write 0xD8: 0xEB |= 0x40 (APRD=1), 0xF9=0x01, 0xFA=<mode>, 0xF8=0xD8
#   EC Power Modes (0xD8) & Static OEM Power Table (applied to OS RAPL):
#     0x00 = Entertainment / Balanced   (OEM PL1=45W, PL2=115W, dGPU PL=60W, PCI PM=on)
#     0x01 = Performance                (OEM PL1=90W, PL2=115W, dGPU PL=75W, PCI PM=on)
#     0x02 = Quiet                      (OEM PL1=15W, PL2=30W,  dGPU PL=25W, PCI PM=on)
#     0x03 = Power Saving               (OEM PL1=15W, PL2=30W,  dGPU RTD3 D3cold auto)
#   EC PECI Power Limit Telemetry (READ-ONLY EC H2RAM 0xF0..0xF4, LE16 0x8000 | (Watts * 8)):
#     0xF0..0xF1 (PL1T), 0xF2..0xF3 (PL2T), 0xF4 (TAUT) — EC hardware PECI telemetry
#   Linux RAPL Powercap (per-profile OS software ceiling defaults, overrideable via --pl1/--pl2):
#     /sys/class/powercap/intel-rapl:0/constraint_{0,1}_power_limit_uw
#     /sys/class/powercap/intel-rapl-mmio:0/constraint_{0,1}_power_limit_uw

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
EC_IO="/sys/kernel/debug/ec/ec0/io"
RAPL_MSR="/sys/class/powercap/intel-rapl:0"
RAPL_MMIO="/sys/class/powercap/intel-rapl-mmio:0"
GPU_PCI_DEV="/sys/bus/pci/devices/0000:01:00.0"
GPU_AUDIO_PCI_DEV="/sys/bus/pci/devices/0000:01:00.1"
EC_FILE=""
DRY_RUN=0
SYNC_OS=1
SYNC_RAPL=1
GPU_BOOST_REQUESTED=0
PL1_OVERRIDE=""
PL2_OVERRIDE=""
ACTION=""

usage() {
    cat <<'EOF'
Usage: power_profile.sh [options] [command|profile]

Commands / Profiles:
  status | get                    Query EC power profile (0xD8), EC hardware PECI PL1T/PL2T, OS RAPL ceiling, and GPU power / RTD3 state
  quiet | silent                  Quiet mode                (EC 0x02; OEM RAPL 15W/30W,  dGPU 25W via nvidia-smi -pl, PCI control=on)
  powersaving | saving | eco      Power Saving / Eco mode   (EC 0x03; OEM RAPL 15W/30W,  dGPU RTD3 D3cold via -pm 0 + PCI control=auto)
  entertainment | balanced | ent  Entertainment / Balanced  (EC 0x00; OEM RAPL 45W/115W, dGPU 60W via nvidia-smi -pl, PCI control=on)
  performance | perf              Performance mode          (EC 0x01; OEM RAPL 90W/115W, dGPU 75W via nvidia-smi -pl, PCI control=on)

Notes:
  Switching profiles sets EC 0xEB bit 6 (APRD=1) + writes EC 0xD8 (EC firmware enforces hardware PECI CPU limits),
  resolves PL1/PL2 from the static OEM table (15/30W, 15/30W, 45/115W, 90/115W) for Linux intel-rapl /
  intel-rapl-mmio unless overridden via --pl1/--pl2/--no-rapl, and manages dGPU power via nvidia-smi -pl + PCI Runtime PM.
  EC 0xF0..0xF4 (PL1T/PL2T/TAUT) are read-only EC PECI telemetry.

Options:
  --pl1 <watts>              Override OS RAPL PL1 in Watts (1..250; default depends on profile)
  --pl2 <watts>              Override OS RAPL PL2 in Watts (1..250; default depends on profile)
  --gpu-boost <value>        Deprecated (WMI disabled): prints notice that GPU power uses nvidia-smi -pl + PCI Runtime PM and exits
  --no-wmi                   Harmless no-op flag (WMI disabled; kept for CLI backward compatibility)
  --no-rapl                  Do not update Linux intel-rapl / intel-rapl-mmio OS software ceiling
  --no-os                    Do not invoke powerprofilesctl alongside EC write
  --ec-file <path>           Read static >=256B EC snapshot file (for status testing only)
  --dry-run                  Print exact EC, OS RAPL, PCI Runtime PM & nvidia-smi operations without touching hardware
  -h, --help                 Show this help message
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

validate_ec_file() {
    if [[ ! -f "$EC_FILE" || ! -r "$EC_FILE" ]]; then
        echo "Error: --ec-file '$EC_FILE' does not exist or is not readable." >&2
        exit 1
    fi
    local size
    size=$(wc -c < "$EC_FILE")
    if (( size < 256 )); then
        echo "Error: --ec-file '$EC_FILE' is too small (${size} bytes; expected >= 256)." >&2
        exit 1
    fi
}

ec_read_byte() {
    local off="$1"
    local src="${EC_FILE:-$EC_IO}"
    local byte=""
    if [[ -n "$EC_FILE" ]] || (( EUID == 0 )) || [[ -r "$src" ]]; then
        byte=$(dd if="$src" bs=1 skip="$((off))" count=1 status=none | od -An -tu1 | tr -d ' \n')
    else
        byte=$(sudo dd if="$src" bs=1 skip="$((off))" count=1 status=none | od -An -tu1 | tr -d ' \n')
    fi
    if [[ ! "$byte" =~ ^[0-9]+$ ]]; then
        echo "Error: failed to read EC byte at offset $off from '$src'." >&2
        return 1
    fi
    printf '%s\n' "$byte"
}

fmt_uw_watts() {
    local uw="$1"
    printf '%d.%03d W' $((uw / 1000000)) $(( (uw % 1000000) / 1000 ))
}

profile_oem_spec() {
    case "${1,,}" in
        quiet|silent)
            printf 'quiet 15 30\n' ;;
        powersaving|power-saving|saving|eco|saver)
            printf 'powersaving 15 30\n' ;;
        entertainment|balanced|ent)
            printf 'entertainment 45 115\n' ;;
        performance|perf)
            printf 'performance 90 115\n' ;;
        *)
            return 1 ;;
    esac
}

resolve_oem_pl1_pl2() {
    local profile="$1"
    local canonical pl1_w pl2_w
    read -r canonical pl1_w pl2_w < <(profile_oem_spec "$profile")

    RESOLVED_PL1_UW=$(( pl1_w * 1000000 ))
    RESOLVED_PL2_UW=$(( pl2_w * 1000000 ))
    RESOLVED_SOURCE="static_oem_table"
    RESOLVED_DETAIL="${canonical} ${pl1_w}W/${pl2_w}W"

    local pl1_str pl2_str
    pl1_str=$(fmt_uw_watts "$RESOLVED_PL1_UW")
    pl2_str=$(fmt_uw_watts "$RESOLVED_PL2_UW")
    if (( DRY_RUN )); then
        echo "[dry-run] OEM PL1/PL2 (${canonical}): PL1=${pl1_str} (${RESOLVED_PL1_UW} uW), PL2=${pl2_str} (${RESOLVED_PL2_UW} uW) [source=${RESOLVED_SOURCE}; ${RESOLVED_DETAIL}]"
    else
        echo "OEM PL1/PL2 (${canonical}): PL1=${pl1_str} (${RESOLVED_PL1_UW} uW), PL2=${pl2_str} (${RESOLVED_PL2_UW} uW) [source=${RESOLVED_SOURCE}; ${RESOLVED_DETAIL}]"
    fi
}

ec_write_byte() {
    local off="$1" val="$2"
    local num hex
    if [[ "$val" =~ ^0x[0-9a-fA-F]+$ ]]; then
        num=$((val))
    elif [[ "$val" =~ ^[0-9]+$ ]]; then
        num=$((10#$val))
    else
        echo "Error: invalid byte value '$val'." >&2
        return 1
    fi
    if (( num < 0 || num > 255 )); then
        echo "Error: EC byte value out of range (0..255): $val" >&2
        return 1
    fi
    printf -v hex '%02x' "$num"
    if (( EUID == 0 )) || [[ -w "$EC_IO" ]]; then
        printf "\\x$hex" | dd of="$EC_IO" bs=1 seek="$((off))" conv=notrunc status=none
    else
        printf "\\x$hex" | sudo dd of="$EC_IO" bs=1 seek="$((off))" conv=notrunc status=none
    fi
}

ec_wait_cmd_done() {
    local i val
    for ((i = 0; i < 50; i++)); do
        val=$(ec_read_byte 0xF8 2>/dev/null) || val=""
        if [[ "$val" =~ ^[0-9]+$ ]] && (( val == 0 )); then
            return 0
        fi
        sleep 0.02
    done
    echo "Warning: EC FCMD (0xF8) did not clear to 0x00 within timeout." >&2
    return 1
}

mode_name() {
    local raw="$1"
    if [[ ! "$raw" =~ ^[0-9]+$ ]]; then
        echo "unknown ($raw)"
        return 0
    fi
    case "$((10#$raw))" in
        0) echo "entertainment / balanced (0x00)" ;;
        1) echo "performance (0x01)" ;;
        2) echo "quiet (0x02)" ;;
        3) echo "powersaving (0x03)" ;;
        *) printf "unknown (0x%02X)\n" "$((10#$raw))" ;;
    esac
}

fmt_rapl() {
    local dir="$1" label="$2"
    [[ -r "$dir/constraint_0_power_limit_uw" && -r "$dir/constraint_1_power_limit_uw" ]] || return 0
    local p0 p1 max0="" extra=" (OS ceiling)"
    p0=$(cat "$dir/constraint_0_power_limit_uw" 2>/dev/null) || return 0
    p1=$(cat "$dir/constraint_1_power_limit_uw" 2>/dev/null) || return 0
    [[ "$p0" =~ ^[0-9]+$ && "$p1" =~ ^[0-9]+$ ]] || return 0
    if [[ -r "$dir/constraint_0_max_power_uw" ]]; then
        max0=$(cat "$dir/constraint_0_max_power_uw" 2>/dev/null) || max0=""
        if [[ "$max0" =~ ^[0-9]+$ ]] && (( max0 > 0 )); then
            printf -v extra " (OS ceiling; nominal TDP=%d.%03d W)" $((max0 / 1000000)) $(( (max0 % 1000000) / 1000 ))
        fi
    fi
    printf "%-14s: %d.%03d W / %d.%03d W%s\n" "$label" \
        $((p0 / 1000000)) $(( (p0 % 1000000) / 1000 )) \
        $((p1 / 1000000)) $(( (p1 % 1000000) / 1000 )) \
        "$extra"
}

write_sysfs_value() {
    local path="$1" val="$2"
    [[ -e "$path" ]] || return 0
    if (( EUID == 0 )) && [[ -w "$path" ]]; then
        printf '%s\n' "$val" > "$path" 2>/dev/null || \
            echo "Warning: failed to write '$val' to $path" >&2
    else
        printf '%s\n' "$val" | sudo tee "$path" >/dev/null 2>&1 || \
            echo "Warning: failed to write '$val' to $path" >&2
    fi
}

write_ec_aprd() {
    local eb_cur eb_new
    eb_cur=$(ec_read_byte 0xEB)
    eb_new=$(( 10#$eb_cur | 0x40 ))
    ec_write_byte 0xEB "$eb_new"
}

read_ec_mode() {
    ec_write_byte 0xF9 0x00
    ec_write_byte 0xF8 0xD8
    if ec_wait_cmd_done; then
        ec_read_byte 0xFA
        return 0
    fi
    return 1
}

write_ec_mode() {
    local mode="$1"
    write_ec_aprd
    ec_write_byte 0xF9 0x01
    ec_write_byte 0xFA "$((mode))"
    ec_write_byte 0xF8 0xD8
    if ! ec_wait_cmd_done; then
        echo "Error: EC did not acknowledge 0xD8 profile write (FCMD=0xF8 timeout)." >&2
        return 1
    fi
}

fmt_watts_token() {
    local tok="$1"
    if [[ "$tok" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        printf '%s W' "$tok"
    else
        printf '%s' "$tok"
    fi
}

show_gpu_status() {
    local cur_mode="${1:-}"
    local rt_status="" pm_control=""
    if [[ ! -d "$GPU_PCI_DEV" ]]; then
        echo "GPU Power     : Disabled / Suspended (D3cold / Power Saving mode)"
        return 0
    fi
    [[ -r "$GPU_PCI_DEV/power/runtime_status" ]] && rt_status=$(<"$GPU_PCI_DEV/power/runtime_status")
    [[ -r "$GPU_PCI_DEV/power/control" ]] && pm_control=$(<"$GPU_PCI_DEV/power/control")

    if [[ "$rt_status" == "suspended" ]]; then
        echo "GPU Power     : Disabled / Suspended (D3cold / Power Saving mode)"
        return 0
    fi
    if [[ "$pm_control" == "auto" && "$rt_status" == "active" && "$cur_mode" =~ ^[0-9]+$ ]] && (( 10#$cur_mode == 3 )); then
        echo "GPU Power     : RTD3 Auto / D3cold Pending (Power Saving mode; display active)"
        return 0
    fi

    local smi_raw="" cur_lim="n/a" def_lim="n/a" max_lim="n/a"
    local powerd_state="n/a" cur_mw="" gpu_tier="" meta_extra=""
    if command -v nvidia-smi >/dev/null 2>&1; then
        smi_raw=$(nvidia-smi --query-gpu=power.limit,power.default_limit,power.max_limit --format=csv,noheader,nounits 2>/dev/null | head -n 1) || smi_raw=""
        if [[ -n "$smi_raw" ]]; then
            IFS=',' read -r cur_lim def_lim max_lim <<<"$smi_raw"
            cur_lim="${cur_lim#"${cur_lim%%[![:space:]]*}"}"; cur_lim="${cur_lim%"${cur_lim##*[![:space:]]}"}"
            def_lim="${def_lim#"${def_lim%%[![:space:]]*}"}"; def_lim="${def_lim%"${def_lim##*[![:space:]]}"}"
            max_lim="${max_lim#"${max_lim%%[![:space:]]*}"}"; max_lim="${max_lim%"${max_lim##*[![:space:]]}"}"
        fi
    fi
    if [[ "$cur_lim" =~ ^([0-9]+)(\.([0-9]+))?$ ]]; then
        local whole="${BASH_REMATCH[1]}"
        local frac="${BASH_REMATCH[3]}000"
        cur_mw=$(( 10#$whole * 1000 + 10#${frac:0:3} ))
    fi
    if command -v systemctl >/dev/null 2>&1; then
        powerd_state=$(systemctl is-active nvidia-powerd 2>/dev/null || true)
        powerd_state="${powerd_state:-unknown}"
    fi
    if [[ -n "$cur_mw" ]]; then
        if (( cur_mw >= 74000 )); then
            gpu_tier="Performance 75W"
        elif (( cur_mw > 55000 )); then
            gpu_tier="Entertainment 60W"
        else
            gpu_tier="Quiet 25W"
        fi
    fi
    if [[ -n "$gpu_tier" ]]; then
        meta_extra=" [${gpu_tier}, cur=$(fmt_watts_token "$cur_lim")]"
    fi

    printf "GPU Power     : %s / default %s / max %s (nvidia-powerd=%s, power/control=%s, runtime=%s)%s\n" \
        "$(fmt_watts_token "$cur_lim")" \
        "$(fmt_watts_token "$def_lim")" \
        "$(fmt_watts_token "$max_lim")" \
        "$powerd_state" \
        "${pm_control:-unknown}" \
        "${rt_status:-unknown}" \
        "$meta_extra"
}

show_status() {
    local src="${EC_FILE:-$EC_IO}"
    local pl1_lo pl1_hi pl2_lo pl2_hi tau cur_mode=""
    if [[ -z "$EC_FILE" ]]; then
        if (( DRY_RUN )); then
            echo "[dry-run] ec[0xF9]=0x00, ec[0xF8]=0xD8 -> read ec[0xFA], EC hardware PECI PL1T/PL2T (0xF0..0xF4), OS RAPL ceiling sysfs, PCI power/runtime_status & GPU power"
            return 0
        fi
        check_ec_sys
        if cur_mode=$(read_ec_mode); then
            echo "Power Profile : $(mode_name "$cur_mode")"
        else
            echo "Power Profile : unknown (EC mailbox 0xD8 read timed out)"
        fi
    else
        echo "Power Profile : (snapshot mode; mailbox read 0xD8 skipped on static file $src)"
    fi

    pl1_lo=$(ec_read_byte 0xF0)
    pl1_hi=$(ec_read_byte 0xF1)
    pl2_lo=$(ec_read_byte 0xF2)
    pl2_hi=$(ec_read_byte 0xF3)
    tau=$(ec_read_byte 0xF4)
    local pl1_word=$(( (pl1_hi << 8) | pl1_lo ))
    local pl2_word=$(( (pl2_hi << 8) | pl2_lo ))
    local pl1_en=$(( (pl1_word >> 15) & 1 ))
    local pl2_en=$(( (pl2_word >> 15) & 1 ))
    local pl1_raw=$(( pl1_word & 0x7FFF ))
    local pl2_raw=$(( pl2_word & 0x7FFF ))
    printf "EC PL1T/PL2T  : %d.%03d W (en=%d) / %d.%03d W (en=%d) (TAUT=0x%02X) (EC hardware PECI)\n" \
        $((pl1_raw / 8)) $(( (pl1_raw % 8) * 125 )) "$pl1_en" \
        $((pl2_raw / 8)) $(( (pl2_raw % 8) * 125 )) "$pl2_en" \
        "$tau"

    echo "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)"

    fmt_rapl "$RAPL_MSR"  "RAPL MSR"
    fmt_rapl "$RAPL_MMIO" "RAPL MMIO"
    show_gpu_status "$cur_mode"
}

write_rapl_constraint() {
    local dir="$1" idx="$2" label="$3" target_uw="$4"
    local limit_file="$dir/constraint_${idx}_power_limit_uw"
    local max_file="$dir/constraint_${idx}_max_power_uw"
    [[ -e "$limit_file" ]] || return 0

    local max_uw="" exceeds_max=0 write_ok=0
    if [[ -r "$max_file" ]]; then
        max_uw=$(cat "$max_file" 2>/dev/null) || max_uw=""
        if [[ "$max_uw" =~ ^[0-9]+$ ]] && (( max_uw > 0 && target_uw > max_uw )); then
            exceeds_max=1
        fi
    fi

    if (( EUID == 0 )) && [[ -w "$limit_file" ]]; then
        printf '%s\n' "$target_uw" > "$limit_file" 2>/dev/null && write_ok=1
    else
        printf '%s\n' "$target_uw" | sudo tee "$limit_file" >/dev/null 2>&1 && write_ok=1
    fi

    if (( ! write_ok )); then
        if (( exceeds_max )); then
            echo "Note: $label target (${target_uw} uW) exceeds nominal TDP (${max_uw} uW in $max_file)."
        fi
        echo "Warning: failed to write $target_uw to $limit_file" >&2
        return 0
    fi

    local actual_uw=""
    if [[ -r "$limit_file" ]]; then
        actual_uw=$(cat "$limit_file" 2>/dev/null) || actual_uw=""
    fi

    if [[ "$actual_uw" =~ ^[0-9]+$ ]] && (( actual_uw != target_uw )); then
        if (( exceeds_max )); then
            echo "Note: $label target (${target_uw} uW) > nominal TDP (${max_uw} uW in $max_file); kernel latched ${actual_uw} uW."
        else
            echo "Note: $limit_file clamped/set to ${actual_uw} uW (requested ${target_uw} uW)."
        fi
    fi
}

write_rapl_limit() {
    local dir="$1" pl1_uw="$2" pl2_uw="$3"
    [[ -d "$dir" ]] || return 0
    write_rapl_constraint "$dir" 0 "PL1" "$pl1_uw"
    write_rapl_constraint "$dir" 1 "PL2" "$pl2_uw"
}

validate_watts() {
    local name="$1" val="$2"
    if [[ ! "$val" =~ ^[0-9]+$ ]] || (( 10#$val < 1 || 10#$val > 250 )); then
        echo "Error: $name must be an integer 1..250 Watts (got '$val')." >&2
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pl1)
            [[ $# -ge 2 ]] || { echo "Error: --pl1 requires watts." >&2; exit 1; }
            validate_watts "PL1" "$2"
            PL1_OVERRIDE=$((10#$2))
            shift 2 ;;
        --pl2)
            [[ $# -ge 2 ]] || { echo "Error: --pl2 requires watts." >&2; exit 1; }
            validate_watts "PL2" "$2"
            PL2_OVERRIDE=$((10#$2))
            shift 2 ;;
        --gpu-boost)
            GPU_BOOST_REQUESTED=1
            if [[ $# -ge 2 && "$2" != -* ]]; then
                shift 2
            else
                shift
            fi ;;
        --gpu-boost=*)
            GPU_BOOST_REQUESTED=1
            shift ;;
        --no-wmi) shift ;;
        --no-rapl) SYNC_RAPL=0; shift ;;
        --no-os) SYNC_OS=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --ec-file)
            [[ $# -ge 2 && -n "$2" ]] || { echo "Error: --ec-file requires a non-empty path." >&2; exit 1; }
            EC_FILE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *)
            if [[ -n "$ACTION" ]]; then
                echo "Unexpected extra argument: $1" >&2; usage >&2; exit 1
            fi
            ACTION="$1"; shift ;;
    esac
done

if (( GPU_BOOST_REQUESTED )); then
    echo "Notice: WMI communication is disabled; --gpu-boost is not supported and GPU power is managed via nvidia-smi -pl + PCI Runtime PM."
    exit 0
fi

if [[ -n "$EC_FILE" ]]; then
    if [[ -n "$ACTION" && "${ACTION,,}" != "status" && "${ACTION,,}" != "get" ]]; then
        echo "Error: --ec-file is only supported with 'status' / 'get' (read-only snapshot testing)." >&2
        exit 1
    fi
    validate_ec_file
fi

ACTION="${ACTION:-status}"

case "${ACTION,,}" in
    status|get)
        if [[ -n "$PL1_OVERRIDE" || -n "$PL2_OVERRIDE" ]]; then
            echo "Error: --pl1 / --pl2 cannot be used with '${ACTION}'; specify a profile name (or omit '${ACTION}')." >&2
            exit 1
        fi
        show_status
        exit 0
        ;;
    entertainment|balanced|ent)
        EC_MODE=0x00; EC_PL1_W=45; EC_PL2_W=115; GPU_PL_W=60; OS_PROFILE="balanced" ;;
    performance|perf)
        EC_MODE=0x01; EC_PL1_W=90; EC_PL2_W=115; GPU_PL_W=75; OS_PROFILE="performance" ;;
    quiet|silent)
        EC_MODE=0x02; EC_PL1_W=15; EC_PL2_W=30;  GPU_PL_W=25; OS_PROFILE="power-saver" ;;
    powersaving|power-saving|saving|eco|saver)
        EC_MODE=0x03; EC_PL1_W=15; EC_PL2_W=30;  GPU_PL_W=""; OS_PROFILE="power-saver" ;;
    *)
        echo "Error: unknown command/profile '$ACTION'." >&2
        usage >&2
        exit 1 ;;
esac

if (( SYNC_RAPL == 0 )) && [[ -n "$PL1_OVERRIDE" || -n "$PL2_OVERRIDE" ]]; then
    echo "Error: --pl1 / --pl2 cannot be combined with --no-rapl (EC 0xF0..0xF4 are read-only telemetry)." >&2
    exit 1
fi

compute_target_rapl_uw() {
    if [[ -n "$PL1_OVERRIDE" ]]; then
        PL1_UW=$(( PL1_OVERRIDE * 1000000 ))
    else
        PL1_UW="$RESOLVED_PL1_UW"
    fi
    if [[ -n "$PL2_OVERRIDE" ]]; then
        PL2_UW=$(( PL2_OVERRIDE * 1000000 ))
    else
        PL2_UW="$RESOLVED_PL2_UW"
    fi
}

if (( DRY_RUN )); then
    printf "[dry-run] ec[0xEB] |= 0x40 (APRD=1) -> %s\n" "$EC_IO"
    printf "[dry-run] ec[0xF9]=0x01, ec[0xFA]=0x%02X, ec[0xF8]=0xD8 -> %s (OEM %sW/%sW)\n" $((EC_MODE)) "$EC_IO" "$EC_PL1_W" "$EC_PL2_W"
    resolve_oem_pl1_pl2 "$ACTION"
    compute_target_rapl_uw
    if (( SYNC_RAPL )); then
        if [[ -n "$PL1_OVERRIDE" || -n "$PL2_OVERRIDE" ]]; then
            rapl_mode_note="explicit OS RAPL software ceiling override; OEM source=${RESOLVED_SOURCE}"
        else
            rapl_mode_note="resolved OEM PL1/PL2; source=${RESOLVED_SOURCE}"
        fi
        echo "[dry-run] RAPL PL1=${PL1_UW} uW ($(fmt_uw_watts "$PL1_UW")), PL2=${PL2_UW} uW ($(fmt_uw_watts "$PL2_UW")) -> $RAPL_MSR & $RAPL_MMIO (${rapl_mode_note})"
        if [[ -r "$RAPL_MSR/constraint_0_max_power_uw" ]]; then
            max0=$(cat "$RAPL_MSR/constraint_0_max_power_uw" 2>/dev/null) || max0=""
            if [[ "$max0" =~ ^[0-9]+$ ]] && (( max0 > 0 && PL1_UW > max0 )); then
                echo "[dry-run] note: PL1 (${PL1_UW} uW) > nominal TDP (${max0} uW in $RAPL_MSR/constraint_0_max_power_uw); kernel/HW caps only if enforced"
            fi
        fi
    fi
    if (( SYNC_OS )) && command -v powerprofilesctl >/dev/null 2>&1; then
        echo "[dry-run] powerprofilesctl set $OS_PROFILE"
    fi
    if (( $((EC_MODE)) == 3 )); then
        if [[ -d "$GPU_PCI_DEV" ]] && command -v nvidia-smi >/dev/null 2>&1; then
            echo "[dry-run] nvidia-smi -pm 0 >/dev/null 2>&1 || true"
        fi
        for ctrl in "$GPU_PCI_DEV/power/control" "$GPU_AUDIO_PCI_DEV/power/control"; do
            [[ -e "$ctrl" ]] && echo "[dry-run] write 'auto' -> $ctrl"
        done
    else
        if [[ -e "$GPU_PCI_DEV/power/control" ]]; then
            echo "[dry-run] write 'on' -> $GPU_PCI_DEV/power/control"
        fi
        if [[ -n "$GPU_PL_W" ]] && command -v nvidia-smi >/dev/null 2>&1; then
            echo "[dry-run] nvidia-smi -pl $GPU_PL_W"
        fi
    fi
    exit 0
fi

# Step 1: Set APRD=1 (EC 0xEB bit 6) and write EC 0xD8 mailbox
check_ec_sys
write_ec_mode "$EC_MODE"

# Step 2: Resolve static OEM PL1/PL2 and apply OS RAPL ceiling
resolve_oem_pl1_pl2 "$ACTION"
compute_target_rapl_uw
if (( SYNC_RAPL )); then
    write_rapl_limit "$RAPL_MSR"  "$PL1_UW" "$PL2_UW"
    write_rapl_limit "$RAPL_MMIO" "$PL1_UW" "$PL2_UW"
fi

if (( SYNC_OS )) && command -v powerprofilesctl >/dev/null 2>&1; then
    powerprofilesctl set "$OS_PROFILE" || true
fi

# Step 3: Manage dGPU power via PCI Runtime PM + nvidia-smi (no WMI)
if (( $((EC_MODE)) == 3 )); then
    gpu_rt_status=""
    [[ -r "$GPU_PCI_DEV/power/runtime_status" ]] && gpu_rt_status=$(<"$GPU_PCI_DEV/power/runtime_status")
    if [[ -d "$GPU_PCI_DEV" && "$gpu_rt_status" != "suspended" ]] && command -v nvidia-smi >/dev/null 2>&1; then
        if (( EUID == 0 )); then
            nvidia-smi -pm 0 >/dev/null 2>&1 || true
        else
            sudo nvidia-smi -pm 0 >/dev/null 2>&1 || true
        fi
    fi
    write_sysfs_value "$GPU_PCI_DEV/power/control" "auto"
    write_sysfs_value "$GPU_AUDIO_PCI_DEV/power/control" "auto"
else
    write_sysfs_value "$GPU_PCI_DEV/power/control" "on"
    if [[ -n "$GPU_PL_W" && -d "$GPU_PCI_DEV" ]] && command -v nvidia-smi >/dev/null 2>&1; then
        if (( EUID == 0 )); then
            nvidia-smi -pl "$GPU_PL_W" >/dev/null 2>&1 || \
                echo "Warning: nvidia-smi -pl $GPU_PL_W failed." >&2
        else
            sudo nvidia-smi -pl "$GPU_PL_W" >/dev/null 2>&1 || \
                echo "Warning: nvidia-smi -pl $GPU_PL_W failed." >&2
        fi
    fi
fi

if (( SYNC_RAPL )); then
    if [[ -n "$PL1_OVERRIDE" || -n "$PL2_OVERRIDE" ]]; then
        echo "Power profile set to $(mode_name $((EC_MODE))) (OEM source=${RESOLVED_SOURCE}; OS RAPL ceiling override PL1=$(fmt_uw_watts "$PL1_UW"), PL2=$(fmt_uw_watts "$PL2_UW"))."
    else
        echo "Power profile set to $(mode_name $((EC_MODE))) (OEM source=${RESOLVED_SOURCE}; OS RAPL PL1=$(fmt_uw_watts "$PL1_UW"), PL2=$(fmt_uw_watts "$PL2_UW"))."
    fi
else
    echo "Power profile set to $(mode_name $((EC_MODE))) (OEM source=${RESOLVED_SOURCE}; OS RAPL unchanged)."
fi
show_status
