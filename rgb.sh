#!/bin/bash
# Keyboard RGB Color, Effects & Sleep Timer Control for Acer Aspire A715-59G (Direct ec_sys)
#
# Verified from Aspire A715-59G BIOS V1.16 DSDT & Control Center 3.0 v7.039 (KBType = 6, 1-zone):
#   Static RGB (FCMD=0xCA, DCHU.SCMD 0x67 Sub 0xF0):
#     0xF9=0x03 (KBType=6 1-zone global target), 0xFA=B, 0xFB=R, 0xFC=G, 0xFD=0x00, 0xF8=0xCA
#     (DSDT ECMD count=5 writes 0xF9..0xFC only; 0xFD stays 0x00, NOT 0xFF.)
#     Channel scaling: Direct EC writes take raw 0..255 (matching LedKeyboardSetting/CC/RGBKB.cs,
#     where (0,255,127) clamps B=70); --cc-scale applies FnKey/CC40 KBType=6 halving (R/2,G/2,B/2
#     after (0,254,129)/(0,255,127)->B=70).
#   KBType=6 Breathing (FCMD=0xCA Sub 0x0F):
#     0xF9=0x0F, 0xFA=0x07, 0xFB=0x10, 0xFC=0x00, 0xFD=0x00, 0xF8=0xCA
#     (Applying a static color via 0xCA 0xF9=0x03 restores static mode.)
#   Sleep Timer (FCMD=0xD4, DCHU.SCMD 0x79 Sub 0x18):
#     0xF9=0xFF|0x00, 0xFA=sec_hi, 0xFB=sec_lo, 0xF8=0xD4

set -euo pipefail

EC_IO="/sys/kernel/debug/ec/ec0/io"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/acer-a715-59g"
STATE_FILE="$CACHE_DIR/rgb_state"
DRY_RUN=0
CC_SCALE=0
EFFECT=""
TIMEOUT=""
ARGS=()

