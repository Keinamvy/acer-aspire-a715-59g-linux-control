#!/bin/bash
# Keyboard Backlight Brightness Control for Acer Aspire A715-59G (Direct ec_sys)
#
# Verified from Aspire A715-59G BIOS V1.16 DSDT & Control Center 3.0 v7.039 (KBType = 6):
#   Write Brightness (DCHU.SCMD 0x67, SubCmd 0xF4):
#     EC Mailbox: 0xF9=FDAT (0x06), 0xFA=FBUF (level 0..255), 0xF8=FCMD (0xCA)
#     Official CC/FnKey steps (KBType 6): 0=off, 1=47(0x2F), 2=95(0x5F), 3=143(0x8F), 4=191(0xBF)
#   Status / State Tracking:
#     On Aspire A715-59G (KBType == 6, 15-color RGB), hardware brightness is write-only;
#     this script tracks applied values in ~/.cache/acer-a715-59g/brightness_state
#     (like Windows AppSettings).

set -euo pipefail

EC_IO="/sys/kernel/debug/ec/ec0/io"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/acer-a715-59g"
STATE_FILE="$CACHE_DIR/brightness_state"
DRY_RUN=0
LEVEL_ARG=""

usage() {
    cat <<'EOF'
Usage: brightness.sh [--dry-run] <status|level|preset>

Commands / Presets (EC FCMD 0xCA, 0xF9=0x06, 0xFA=level, 0xF8=0xCA):
  status            Show cached KBType=6 brightness state (write-only 0xCA sub 0x06)
  off | 0           Turn backlight off (0   / 0x00)
  low | p1          Control Center step 1 (47  / 0x2F)
  med | p2          Control Center step 2 (95  / 0x5F)
  high | p3         Control Center step 3 (143 / 0x8F)
  max | p4          Control Center step 4 (191 / 0xBF)
  full              Maximum raw EC value  (255 / 0xFF)
  <0..255>          Raw brightness byte

Options:
  --dry-run         Print exact EC writes without touching hardware
  -h, --help        Show this help message
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
    local off="$1" val
    val=$(sudo dd if="$EC_IO" bs=1 skip="$((off))" count=1 status=none | od -An -tu1 | tr -d ' \n')
    if [[ ! "$val" =~ ^[0-9]+$ ]]; then
        echo "Error: failed to read EC byte at $(printf '0x%02X' "$((off))")." >&2
        return 1
    fi
    printf '%s' "$val"
}

ec_write_byte() {
    local off="$1" val="$2"
    printf "\\x$(printf '%02x' "$((val))")" | sudo dd of="$EC_IO" bs=1 seek="$((off))" conv=notrunc status=none
}

ec_wait_cmd_done() {
    local i
    for ((i = 0; i < 50; i++)); do
        if [[ "$(ec_read_byte 0xF8)" -eq 0 ]]; then
            return 0
        fi
        sleep 0.02
    done
    echo "Warning: EC FCMD (0xF8) did not return to 0x00 within 1s." >&2
    return 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *)
            if [[ -n "$LEVEL_ARG" ]]; then
                echo "Unexpected extra argument: $1" >&2; usage >&2; exit 1
            fi
            LEVEL_ARG="$1"; shift ;;
    esac
done

LEVEL_ARG="${LEVEL_ARG:-status}"

if [[ "${LEVEL_ARG,,}" == "status" ]]; then
    if [[ -r "$STATE_FILE" ]]; then
        echo "Cached state (KBType=6 RGB): $(<"$STATE_FILE")"
    else
        echo "Cached state (KBType=6 RGB): (none recorded yet)"
    fi
    echo "Mode (KBType=6)            : 0xCA sub 0x06 (0xF9=0x06, 0xFA=level, 0xF8=0xCA; write-only)"
    if (( DRY_RUN )); then
        echo "[dry-run] status uses cached $STATE_FILE only (KBType=6 EC brightness is write-only)"
        exit 0
    fi
    exit 0
fi

case "${LEVEL_ARG,,}" in
    off|0)     LEVEL=0 ;;
    low|p1)    LEVEL=47 ;;
    med|p2)    LEVEL=95 ;;
    high|p3)   LEVEL=143 ;;
    max|p4)    LEVEL=191 ;;
    full)      LEVEL=255 ;;
    *)
        if [[ ! "$LEVEL_ARG" =~ ^[0-9]+$ ]] || (( 10#$LEVEL_ARG < 0 || 10#$LEVEL_ARG > 255 )); then
            echo "Error: brightness level must be 0..255 or a named preset (got '$LEVEL_ARG')." >&2
            exit 1
        fi
        LEVEL=$((10#$LEVEL_ARG))
        ;;
esac

HEX=$(printf '%02X' "$LEVEL")

if (( DRY_RUN )); then
    echo "[dry-run] ec[0xF9]=0x06, ec[0xFA]=0x${HEX}, ec[0xF8]=0xCA -> $EC_IO"
    exit 0
fi

check_ec_sys
ec_write_byte 0xF9 0x06
ec_write_byte 0xFA "$LEVEL"
ec_write_byte 0xF8 0xCA
ec_wait_cmd_done

mkdir -p "$CACHE_DIR"
printf "brightness=%d (0x%s)\n" "$LEVEL" "$HEX" > "$STATE_FILE"
