#!/bin/bash
# FlexiCharger Battery Thresholds & EC Battery Telemetry for Acer Aspire A715-59G (Direct ec_sys)
#
# Verified from Aspire A715-59G BIOS V1.16 DSDT & PeakShift.cs:336-352:
#   Static EC RAM:
#     0x10 (ADP bit0=AC plugged, BAT0 bit2=battery present), 0x38..0x39 (BRS0 LE16)=Battery %, 0x94..0x95 (CYCN LE16)=Cycles
#   EC Mailbox FCMD=0xCB (FlexiCharger Write):
#     0xF9=stop% (BTCP), 0xFA=start% (BSCP), 0xFB=enable (0x02=Enable, 0x00=Disable), 0xF8=0xCB
#     Valid BIOS presets: start in {40,50,60,70,80,90}, stop in {60,70,80,90,100}
#   EC XRAM Readback via ECMD=0xB1 (0x06D0..0x06D2, mirrored at 0x0DD0..0x0DD2):
#     0xF9=addr_hi, 0xFA=addr_lo, 0xFB=0x01, 0xFC=0x00, 0xFD=0x00, 0xF8=0xB1 -> read 0xF9
#     0x06D0: bit 1 (0x02)=FlexiCharger enable (0x18 disabled; 0x1A/0x1B enabled, bit 0 charge-inhibit latch)
#     0x06D1: start%, 0x06D2: stop%
#     (Note: ECMD 0xF1 sub 0x04 returns non-threshold 00 47 93 A8 FF 00 Fan 2 duty bytes on this EC.)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
EC_IO="/sys/kernel/debug/ec/ec0/io"
EC_FILE=""
DRY_RUN=0
USING_LIVE_EC=0
ARGS=()

