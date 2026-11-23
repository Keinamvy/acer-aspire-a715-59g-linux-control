#!/bin/bash
# Peripheral Power, Lid/Wake, Clamshell Inhibit & Keyboard Lock Control for Acer Aspire A715-59G
#
# Verified from Aspire A715-59G BIOS V1.16 DSDT:
#   EC Mailbox 0xB8 (DCHU GCMD 0x06/0x09 & SCMD 0x22/0x2A; 0xF8=FCMD, 0xF9=FDAT, 0xFA=FBUF):
#     Query  : 0xF9=0xA1, 0xF8=0xB8 -> wait 0xF8==0 -> read 0xF9: bit0=Webcam (CCD), bit4=Trackpad
#     Webcam : 0xF9=0xC2(ON)|0xC3(OFF), 0xFA=0xA1, 0xF8=0xB8
#     Touchpad: 0xF9=0xC2(ON)|0xC3(OFF), 0xFA=0xA5, 0xF8=0xB8 + EC[0xDB] |= 0x04 + sysfs input*/inhibited
#   Static EC RAM (0xFE0B0100):
#     0x03 : bit0 (LSTE) = Lid State (1=Open, 0=Closed); bit2 (LWKE) = Wake on Lid (1=ON, 0=OFF)
#     0xDB : bit2 = Touchpad hotkey/EC enable flag
#     0xE2 (ECKS, PK04 0x18/0x19): bit4 (0x10) = Win-Key Lock, bit3 (0x08) = Fn/Win Swap
#     0xEC (B2E9, PK04 0x18/0x19): bit5 (0x20) = Fn-Lock
#   Linux interfaces:
#     /dev/video0, /sys/class/input/input*/{name,inhibited} (*ELAN* / *Touchpad*)
#     /proc/acpi/button/lid/LID0/state, /sys/bus/platform/devices/PNP0C0D:00/power/wakeup
#     systemd-inhibit --what=handle-lid-switch (tracked via lid_inhibit.pid)

set -euo pipefail

EC_IO="/sys/kernel/debug/ec/ec0/io"
CACHE_DIR="${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/acer-a715-59g"
STATE_FILE="$CACHE_DIR/peripherals_state"
PID_FILE="$CACHE_DIR/lid_inhibit.pid"
WAKEUP_PATH="/sys/bus/platform/devices/PNP0C0D:00/power/wakeup"
LID_PROC_PATH="/proc/acpi/button/lid/LID0/state"

EC_FILE=""
DRY_RUN=0
ARGS=()
ACTIVE_INHIBIT_PID=""
TP_ALL_PATHS=()
TP_PRIMARY_PATH=""
TP_PRIMARY_INHIBITED="N/A"
RMW_TARGET_BIT=""