usage() {
    cat <<'EOF'
Usage:
  rgb.sh [--dry-run] status
  rgb.sh [--dry-run] [--cc-scale] <R> <G> <B>
  rgb.sh [--dry-run] [--cc-scale] <preset>
  rgb.sh [--dry-run] --effect <name>
  rgb.sh [--dry-run] --timeout <seconds|off>

Presets (from Control Center palette; direct 0..255 by default):
  blue (0 0 255)       green (0 255 0)     red (255 0 0)
  cyan (0 255 200)     skyblue (0 255 229) yellow (255 255 0)
  orange (255 127 0)   purple (162 0 255)  pink (255 0 210)
  white (255 255 255)  off (0 0 0)

Target (EC FCMD 0xCA, KBType=6 1-zone):
  0xF9=0x03 (global 1-zone), 0xFA=B, 0xFB=R, 0xFC=G, 0xFD=0x00, 0xF8=0xCA

Effects (--effect, KBType=6):
  breath             15-color breathing via 0xCA sub 0x0F (0xF9=0x0F, 0xFA=0x07, 0xFB=0x10)

Sleep Timer (--timeout via EC FCMD 0xD4):
  off | 0              Disable keyboard backlight sleep timeout
  <5..65535>           Turn backlight off after N seconds of inactivity

Options:
  --cc-scale           Apply FnKey/CC40 KBType=6 halving (R/2, G/2, B/2; (0,254,129)/(0,255,127)->B=35)
  --dry-run            Print exact EC writes without touching hardware
  -h, --help           Show this help message
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

parse_byte() {
    local outvar="$1" name="$2" val="$3"
    if [[ ! "$val" =~ ^[0-9]+$ ]] || (( 10#$val < 0 || 10#$val > 255 )); then
        echo "Error: $name must be an integer 0..255 (got '$val')." >&2
        exit 1
    fi
    printf -v "$outvar" '%d' "$((10#$val))"
}

save_state() {
    mkdir -p "$CACHE_DIR"
    echo "$1" > "$STATE_FILE"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --cc-scale) CC_SCALE=1; shift ;;
        --effect)
            [[ $# -ge 2 ]] || { echo "Error: --effect requires an argument." >&2; exit 1; }
            EFFECT="$2"; shift 2 ;;
        --timeout)
            [[ $# -ge 2 ]] || { echo "Error: --timeout requires an argument." >&2; exit 1; }
            TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

if [[ -n "$EFFECT" && -n "$TIMEOUT" ]]; then
    echo "Error: --effect and --timeout cannot be combined." >&2
    exit 1
fi

if [[ -n "$EFFECT" ]]; then
    if (( CC_SCALE )) || [[ ${#ARGS[@]} -gt 0 ]]; then
        echo "Error: --effect does not take --cc-scale or positional arguments." >&2
        exit 1
    fi
    case "${EFFECT,,}" in
        breath)
            ;;
        *)
            echo "Error: unknown effect '$EFFECT' (only 'breath' is supported on KBType=6)." >&2
            exit 1 ;;
    esac
    if (( DRY_RUN )); then
        echo "[dry-run] ec[0xF9]=0x0F, ec[0xFA]=0x07, ec[0xFB]=0x10, ec[0xFC]=0x00, ec[0xFD]=0x00, ec[0xF8]=0xCA -> $EC_IO (KBType=6 15-color breathing via 0xCA sub 0x0F)"
        exit 0
    fi
    check_ec_sys
    ec_write_byte 0xF9 0x0F
    ec_write_byte 0xFA 0x07
    ec_write_byte 0xFB 0x10
    ec_write_byte 0xFC 0x00
    ec_write_byte 0xFD 0x00
    ec_write_byte 0xF8 0xCA
    ec_wait_cmd_done
    save_state "effect=breath"
    exit 0
fi

if [[ -n "$TIMEOUT" ]]; then
    if (( CC_SCALE )) || [[ ${#ARGS[@]} -gt 0 ]]; then
        echo "Error: --timeout does not take --cc-scale or positional arguments." >&2
        exit 1
    fi
    if [[ "${TIMEOUT,,}" == "off" || "$TIMEOUT" == "0" ]]; then
        EN=0x00; HI=0; LO=0
    else
        if [[ ! "$TIMEOUT" =~ ^[0-9]+$ ]] || (( 10#$TIMEOUT < 5 || 10#$TIMEOUT > 65535 )); then
            echo "Error: timeout must be 'off', 0, or 5..65535 seconds (got '$TIMEOUT')." >&2
            exit 1
        fi
        TIMEOUT=$((10#$TIMEOUT))
        EN=0xFF
        HI=$(( (TIMEOUT >> 8) & 0xFF ))
        LO=$(( TIMEOUT & 0xFF ))
    fi
    if (( DRY_RUN )); then
        printf "[dry-run] ec[0xF9]=0x%02X, ec[0xFA]=0x%02X (sec_hi), ec[0xFB]=0x%02X (sec_lo), ec[0xF8]=0xD4 -> %s\n" \
            $((EN)) "$HI" "$LO" "$EC_IO"
        exit 0
    fi
    check_ec_sys
    ec_write_byte 0xF9 $((EN))
    ec_write_byte 0xFA "$HI"
    ec_write_byte 0xFB "$LO"
    ec_write_byte 0xF8 0xD4
    ec_wait_cmd_done
    save_state "timeout=$TIMEOUT"
    exit 0
fi

if [[ ${#ARGS[@]} -eq 0 ]]; then
    ARGS=("status")
fi

if [[ "${ARGS[0],,}" == "status" ]]; then
    if (( CC_SCALE )) || [[ ${#ARGS[@]} -gt 1 ]]; then
        echo "Error: status does not take --cc-scale or extra arguments." >&2
        exit 1
    fi
    echo "Target mode   : KBType=6 1-zone (0xF9=0x03)"
    if [[ -r "$STATE_FILE" ]]; then
        echo "Cached state  : $(<"$STATE_FILE")"
    else
        echo "Cached state  : (none recorded yet)"
    fi
    if (( DRY_RUN )); then
        echo "[dry-run] read ec[0xF8..0xFD] (FCMD..FBF3) from $EC_IO"
        exit 0
    fi
    check_ec_sys
    printf "EC Mailbox    : "
    sudo dd if="$EC_IO" bs=1 skip=$((0xF8)) count=6 status=none | od -An -tx1 | sed 's/^ //'
    exit 0
fi

IS_PRESET=1
case "${ARGS[0],,}" in
    blue)    R=0;   G=0;   B=255 ;;
    green)   R=0;   G=255; B=0 ;;
    red)     R=255; G=0;   B=0 ;;
    cyan)    R=0;   G=255; B=200 ;;
    skyblue) R=0;   G=255; B=229 ;;
    yellow)  R=255; G=255; B=0 ;;
    orange)  R=255; G=127; B=0 ;;
    purple)  R=162; G=0;   B=255 ;;
    pink)    R=255; G=0;   B=210 ;;
    white)   R=255; G=255; B=255 ;;
    off)     R=0;   G=0;   B=0 ;;
    *)
        IS_PRESET=0
        if [[ ${#ARGS[@]} -ne 3 ]]; then
            echo "Error: expected '<R> <G> <B>', '<preset>', or 'status' (KBType=6 1-zone takes no zone argument; got ${#ARGS[@]} arguments)." >&2
            usage >&2
            exit 1
        fi
        R="${ARGS[0]}"
        G="${ARGS[1]}"
        B="${ARGS[2]}"
        ;;
esac

if (( IS_PRESET )); then
    if [[ ${#ARGS[@]} -ne 1 ]]; then
        echo "Error: expected '<preset>' with no zone argument (got ${#ARGS[@]} arguments)." >&2
        usage >&2
        exit 1
    fi
fi

parse_byte R "R" "$R"
parse_byte G "G" "$G"
parse_byte B "B" "$B"

RAW_R=$R; RAW_G=$G; RAW_B=$B
if (( CC_SCALE )); then
    # FnKey/RGBKB.cs & CC40.LEDdevice/RGBKB.cs KBType=6 scaling
    if (( (R == 0 && G == 254 && B == 129) || (R == 0 && G == 255 && B == 127) )); then
        B=70
    fi
    R=$(( R / 2 ))
    G=$(( G / 2 ))
    B=$(( B / 2 ))
fi

if (( DRY_RUN )); then
    printf "[dry-run] ec[0xF9]=0x03 (KBType=6 1-zone), ec[0xFA]=0x%02X (B), ec[0xFB]=0x%02X (R), ec[0xFC]=0x%02X (G), ec[0xFD]=0x00, ec[0xF8]=0xCA -> %s" \
        "$B" "$R" "$G" "$EC_IO"
    if (( CC_SCALE )); then
        printf " [cc-scale from (%d,%d,%d)]" "$RAW_R" "$RAW_G" "$RAW_B"
    fi
    echo
    exit 0
fi

check_ec_sys
ec_write_byte 0xF9 0x03
ec_write_byte 0xFA "$B"
ec_write_byte 0xFB "$R"
ec_write_byte 0xFC "$G"
ec_write_byte 0xFD 0x00
ec_write_byte 0xF8 0xCA
ec_wait_cmd_done

if (( CC_SCALE )); then
    save_state "color=($RAW_R,$RAW_G,$RAW_B) ec=($R,$G,$B) cc_scale=1"
else
    save_state "color=($R,$G,$B)"
fi