usage() {
    cat <<'EOF'
Usage: battery.sh [options] <command> [args]

Commands:
  status                    Read battery %, cycle count, AC status, and FlexiCharger state (EC XRAM 0x06D0..0x06D2 / 0xB1)
  enable [start% stop%]     Enable FlexiCharger via EC 0xCB (defaults to 60 80 -> 80% limit)
  limit <start%> <stop%>    Enable FlexiCharger charge thresholds via EC 0xCB (e.g. limit 60 80)
  full | disable            Disable FlexiCharger and allow 100% charging (0xF9=100, 0xFA=80, 0xFB=0x00)

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

validate_ec_file() {
    if [[ -z "$EC_FILE" || ! -r "$EC_FILE" ]]; then
        echo "Error: EC snapshot file is missing or unreadable: '${EC_FILE:-}'" >&2
        exit 1
    fi
    local sz
    sz=$(wc -c < "$EC_FILE")
    if (( sz < 256 )); then
        echo "Error: EC snapshot file '$EC_FILE' is too small (${sz} bytes; expected >= 256)." >&2
        exit 1
    fi
}

can_read_live_ec() {
    [[ -e /sys/module/ec_sys ]] || return 1
    if (( EUID == 0 )) || [[ -r "$EC_IO" ]] || sudo -n test -r "$EC_IO" 2>/dev/null; then
        return 0
    fi
    return 1
}

can_write_live_ec() {
    [[ -z "$EC_FILE" ]] && (( ! DRY_RUN )) || return 1
    [[ -e /sys/module/ec_sys ]] || return 1
    [[ "$(cat /sys/module/ec_sys/parameters/write_support 2>/dev/null || echo N)" == "Y" ]] || return 1
    if (( EUID == 0 )) || [[ -w "$EC_IO" ]] || sudo -n test -w "$EC_IO" 2>/dev/null; then
        return 0
    fi
    return 1
}

ec_read_byte() {
    local off="$1"
    local src="${EC_FILE:-$EC_IO}"
    local val
    if [[ -n "$EC_FILE" ]] || (( EUID == 0 )) || [[ -r "$src" ]]; then
        val=$(dd if="$src" bs=1 skip="$((off))" count=1 status=none | od -An -tu1 | tr -d ' \n')
    else
        val=$(sudo dd if="$src" bs=1 skip="$((off))" count=1 status=none | od -An -tu1 | tr -d ' \n')
    fi
    if [[ ! "$val" =~ ^[0-9]+$ ]]; then
        echo "Error: failed to read EC byte at offset $(printf '0x%02X' "$((off))") from '$src'." >&2
        return 1
    fi
    printf '%s' "$val"
}

ec_write_byte() {
    local off="$1" val="$2"
    local byte_hex
    printf -v byte_hex '%02x' "$((val & 0xFF))"
    if (( EUID == 0 )) || [[ -w "$EC_IO" ]]; then
        printf "\\x$byte_hex" | dd of="$EC_IO" bs=1 seek="$((off))" conv=notrunc status=none
    else
        printf "\\x$byte_hex" | sudo dd of="$EC_IO" bs=1 seek="$((off))" conv=notrunc status=none
    fi
}

ec_wait_cmd_done() {
    local strict="${1:-0}"
    local i val
    for ((i = 0; i < 50; i++)); do
        val=$(ec_read_byte 0xF8 2>/dev/null) || val=""
        if [[ "$val" =~ ^[0-9]+$ ]] && (( 10#$val == 0 )); then
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

ec_wait_cmd_ready() {
    ec_wait_cmd_done 1
}

ec_xram_read_byte() {
    local addr_raw="$1"
    if [[ ! "$addr_raw" =~ ^(0[xX][0-9a-fA-F]+|[0-9]+)$ ]]; then
        echo "Error: invalid XRAM address '$addr_raw'." >&2
        return 1
    fi
    local addr=$(( addr_raw ))
    local addr_hi=$(( (addr >> 8) & 0xFF ))
    local addr_lo=$(( addr & 0xFF ))

    ec_wait_cmd_ready || return 1
    ec_write_byte 0xF9 "$addr_hi" || return 1
    ec_write_byte 0xFA "$addr_lo" || return 1
    ec_write_byte 0xFB 0x01 || return 1
    ec_write_byte 0xFC 0x00 || return 1
    ec_write_byte 0xFD 0x00 || return 1
    ec_write_byte 0xF8 0xB1 || return 1
    ec_wait_cmd_done 1 || return 1

    local val
    val=$(ec_read_byte 0xF9 2>/dev/null) || return 1
    [[ "$val" =~ ^[0-9]+$ ]] || return 1
    printf '%d' "$((10#$val))"
}

is_in_list() {
    local needle="$1"; shift
    local item
    for item in "$@"; do
        [[ "$needle" -eq "$item" ]] && return 0
    done
    return 1
}

print_flexicharger_ready() {
    echo "FlexiCharger  : Ready (Direct EC 0xCB / XRAM 0x06D0..0x06D2; run via root/daemon to read live XRAM)"
}

query_flexicharger_status() {
    if (( USING_LIVE_EC == 1 && DRY_RUN == 0 )) && [[ -w "$EC_IO" ]] && can_write_live_ec; then
        local flag start stop flag_hex
        if flag=$(ec_xram_read_byte 0x06D0) &&
           start=$(ec_xram_read_byte 0x06D1) &&
           stop=$(ec_xram_read_byte 0x06D2); then
            flag=$((10#$flag))
            start=$((10#$start))
            stop=$((10#$stop))
            printf -v flag_hex '0x%02X' "$flag"
            if (( (flag & 0x02) != 0 && stop >= 50 && stop <= 100 && start >= 30 && start <= 99 )); then
                printf "FlexiCharger  : Enabled (%d%% limit, Stop=%d%% Start=%d%% | Direct EC 0xCB / XRAM 0x06D0=%s)\n" \
                    "$stop" "$stop" "$start" "$flag_hex"
                return 0
            elif (( (flag & 0x02) == 0 && stop >= 50 && stop <= 100 )); then
                printf "FlexiCharger  : Disabled (100%% full charge, Stop=%d%% Start=%d%% | Direct EC 0xCB / XRAM 0x06D0=%s)\n" \
                    "$stop" "$start" "$flag_hex"
                return 0
            fi
        fi
    else
        local fcmd fdat fbuf fbf1
        fcmd=$(ec_read_byte 0xF8 2>/dev/null || echo "")
        fdat=$(ec_read_byte 0xF9 2>/dev/null || echo "")
        fbuf=$(ec_read_byte 0xFA 2>/dev/null || echo "")
        fbf1=$(ec_read_byte 0xFB 2>/dev/null || echo "")

        if [[ "$fcmd" =~ ^[0-9]+$ && "$fdat" =~ ^[0-9]+$ && "$fbuf" =~ ^[0-9]+$ && "$fbf1" =~ ^[0-9]+$ ]]; then
            fcmd=$((10#$fcmd)); fdat=$((10#$fdat)); fbuf=$((10#$fbuf)); fbf1=$((10#$fbf1))
            if (( fcmd == 0x00 || fcmd == 0xCB )); then
                if (( fdat == 80 && fbuf >= 60 && fbuf <= 80 && fbf1 == 0x02 )); then
                    printf "FlexiCharger  : Enabled (80%% limit, Stop=80%% Start=%d%% | Direct EC 0xCB)\n" "$fbuf"
                    return 0
                elif (( fdat == 100 && fbf1 == 0x00 )); then
                    printf "FlexiCharger  : Disabled (100%% full charge, Stop=100%% Start=%d%% | Direct EC 0xCB)\n" "$fbuf"
                    return 0
                elif (( fbf1 == 0x02 && fdat >= 60 && fdat <= 99 && fbuf >= 20 && fbuf < fdat )); then
                    printf "FlexiCharger  : Enabled (%d%% limit, Stop=%d%% Start=%d%% | Direct EC 0xCB)\n" "$fdat" "$fdat" "$fbuf"
                    return 0
                fi
            fi
        fi
    fi

    print_flexicharger_ready
}

apply_flexicharger() {
    local enable="$1" start="$2" stop="$3"
    local ec_en_byte=0x00 ec_en_label="disable"
    if (( enable == 1 )); then
        ec_en_byte=0x02
        ec_en_label="enable"
    fi

    if (( DRY_RUN )); then
        printf "[dry-run] ec[0xF9]=0x%02X (stop=%d%%), ec[0xFA]=0x%02X (start=%d%%), ec[0xFB]=0x%02X (%s), ec[0xF8]=0xCB -> %s\n" \
            "$stop" "$stop" "$start" "$start" "$ec_en_byte" "$ec_en_label" "$EC_IO"
        return 0
    fi

    check_ec_sys
    ec_wait_cmd_ready || true
    ec_write_byte 0xF9 "$stop"
    ec_write_byte 0xFA "$start"
    ec_write_byte 0xFB "$ec_en_byte"
    ec_write_byte 0xF8 0xCB
    ec_wait_cmd_done || true
}

apply_limit_thresholds() {
    local start_raw="$1" stop_raw="$2"
    if [[ ! "$start_raw" =~ ^[0-9]+$ || ! "$stop_raw" =~ ^[0-9]+$ ]]; then
        echo "Error: expected integer thresholds '<start%> <stop%>' with 20 <= start < stop <= 100." >&2
        exit 1
    fi
    local start=$((10#$start_raw))
    local stop=$((10#$stop_raw))
    if (( start < 20 || start >= stop || stop > 100 )); then
        echo "Error: expected valid thresholds '<start%> <stop%>' with 20 <= start < stop <= 100." >&2
        exit 1
    fi
    if ! is_in_list "$start" 40 50 60 70 80 90 || ! is_in_list "$stop" 60 70 80 90 100; then
        echo "Note: BIOS FCSC/FCTC preset tables list start in {40,50,60,70,80,90} and stop in {60,70,80,90,100}." >&2
    fi
    apply_flexicharger 1 "$start" "$stop"
    if (( ! DRY_RUN )); then
        echo "FlexiCharger enabled: start=${start}%, stop=${stop}%."
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --ec-file)
            [[ $# -ge 2 && -n "$2" ]] || { echo "Error: --ec-file requires a non-empty path." >&2; exit 1; }
            EC_FILE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; ARGS+=("$@"); break ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

CMD="${ARGS[0]:-status}"

case "${CMD,,}" in
    status)
        if (( ${#ARGS[@]} > 1 )); then
            echo "Error: unexpected extra argument '${ARGS[1]}' for status." >&2
            usage >&2
            exit 1
        fi
        if [[ -n "$EC_FILE" ]]; then
            validate_ec_file
        elif (( ! DRY_RUN && EUID == 0 )); then
            check_ec_sys
        fi
        if [[ -r /sys/class/power_supply/BAT0/status && -r /sys/class/power_supply/BAT0/capacity ]]; then
            echo "Linux sysfs   : status=$(</sys/class/power_supply/BAT0/status), capacity=$(</sys/class/power_supply/BAT0/capacity)%"
        fi
        if [[ -z "$EC_FILE" ]] && (( DRY_RUN )); then
            echo "[dry-run] read EC power telemetry (0x10, 0x38..0x39, 0x94..0x95) and FlexiCharger state (XRAM 0x06D0..0x06D2 via ECMD 0xB1)"
            print_flexicharger_ready
            exit 0
        fi

        if [[ -z "$EC_FILE" ]] && can_read_live_ec; then
            USING_LIVE_EC=1
        else
            USING_LIVE_EC=0
        fi

        if [[ -n "$EC_FILE" ]] || (( USING_LIVE_EC == 1 )); then
            pstat=$(ec_read_byte 0x10)
            brs_lo=$(ec_read_byte 0x38); brs_hi=$(ec_read_byte 0x39)
            cyc_lo=$(ec_read_byte 0x94); cyc_hi=$(ec_read_byte 0x95)
            printf "EC Power      : AC=%d, BAT0=%d, Remaining=%d%%, Cycles=%d\n" \
                $((pstat & 1)) $(((pstat >> 2) & 1)) $(( (brs_hi << 8) | brs_lo )) $(( (cyc_hi << 8) | cyc_lo ))
            query_flexicharger_status
        else
            print_flexicharger_ready
        fi
        ;;
    enable)
        if [[ -n "$EC_FILE" ]]; then
            echo "Error: --ec-file is only supported with 'status'." >&2
            exit 1
        fi
        if (( ${#ARGS[@]} == 1 )); then
            apply_limit_thresholds 60 80
        elif (( ${#ARGS[@]} == 3 )); then
            apply_limit_thresholds "${ARGS[1]}" "${ARGS[2]}"
        else
            echo "Error: 'enable' accepts either 0 arguments (default 60 80) or 2 arguments: <start%> <stop%>." >&2
            usage >&2
            exit 1
        fi
        ;;
    limit)
        if [[ -n "$EC_FILE" ]]; then
            echo "Error: --ec-file is only supported with 'status'." >&2
            exit 1
        fi
        if (( ${#ARGS[@]} != 3 )); then
            echo "Error: 'limit' requires exactly 2 arguments: <start%> <stop%>." >&2
            usage >&2
            exit 1
        fi
        apply_limit_thresholds "${ARGS[1]}" "${ARGS[2]}"
        ;;
    full|disable)
        if [[ -n "$EC_FILE" ]]; then
            echo "Error: --ec-file is only supported with 'status'." >&2
            exit 1
        fi
        if (( ${#ARGS[@]} > 1 )); then
            echo "Error: unexpected extra argument '${ARGS[1]}' for $CMD." >&2
            usage >&2
            exit 1
        fi
        apply_flexicharger 0 80 100
        if (( ! DRY_RUN )); then
            echo "FlexiCharger disabled (full charge enabled)."
        fi
        ;;
    *)
        echo "Error: unknown command '$CMD'." >&2
        usage >&2
        exit 1 ;;
esac