usage() {
    cat <<'EOF'
Usage: peripherals.sh [options] <command> [args]

Commands:
  status                            Show webcam, trackpad, lid/wake, lid-close mode, and keyboard lock states
  webcam <on|off|toggle>            Control webcam power via EC 0xB8 (0xFA=0xA1, 0xF9=0xC2|0xC3)
  touchpad <on|off|toggle>          Control trackpad via EC 0xB8 (0xFA=0xA5), EC[0xDB]|=0x04, & input inhibited
  lid-wake <on|off|toggle>          Control Wake-on-Lid via EC[0x03] bit 2 (LWKE) & PNP0C0D:00 power/wakeup
  lid-close <suspend|ignore|toggle> Control lid-close behavior (Suspend vs systemd-inhibit Clamshell Ignore)
  win-lock <on|off|toggle>          Control Win-Key Lock via EC[0xE2] bit 4 (ECKS)
  fn-swap <on|off|toggle>           Control Fn/Win Swap via EC[0xE2] bit 3 (ECKS)
  fn-lock <on|off|toggle>           Control Fn-Lock via EC[0xEC] bit 5 (B2E9)

Options:
  --ec-file <path>                  Read static >=256B EC snapshot file (status & dry-run toggle testing)
  --dry-run                         Print planned EC/sysfs/process actions without modifying state
  -h, --help                        Show this help message
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
    can_read_live_ec || return 1
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
        val=$(dd if="$src" bs=1 skip="$((off))" count=1 status=none | od -An -tu1 | tr -d ' \t\n')
    else
        val=$(sudo dd if="$src" bs=1 skip="$((off))" count=1 status=none | od -An -tu1 | tr -d ' \t\n')
    fi
    if [[ ! "$val" =~ ^[0-9]+$ ]]; then
        echo "Error: failed to read EC byte at offset $(printf '0x%02X' "$((off))") from '$src'." >&2
        return 1
    fi
    printf '%s' "$((10#$val))"
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
    local i val
    for ((i = 0; i < 50; i++)); do
        if val=$(ec_read_byte 0xF8 2>/dev/null) && [[ "$val" =~ ^[0-9]+$ ]] && (( 10#$val == 0 )); then
            return 0
        fi
        sleep 0.02
    done
    echo "Warning: EC FCMD (0xF8) did not clear to 0x00 within timeout." >&2
    return 0
}

read_state_var() {
    local target_key="$1"
    [[ -r "$STATE_FILE" ]] || return 0
    local line key val
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line//$'\r'/}"
        [[ -n "$line" && ! "$line" =~ ^[[:space:]]*# && "$line" == *=* ]] || continue
        key="${line%%=*}"
        val="${line#*=}"
        key="${key//[[:space:]]/}"
        if [[ "$key" == "$target_key" ]]; then
            printf '%s' "$val"
            return 0
        fi
    done < "$STATE_FILE"
}

write_state_var() {
    local target_key="$1" target_val="$2"
    local -a lines=()
    local line
    mkdir -p "$CACHE_DIR"
    if [[ -r "$STATE_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            line="${line//$'\r'/}"
            [[ -n "$line" ]] || continue
            if [[ "${line%%=*}" == "$target_key" ]]; then
                continue
            fi
            lines+=("$line")
        done < "$STATE_FILE"
    fi
    lines+=("${target_key}=${target_val}")
    printf '%s\n' "${lines[@]}" > "$STATE_FILE"
}

collect_touchpad_inhibit_paths() {
    local name_file dev_name inh_file
    local best_elan_tp="" best_elan="" best_tp=""
    TP_ALL_PATHS=()
    TP_PRIMARY_PATH=""
    for name_file in /sys/class/input/input*/name; do
        [[ -r "$name_file" ]] || continue
        dev_name=$(<"$name_file")
        dev_name="${dev_name//$'\r'/}"
        if [[ "$dev_name" == *ELAN* || "$dev_name" == *Touchpad* ]]; then
            inh_file="${name_file%/name}/inhibited"
            [[ -e "$inh_file" ]] || continue
            TP_ALL_PATHS+=("$inh_file")
            if [[ "$dev_name" == *ELAN* && "$dev_name" == *Touchpad* && -z "$best_elan_tp" ]]; then
                best_elan_tp="$inh_file"
            elif [[ "$dev_name" == *ELAN* && -z "$best_elan" ]]; then
                best_elan="$inh_file"
            elif [[ "$dev_name" == *Touchpad* && -z "$best_tp" ]]; then
                best_tp="$inh_file"
            fi
        fi
    done
    TP_PRIMARY_PATH="${best_elan_tp:-${best_elan:-$best_tp}}"
}

refresh_touchpad_info() {
    collect_touchpad_inhibit_paths
    TP_PRIMARY_INHIBITED="N/A"
    if [[ -n "$TP_PRIMARY_PATH" && -r "$TP_PRIMARY_PATH" ]]; then
        local val
        val=$(tr -d '[:space:]' < "$TP_PRIMARY_PATH" 2>/dev/null || true)
        if [[ "$val" == "0" || "$val" == "1" ]]; then
            TP_PRIMARY_INHIBITED="$val"
        fi
    fi
}

read_kernel_wakeup() {
    if [[ -r "$WAKEUP_PATH" ]]; then
        local val
        val=$(tr -d '[:space:]' < "$WAKEUP_PATH" 2>/dev/null || true)
        if [[ "$val" == "enabled" || "$val" == "disabled" ]]; then
            printf '%s' "$val"
            return 0
        fi
    fi
    printf 'N/A'
}

read_proc_lid_state() {
    if [[ -r "$LID_PROC_PATH" ]]; then
        local raw
        raw=$(<"$LID_PROC_PATH")
        raw="${raw,,}"
        if [[ "$raw" == *closed* ]]; then
            printf 'Closed'
            return 0
        elif [[ "$raw" == *open* ]]; then
            printf 'Open'
            return 0
        fi
    fi
    printf 'Open'
}

refresh_lid_inhibit_pid() {
    ACTIVE_INHIBIT_PID=""
    [[ -r "$PID_FILE" ]] || return 0
    local pid
    pid=$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)
    if [[ ! "$pid" =~ ^[0-9]+$ ]] || (( 10#$pid <= 0 )); then
        if (( ! DRY_RUN )); then
            rm -f "$PID_FILE"
        fi
        return 0
    fi
    local alive=0
    if kill -0 "$pid" 2>/dev/null || [[ -r "/proc/$pid/cmdline" ]]; then
        alive=1
    fi
    if (( alive )) && [[ -r "/proc/$pid/cmdline" ]] && grep -qa "systemd-inhibit" "/proc/$pid/cmdline" 2>/dev/null; then
        ACTIVE_INHIBIT_PID="$pid"
        return 0
    fi
    if (( ! DRY_RUN )); then
        rm -f "$PID_FILE"
    fi
}

query_0xb8_a1() {
    ec_write_byte 0xF9 0xA1
    ec_write_byte 0xF8 0xB8
    ec_wait_cmd_done
    ec_read_byte 0xF9
}

write_root_sysfs() {
    local path="$1" value="$2"
    if (( EUID == 0 )) || [[ -w "$path" ]]; then
        printf '%s\n' "$value" > "$path"
    else
        printf '%s\n' "$value" | sudo tee "$path" >/dev/null
    fi
}

require_one_arg() {
    local cmd_name="$1"
    if (( ${#ARGS[@]} != 2 )); then
        echo "Error: '$cmd_name' requires exactly 1 argument." >&2
        usage >&2
        exit 1
    fi
}

parse_on_off_toggle() {
    local cmd_name="$1" raw="${2,,}"
    case "$raw" in
        on|off|toggle) printf '%s' "$raw" ;;
        *)
            echo "Error: '$cmd_name' expects <on|off|toggle> (got '$2')." >&2
            exit 1 ;;
    esac
}

cmd_status() {
    if (( ${#ARGS[@]} > 1 )); then
        echo "Error: unexpected extra argument '${ARGS[1]}' for status." >&2
        usage >&2
        exit 1
    fi
    if [[ -n "$EC_FILE" ]]; then
        validate_ec_file
    elif (( DRY_RUN )); then
        echo "[dry-run] ec[0xF9]=0xA1, ec[0xF8]=0xB8 -> read ec[0xF9]; read ec[0x03], ec[0xE2], ec[0xEC] (using sysfs/proc/cache fallbacks)"
    fi

    local use_static_ec=0 use_live_mbx=0
    if [[ -n "$EC_FILE" ]]; then
        use_static_ec=1
    elif (( ! DRY_RUN )); then
        if can_read_live_ec; then
            use_static_ec=1
        fi
        if can_write_live_ec; then
            use_live_mbx=1
        fi
    fi

    # 1. Webcam (0xB8 bit0) & Trackpad (0xB8 bit4 + input*/inhibited)
    local video0_state="absent"
    if [[ -e /dev/video0 ]]; then
        video0_state="present"
    fi

    local cam_bit0="N/A" tp_bit4="N/A" b8_val=""
    if (( use_live_mbx )); then
        if b8_val=$(query_0xb8_a1 2>/dev/null) && [[ "$b8_val" =~ ^[0-9]+$ ]]; then
            cam_bit0=$(( b8_val & 0x01 ))
            tp_bit4=$(( (b8_val >> 4) & 0x01 ))
        fi
    fi

    local cam_status=""
    if [[ "$cam_bit0" == "1" ]]; then
        cam_status="ON"
    elif [[ "$cam_bit0" == "0" ]]; then
        cam_status="OFF"
    else
        local cached_cam
        cached_cam=$(read_state_var "WEBCAM")
        case "${cached_cam^^}" in
            ON|1) cam_status="ON" ;;
            OFF|0) cam_status="OFF" ;;
            *)
                if [[ "$video0_state" == "present" ]]; then
                    cam_status="ON"
                else
                    cam_status="OFF"
                fi
                ;;
        esac
    fi

    local tp_inhibited tp_status=""
    refresh_touchpad_info
    tp_inhibited="$TP_PRIMARY_INHIBITED"
    if [[ "$tp_inhibited" == "1" ]]; then
        tp_status="OFF"
    elif [[ "$tp_inhibited" == "0" ]]; then
        tp_status="ON"
    elif [[ "$tp_bit4" == "1" ]]; then
        tp_status="ON"
    elif [[ "$tp_bit4" == "0" ]]; then
        tp_status="OFF"
    else
        local cached_tp
        cached_tp=$(read_state_var "TOUCHPAD")
        if [[ "${cached_tp^^}" == "OFF" || "$cached_tp" == "0" ]]; then
            tp_status="OFF"
        else
            tp_status="ON"
        fi
    fi

    # 2. Lid State & Wake on Lid (EC[0x03], /proc lid, PNP0C0D:00 wakeup)
    local ec03="" ec03_disp="N/A" lste="N/A" lwke="N/A"
    local lid_state="" wake_status=""
    local kernel_wakeup
    kernel_wakeup=$(read_kernel_wakeup)

    if (( use_static_ec )) && ec03=$(ec_read_byte 0x03 2>/dev/null); then
        printf -v ec03_disp '0x%02X' "$ec03"
        lste=$(( ec03 & 0x01 ))
        lwke=$(( (ec03 >> 2) & 0x01 ))
        if (( lste == 1 )); then lid_state="Open"; else lid_state="Closed"; fi
        if (( lwke == 1 )); then wake_status="ON"; else wake_status="OFF"; fi
    else
        lid_state=$(read_proc_lid_state)
        if [[ "$kernel_wakeup" == "enabled" ]]; then
            wake_status="ON"
        else
            wake_status="OFF"
        fi
    fi

    # 3. Lid Close Mode (lid_inhibit.pid)
    refresh_lid_inhibit_pid
    local lid_close_mode="Suspend" inhibit_pid_disp="none"
    if [[ -n "$ACTIVE_INHIBIT_PID" ]]; then
        lid_close_mode="Ignore (Clamshell)"
        inhibit_pid_disp="$ACTIVE_INHIBIT_PID"
    fi

    # 4. Keyboard Hardware Locks (EC[0xE2] & EC[0xEC])
    local ec_e2="" e2_disp="N/A" win_status="N/A" win_bit4="N/A" fn_swap_status="N/A" fn_swap_bit3="N/A"
    if (( use_static_ec )) && ec_e2=$(ec_read_byte 0xE2 2>/dev/null); then
        printf -v e2_disp '0x%02X' "$ec_e2"
        win_bit4=$(( (ec_e2 >> 4) & 0x01 ))
        fn_swap_bit3=$(( (ec_e2 >> 3) & 0x01 ))
        if (( win_bit4 == 1 )); then win_status="ON"; else win_status="OFF"; fi
        if (( fn_swap_bit3 == 1 )); then fn_swap_status="ON"; else fn_swap_status="OFF"; fi
    fi

    local ec_ec="" ec_disp="N/A" fn_lock_status="N/A" fn_lock_bit5="N/A"
    if (( use_static_ec )) && ec_ec=$(ec_read_byte 0xEC 2>/dev/null); then
        printf -v ec_disp '0x%02X' "$ec_ec"
        fn_lock_bit5=$(( (ec_ec >> 5) & 0x01 ))
        if (( fn_lock_bit5 == 1 )); then fn_lock_status="ON"; else fn_lock_status="OFF"; fi
    fi

    printf "Webcam (0xB8) : %s (bit0=%s, video0=%s)\n" "$cam_status" "$cam_bit0" "$video0_state"
    printf "Trackpad      : %s (EC bit4=%s, input_inhibited=%s)\n" "$tp_status" "$tp_bit4" "$tp_inhibited"
    printf "Lid State     : %s (EC 0x03=%s, LSTE=%s)\n" "$lid_state" "$ec03_disp" "$lste"
    printf "Wake on Lid   : %s (EC LWKE=%s, kernel_wakeup=%s)\n" "$wake_status" "$lwke" "$kernel_wakeup"
    printf "Lid Close Mode: %s (inhibit_pid=%s)\n" "$lid_close_mode" "$inhibit_pid_disp"
    printf "Win-Key Lock  : %s (EC 0xE2=%s, bit4=%s)\n" "$win_status" "$e2_disp" "$win_bit4"
    printf "Fn/Win Swap   : %s (EC 0xE2=%s, bit3=%s)\n" "$fn_swap_status" "$e2_disp" "$fn_swap_bit3"
    printf "Fn-Lock       : %s (EC 0xEC=%s, bit5=%s)\n" "$fn_lock_status" "$ec_disp" "$fn_lock_bit5"
}

cmd_webcam() {
    require_one_arg "webcam"
    local mode
    mode=$(parse_on_off_toggle "webcam" "${ARGS[1]}")

    local target=""
    if [[ "$mode" == "on" ]]; then
        target="ON"
    elif [[ "$mode" == "off" ]]; then
        target="OFF"
    else
        if (( DRY_RUN )); then
            local cached_cam
            cached_cam=$(read_state_var "WEBCAM")
            case "${cached_cam^^}" in
                ON|1) target="OFF" ;;
                OFF|0) target="ON" ;;
                *)
                    if [[ -e /dev/video0 ]]; then target="OFF"; else target="ON"; fi ;;
            esac
        else
            check_ec_sys
            local b8_val
            b8_val=$(query_0xb8_a1)
            if (( (b8_val & 0x01) == 1 )); then target="OFF"; else target="ON"; fi
        fi
    fi

    local fdat=0xC2
    if [[ "$target" == "OFF" ]]; then fdat=0xC3; fi

    if (( DRY_RUN )); then
        printf "[dry-run] Webcam -> %s: ec[0xF9]=0x%02X, ec[0xFA]=0xA1, ec[0xF8]=0xB8 -> %s\n" \
            "$target" "$((fdat))" "$EC_IO"
        printf "[dry-run] cache WEBCAM=%s -> %s\n" "$target" "$STATE_FILE"
        return 0
    fi

    check_ec_sys
    ec_write_byte 0xF9 "$fdat"
    ec_write_byte 0xFA 0xA1
    ec_write_byte 0xF8 0xB8
    ec_wait_cmd_done
    write_state_var "WEBCAM" "$target"
    printf "Webcam set to %s (0xB8 sub 0xA1, FDAT=0x%02X).\n" "$target" "$((fdat))"
}

cmd_touchpad() {
    require_one_arg "touchpad"
    local mode
    mode=$(parse_on_off_toggle "touchpad" "${ARGS[1]}")

    refresh_touchpad_info
    local inh_cur="$TP_PRIMARY_INHIBITED"

    local target=""
    if [[ "$mode" == "on" ]]; then
        target="ON"
    elif [[ "$mode" == "off" ]]; then
        target="OFF"
    else
        if [[ "$inh_cur" == "1" ]]; then
            target="ON"
        elif [[ "$inh_cur" == "0" ]]; then
            target="OFF"
        elif (( ! DRY_RUN )); then
            check_ec_sys
            local b8_val
            b8_val=$(query_0xb8_a1)
            if (( (b8_val & 0x10) != 0 )); then target="OFF"; else target="ON"; fi
        else
            local cached_tp
            cached_tp=$(read_state_var "TOUCHPAD")
            if [[ "${cached_tp^^}" == "OFF" || "$cached_tp" == "0" ]]; then target="ON"; else target="OFF"; fi
        fi
    fi

    local fdat=0xC2 inh_val=0
    if [[ "$target" == "OFF" ]]; then
        fdat=0xC3
        inh_val=1
    fi

    if (( DRY_RUN )); then
        printf "[dry-run] Trackpad -> %s: ec[0xF9]=0x%02X, ec[0xFA]=0xA5, ec[0xF8]=0xB8 -> %s\n" \
            "$target" "$((fdat))" "$EC_IO"
        if [[ -n "$EC_FILE" ]]; then
            local db_cur
            db_cur=$(ec_read_byte 0xDB)
            if (( (db_cur & 0x04) == 0 )); then
                printf "[dry-run] ec[0xDB]: 0x%02X -> 0x%02X (|= 0x04) -> %s\n" "$db_cur" "$((db_cur | 0x04))" "$EC_IO"
            else
                printf "[dry-run] ec[0xDB]: 0x%02X (bit 2 already 1; unchanged)\n" "$db_cur"
            fi
        else
            printf "[dry-run] ec[0xDB] |= 0x04 (only when bit 2 is 0) -> %s\n" "$EC_IO"
        fi
        if (( ${#TP_ALL_PATHS[@]} > 0 )); then
            printf "[dry-run] write '%d' -> %s\n" "$inh_val" "${TP_ALL_PATHS[*]}"
        else
            echo "[dry-run] no matching /sys/class/input/input*/inhibited (*ELAN*/*Touchpad*) found"
        fi
        printf "[dry-run] cache TOUCHPAD=%s -> %s\n" "$target" "$STATE_FILE"
        return 0
    fi

    check_ec_sys
    ec_write_byte 0xF9 "$fdat"
    ec_write_byte 0xFA 0xA5
    ec_write_byte 0xF8 0xB8
    ec_wait_cmd_done

    local db_val
    db_val=$(ec_read_byte 0xDB)
    if (( (db_val & 0x04) == 0 )); then
        ec_write_byte 0xDB $(( db_val | 0x04 ))
    fi

    local p
    if (( ${#TP_ALL_PATHS[@]} > 0 )); then
        for p in "${TP_ALL_PATHS[@]}"; do
            write_root_sysfs "$p" "$inh_val"
        done
    fi
    write_state_var "TOUCHPAD" "$target"
    printf "Trackpad set to %s (0xB8 sub 0xA5 = 0x%02X, inhibited=%d on %d device(s)).\n" \
        "$target" "$((fdat))" "$inh_val" "${#TP_ALL_PATHS[@]}"
}

rmw_ec_bit() {
    local label="$1" reg_off="$2" bit_pos="$3" mode="$4"
    local mask=$(( 1 << bit_pos ))
    local inv_mask=$(( (~mask) & 0xFF ))
    RMW_TARGET_BIT=""

    if (( DRY_RUN )); then
        if [[ -n "$EC_FILE" ]]; then
            local cur cur_bit target_bit new_val state_str
            cur=$(ec_read_byte "$reg_off")
            cur_bit=$(( (cur >> bit_pos) & 1 ))
            case "$mode" in
                on)     target_bit=1 ;;
                off)    target_bit=0 ;;
                toggle) target_bit=$(( cur_bit ^ 1 )) ;;
            esac
            RMW_TARGET_BIT="$target_bit"
            if (( target_bit == 1 )); then
                new_val=$(( cur | mask ))
                state_str="ON"
            else
                new_val=$(( cur & inv_mask ))
                state_str="OFF"
            fi
            printf "[dry-run] %s -> %s: ec[0x%02X] 0x%02X -> 0x%02X (bit%d=%d) -> %s\n" \
                "$label" "$state_str" "$((reg_off))" "$cur" "$new_val" "$bit_pos" "$target_bit" "$EC_IO"
        else
            case "$mode" in
                on)
                    RMW_TARGET_BIT=1
                    printf "[dry-run] %s -> ON: ec[0x%02X] |= 0x%02X (bit%d=1) -> %s\n" \
                        "$label" "$((reg_off))" "$mask" "$bit_pos" "$EC_IO" ;;
                off)
                    RMW_TARGET_BIT=0
                    printf "[dry-run] %s -> OFF: ec[0x%02X] &= 0x%02X (bit%d=0) -> %s\n" \
                        "$label" "$((reg_off))" "$inv_mask" "$bit_pos" "$EC_IO" ;;
                toggle)
                    printf "[dry-run] %s toggle: ec[0x%02X] ^= 0x%02X (flip bit%d) -> %s\n" \
                        "$label" "$((reg_off))" "$mask" "$bit_pos" "$EC_IO" ;;
            esac
        fi
        return 0
    fi

    check_ec_sys
    local cur cur_bit target_bit new_val state_str
    cur=$(ec_read_byte "$reg_off")
    cur_bit=$(( (cur >> bit_pos) & 1 ))
    case "$mode" in
        on)     target_bit=1 ;;
        off)    target_bit=0 ;;
        toggle) target_bit=$(( cur_bit ^ 1 )) ;;
    esac
    RMW_TARGET_BIT="$target_bit"
    if (( target_bit == 1 )); then
        new_val=$(( cur | mask ))
        state_str="ON"
    else
        new_val=$(( cur & inv_mask ))
        state_str="OFF"
    fi
    ec_write_byte "$reg_off" "$new_val"
    printf "%s set to %s (EC 0x%02X: 0x%02X -> 0x%02X, bit%d=%d).\n" \
        "$label" "$state_str" "$((reg_off))" "$cur" "$new_val" "$bit_pos" "$target_bit"
}

cmd_lid_wake() {
    require_one_arg "lid-wake"
    local mode resolved_mode
    mode=$(parse_on_off_toggle "lid-wake" "${ARGS[1]}")
    resolved_mode="$mode"

    if (( DRY_RUN )) && [[ "$mode" == "toggle" && -z "$EC_FILE" ]]; then
        local kw
        kw=$(read_kernel_wakeup)
        if [[ "$kw" == "enabled" ]]; then
            resolved_mode="off"
        elif [[ "$kw" == "disabled" ]]; then
            resolved_mode="on"
        fi
    fi

    rmw_ec_bit "Wake on Lid" 0x03 2 "$resolved_mode"

    local wakeup_str=""
    if [[ "$RMW_TARGET_BIT" == "1" ]]; then
        wakeup_str="enabled"
    elif [[ "$RMW_TARGET_BIT" == "0" ]]; then
        wakeup_str="disabled"
    fi

    if (( DRY_RUN )); then
        if [[ -n "$wakeup_str" ]]; then
            printf "[dry-run] write '%s' -> %s (if present/writable)\n" "$wakeup_str" "$WAKEUP_PATH"
        else
            printf "[dry-run] write 'enabled|disabled' -> %s (matching flipped LWKE)\n" "$WAKEUP_PATH"
        fi
        return 0
    fi

    if [[ -n "$wakeup_str" && -e "$WAKEUP_PATH" ]]; then
        write_root_sysfs "$WAKEUP_PATH" "$wakeup_str" 2>/dev/null || true
    fi
    printf "Kernel wakeup (%s): %s\n" "$WAKEUP_PATH" "$(read_kernel_wakeup)"
}

cmd_lid_close() {
    require_one_arg "lid-close"
    local raw="${ARGS[1],,}" mode=""
    case "$raw" in
        suspend|ignore|toggle) mode="$raw" ;;
        *)
            echo "Error: 'lid-close' expects <suspend|ignore|toggle> (got '${ARGS[1]}')." >&2
            exit 1 ;;
    esac

    refresh_lid_inhibit_pid
    if [[ "$mode" == "toggle" ]]; then
        if [[ -n "$ACTIVE_INHIBIT_PID" ]]; then
            mode="suspend"
        else
            mode="ignore"
        fi
    fi

    if [[ "$mode" == "ignore" ]]; then
        if (( DRY_RUN )); then
            if [[ -n "$ACTIVE_INHIBIT_PID" ]]; then
                printf "[dry-run] Lid Close Mode already Ignore (Clamshell): systemd-inhibit active (pid=%s)\n" "$ACTIVE_INHIBIT_PID"
            else
                echo '[dry-run] setsid systemd-inhibit --what=handle-lid-switch --who="Acer A715-59G Control" --why="Clamshell/Ignore Lid Close Mode" sleep infinity </dev/null >/dev/null 2>&1 &'
                printf "[dry-run] record PID -> %s\n" "$PID_FILE"
            fi
            return 0
        fi
        if [[ -n "$ACTIVE_INHIBIT_PID" ]]; then
            printf "Lid Close Mode already set to Ignore (Clamshell) (inhibit_pid=%s).\n" "$ACTIVE_INHIBIT_PID"
            return 0
        fi
        command -v systemd-inhibit >/dev/null 2>&1 || { echo "Error: systemd-inhibit not found." >&2; exit 1; }
        command -v setsid >/dev/null 2>&1 || { echo "Error: setsid not found." >&2; exit 1; }

        mkdir -p "$CACHE_DIR"
        setsid systemd-inhibit \
            --what=handle-lid-switch \
            --who="Acer A715-59G Control" \
            --why="Clamshell/Ignore Lid Close Mode" \
            sleep infinity </dev/null >/dev/null 2>&1 &
        local new_pid=$!

        local i
        for ((i = 0; i < 15; i++)); do
            if ! kill -0 "$new_pid" 2>/dev/null; then
                wait "$new_pid" 2>/dev/null || true
                echo "Error: systemd-inhibit exited immediately." >&2
                exit 1
            fi
            if [[ -r "/proc/$new_pid/cmdline" ]] && grep -qa "systemd-inhibit" "/proc/$new_pid/cmdline" 2>/dev/null; then
                break
            fi
            sleep 0.02
        done
        printf '%s\n' "$new_pid" > "$PID_FILE"
        printf "Lid Close Mode set to Ignore (Clamshell) (inhibit_pid=%s).\n" "$new_pid"
        return 0
    fi

    # mode == suspend
    if (( DRY_RUN )); then
        if [[ -n "$ACTIVE_INHIBIT_PID" ]]; then
            printf "[dry-run] kill %s (systemd-inhibit) and remove %s\n" "$ACTIVE_INHIBIT_PID" "$PID_FILE"
        else
            printf "[dry-run] no active systemd-inhibit; remove %s if present\n" "$PID_FILE"
        fi
        return 0
    fi

    if [[ -n "$ACTIVE_INHIBIT_PID" ]]; then
        if kill -0 "$ACTIVE_INHIBIT_PID" 2>/dev/null; then
            kill "$ACTIVE_INHIBIT_PID" 2>/dev/null || true
        else
            sudo kill "$ACTIVE_INHIBIT_PID" 2>/dev/null || true
        fi
        local i
        for ((i = 0; i < 25; i++)); do
            if ! kill -0 "$ACTIVE_INHIBIT_PID" 2>/dev/null && [[ ! -e "/proc/$ACTIVE_INHIBIT_PID" ]]; then
                break
            fi
            sleep 0.02
        done
        rm -f "$PID_FILE"
        printf "Lid Close Mode set to Suspend (stopped inhibit_pid=%s).\n" "$ACTIVE_INHIBIT_PID"
    else
        rm -f "$PID_FILE"
        echo "Lid Close Mode set to Suspend (no active inhibit process)."
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --ec-file)
            [[ $# -ge 2 && -n "$2" ]] || { echo "Error: --ec-file requires a non-empty path." >&2; exit 1; }
            EC_FILE="$2"; shift 2 ;;
        --ec-file=*)
            EC_FILE="${1#--ec-file=}"
            [[ -n "$EC_FILE" ]] || { echo "Error: --ec-file requires a non-empty path." >&2; exit 1; }
            shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; ARGS+=("$@"); break ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

CMD="${ARGS[0]:-status}"

if [[ -n "$EC_FILE" ]]; then
    if [[ "${CMD,,}" != "status" ]] && (( ! DRY_RUN )); then
        echo "Error: --ec-file is only supported with 'status' or '--dry-run'." >&2
        exit 1
    fi
    validate_ec_file
fi

case "${CMD,,}" in
    status)     cmd_status ;;
    webcam)     cmd_webcam ;;
    touchpad)   cmd_touchpad ;;
    lid-wake)   cmd_lid_wake ;;
    lid-close)  cmd_lid_close ;;
    win-lock)
        require_one_arg "win-lock"
        rmw_ec_bit "Win-Key Lock" 0xE2 4 "$(parse_on_off_toggle "win-lock" "${ARGS[1]}")"
        ;;
    fn-swap)
        require_one_arg "fn-swap"
        rmw_ec_bit "Fn/Win Swap" 0xE2 3 "$(parse_on_off_toggle "fn-swap" "${ARGS[1]}")"
        ;;
    fn-lock)
        require_one_arg "fn-lock"
        rmw_ec_bit "Fn-Lock" 0xEC 5 "$(parse_on_off_toggle "fn-lock" "${ARGS[1]}")"
        ;;
    *)
        echo "Error: unknown command '$CMD'." >&2
        usage >&2
        exit 1 ;;
esac
