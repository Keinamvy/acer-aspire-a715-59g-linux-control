#!/bin/bash
# Install, uninstall, and control Acer Aspire A715-59G daemon & tray systemd services.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"

DAEMON_UNIT_NAME="acer-a715-59g-daemon.service"
TRAY_UNIT_NAME="acer-a715-59g-tray.service"
TRAY_DESKTOP_NAME="acer-a715-59g-tray.desktop"
GUI_DESKTOP_NAME="acer-a715-59g-control.desktop"

DAEMON_UNIT_SRC="$SCRIPT_DIR/$DAEMON_UNIT_NAME"
TRAY_UNIT_SRC="$SCRIPT_DIR/$TRAY_UNIT_NAME"
TRAY_DESKTOP_SRC="$SCRIPT_DIR/$TRAY_DESKTOP_NAME"
GUI_DESKTOP_SRC="$SCRIPT_DIR/$GUI_DESKTOP_NAME"

DAEMON_UNIT_DST="/etc/systemd/system/$DAEMON_UNIT_NAME"
DAEMON_SOCK="/run/acer-a715-59g-control.sock"

TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GID="$(id -g "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" 2>/dev/null | cut -d: -f6 || true)"
TARGET_HOME="${TARGET_HOME:-${HOME:-/home/$TARGET_USER}}"
DAEMON_CONFIG_FILE="${ACER_CONTROL_CONFIG:-$TARGET_HOME/.config/acer-a715-59g-control/config.json}"

if (( EUID == 0 )) && [[ -n "${SUDO_USER:-}" ]]; then
    USER_CONFIG_HOME="$TARGET_HOME/.config"
    USER_DATA_HOME="$TARGET_HOME/.local/share"
else
    USER_CONFIG_HOME="${XDG_CONFIG_HOME:-$TARGET_HOME/.config}"
    USER_DATA_HOME="${XDG_DATA_HOME:-$TARGET_HOME/.local/share}"
fi

USER_SYSTEMD_DIR="$USER_CONFIG_HOME/systemd/user"
AUTOSTART_DIR="$USER_CONFIG_HOME/autostart"
APPS_DIR="$USER_DATA_HOME/applications"

TRAY_UNIT_DST="$USER_SYSTEMD_DIR/$TRAY_UNIT_NAME"
TRAY_DESKTOP_DST="$AUTOSTART_DIR/$TRAY_DESKTOP_NAME"
GUI_DESKTOP_DST="$APPS_DIR/$GUI_DESKTOP_NAME"

DRY_RUN=0
CMD=""

usage() {
    cat <<'EOF'
Usage: service.sh [--dry-run] <command>

Commands:
  install        Install user tray service + autostart + desktop launcher AND root daemon service
  install-user   Install & enable/start only user tray service, autostart & desktop launcher (no root)
  uninstall      Stop/disable & remove user tray/desktop files and root daemon service (if installed)
  status         Show daemon unit, socket ping, ec_sys module, tray unit & SNI watcher
  start          Start daemon (if installed) and user tray service
  stop           Stop daemon (if installed) and user tray service
  restart        Restart daemon (if installed) and user tray service
  tray           Start/restart user tray service (or detached tray.py if unit is not installed)

Options:
  --dry-run      Print planned filesystem & systemctl operations without making changes
  -h, --help     Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "Error: unknown option '$1'" >&2
            usage >&2
            exit 1
            ;;
        *)
            if [[ -n "$CMD" ]]; then
                echo "Error: unexpected extra argument '$1'" >&2
                usage >&2
                exit 1
            fi
            CMD="$1"
            shift
            ;;
    esac
done

if [[ -z "$CMD" ]]; then
    usage >&2
    exit 1
fi

run_cmd() {
    if (( DRY_RUN )); then
        printf '[dry-run]'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

run_root() {
    if (( DRY_RUN )); then
        if (( EUID == 0 )); then
            printf '[dry-run]'
        elif command -v sudo >/dev/null 2>&1; then
            printf '[dry-run] sudo'
        elif command -v pkexec >/dev/null 2>&1; then
            printf '[dry-run] pkexec'
        else
            printf '[dry-run] root:'
        fi
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi

    if (( EUID == 0 )); then
        "$@"
    elif [[ -t 0 ]] && command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo "$@"
    elif command -v pkexec >/dev/null 2>&1 && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
        pkexec "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "Error: root privileges required (neither sudo nor pkexec available)." >&2
        exit 1
    fi
}

run_user_systemctl() {
    if (( DRY_RUN )); then
        printf '[dry-run] systemctl --user'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi

    if (( EUID == 0 )) && [[ "$TARGET_UID" != "0" ]]; then
        runuser -u "$TARGET_USER" -- env \
            XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" \
            systemctl --user "$@"
    else
        systemctl --user "$@"
    fi
}

user_unit_present() {
    [[ -f "$TRAY_UNIT_DST" ]] || systemctl --user cat "$TRAY_UNIT_NAME" >/dev/null 2>&1
}

daemon_unit_present() {
    [[ -f "$DAEMON_UNIT_DST" ]] || systemctl cat "$DAEMON_UNIT_NAME" >/dev/null 2>&1
}

install_user_files() {
    for src in "$TRAY_UNIT_SRC" "$TRAY_DESKTOP_SRC" "$GUI_DESKTOP_SRC"; do
        if [[ ! -f "$src" ]]; then
            echo "Error: missing source file: $src" >&2
            exit 1
        fi
    done

    run_cmd mkdir -p "$USER_SYSTEMD_DIR" "$AUTOSTART_DIR" "$APPS_DIR"
    if (( DRY_RUN )); then
        echo "[dry-run] render $TRAY_UNIT_SRC, $TRAY_DESKTOP_SRC, $GUI_DESKTOP_SRC for $SCRIPT_DIR"
        run_cmd install -m 0644 "$TRAY_UNIT_SRC" "$TRAY_UNIT_DST"
        run_cmd install -m 0644 "$TRAY_DESKTOP_SRC" "$TRAY_DESKTOP_DST"
        run_cmd install -m 0644 "$GUI_DESKTOP_SRC" "$GUI_DESKTOP_DST"
    else
        local tmp_tray_unit tmp_tray_desktop tmp_gui_desktop
        tmp_tray_unit="$(mktemp)"
        tmp_tray_desktop="$(mktemp)"
        tmp_gui_desktop="$(mktemp)"
        /usr/bin/python3 - "$TRAY_UNIT_SRC" "$tmp_tray_unit" "$TRAY_DESKTOP_SRC" "$tmp_tray_desktop" "$GUI_DESKTOP_SRC" "$tmp_gui_desktop" "$SCRIPT_DIR" <<'PY'
import pathlib, shlex, sys

tu_src, tu_dst, td_src, td_dst, gd_src, gd_dst, workdir = sys.argv[1:8]

tu_out = []
for ln in pathlib.Path(tu_src).read_text(encoding="utf-8").splitlines(keepends=True):
    if ln.startswith("WorkingDirectory="):
        tu_out.append(f"WorkingDirectory={workdir}\n")
    elif ln.startswith("ExecStart="):
        tu_out.append(f"ExecStart=/usr/bin/python3 {shlex.quote(workdir + '/tray.py')}\n")
    else:
        tu_out.append(ln)
pathlib.Path(tu_dst).write_text("".join(tu_out), encoding="utf-8")

td_out = []
for ln in pathlib.Path(td_src).read_text(encoding="utf-8").splitlines(keepends=True):
    if ln.startswith("Exec="):
        td_out.append(f"Exec=/usr/bin/python3 {workdir}/tray.py\n")
    else:
        td_out.append(ln)
pathlib.Path(td_dst).write_text("".join(td_out), encoding="utf-8")

gd_out = []
for ln in pathlib.Path(gd_src).read_text(encoding="utf-8").splitlines(keepends=True):
    if ln.startswith("Exec="):
        gd_out.append(f"Exec={workdir}/gui.sh\n")
    else:
        gd_out.append(ln)
pathlib.Path(gd_dst).write_text("".join(gd_out), encoding="utf-8")
PY
        run_cmd install -m 0644 "$tmp_tray_unit" "$TRAY_UNIT_DST"
        run_cmd install -m 0644 "$tmp_tray_desktop" "$TRAY_DESKTOP_DST"
        run_cmd install -m 0644 "$tmp_gui_desktop" "$GUI_DESKTOP_DST"
        rm -f "$tmp_tray_unit" "$tmp_tray_desktop" "$tmp_gui_desktop"
    fi

    if (( EUID == 0 )) && [[ "$TARGET_UID" != "0" ]]; then
        run_cmd chown "$TARGET_UID:$TARGET_GID" \
            "$USER_SYSTEMD_DIR" "$AUTOSTART_DIR" "$APPS_DIR" \
            "$TRAY_UNIT_DST" "$TRAY_DESKTOP_DST" "$GUI_DESKTOP_DST"
    fi

    if command -v update-desktop-database >/dev/null 2>&1; then
        if (( DRY_RUN )); then
            run_cmd update-desktop-database "$APPS_DIR"
        else
            update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
        fi
    fi

    run_user_systemctl daemon-reload
    run_user_systemctl enable "$TRAY_UNIT_NAME"
    run_user_systemctl restart "$TRAY_UNIT_NAME"
}

install_daemon_unit() {
    if [[ ! -f "$DAEMON_UNIT_SRC" ]]; then
        echo "Error: missing source unit: $DAEMON_UNIT_SRC" >&2
        exit 1
    fi
    local daemon_cfg="$TARGET_HOME/.config/acer-a715-59g-control/config.json"
    if (( DRY_RUN )); then
        echo "[dry-run] render $DAEMON_UNIT_SRC (WorkingDirectory=$SCRIPT_DIR, --uid $TARGET_UID --gid $TARGET_GID --cache-dir $TARGET_HOME/.cache --config $daemon_cfg)"
        run_root install -m 0644 "$DAEMON_UNIT_SRC" "$DAEMON_UNIT_DST"
    else
        local tmp_unit
        tmp_unit="$(mktemp)"
        /usr/bin/python3 - "$DAEMON_UNIT_SRC" "$tmp_unit" "$SCRIPT_DIR" "$TARGET_UID" "$TARGET_GID" "$TARGET_HOME/.cache" "$daemon_cfg" <<'PY'
import pathlib, shlex, sys

src, dst, workdir, uid, gid, cache_dir, cfg_path = sys.argv[1:8]
lines = pathlib.Path(src).read_text(encoding="utf-8").splitlines(keepends=True)
exec_line = (
    f"ExecStart=/usr/bin/python3 {shlex.quote(workdir + '/daemon.py')} --boot-init "
    f"--socket /run/acer-a715-59g-control.sock --uid {uid} --gid {gid} "
    f"--cache-dir {shlex.quote(cache_dir)} --config {shlex.quote(cfg_path)}\n"
)
out = []
for ln in lines:
    if ln.startswith("WorkingDirectory="):
        out.append(f"WorkingDirectory={workdir}\n")
    elif ln.startswith("ExecStart="):
        out.append(exec_line)
    else:
        out.append(ln)
pathlib.Path(dst).write_text("".join(out), encoding="utf-8")
PY
        run_root install -m 0644 "$tmp_unit" "$DAEMON_UNIT_DST"
        rm -f "$tmp_unit"
    fi
    run_root systemctl daemon-reload
    run_root systemctl enable "$DAEMON_UNIT_NAME"
    run_root systemctl restart "$DAEMON_UNIT_NAME"
}

do_uninstall() {
    if (( DRY_RUN )); then
        run_user_systemctl disable --now "$TRAY_UNIT_NAME"
        run_cmd rm -f "$TRAY_UNIT_DST" "$TRAY_DESKTOP_DST" "$GUI_DESKTOP_DST"
        run_user_systemctl daemon-reload
        if daemon_unit_present; then
            run_root systemctl disable --now "$DAEMON_UNIT_NAME"
            run_root rm -f "$DAEMON_UNIT_DST"
            run_root systemctl daemon-reload
        else
            echo "[dry-run] Root daemon unit ($DAEMON_UNIT_DST) not installed; skipping root cleanup."
        fi
        return 0
    fi

    run_user_systemctl disable --now "$TRAY_UNIT_NAME" >/dev/null 2>&1 || true
    rm -f "$TRAY_UNIT_DST" "$TRAY_DESKTOP_DST" "$GUI_DESKTOP_DST"
    run_user_systemctl daemon-reload >/dev/null 2>&1 || true

    if command -v update-desktop-database >/dev/null 2>&1 && [[ -d "$APPS_DIR" ]]; then
        update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    fi

    if daemon_unit_present; then
        run_root systemctl disable --now "$DAEMON_UNIT_NAME" || true
        run_root rm -f "$DAEMON_UNIT_DST"
        run_root systemctl daemon-reload
    fi
}

do_status() {
    echo "=== System Daemon ($DAEMON_UNIT_NAME) ==="
    if daemon_unit_present; then
        systemctl --no-pager --full status "$DAEMON_UNIT_NAME" 2>&1 || true
    else
        echo "Not installed ($DAEMON_UNIT_DST absent)"
    fi

    echo ""
    echo "=== Daemon UNIX Socket ($DAEMON_SOCK) ==="
    if [[ -S "$DAEMON_SOCK" ]]; then
        ls -l "$DAEMON_SOCK"
        /usr/bin/python3 - "$DAEMON_SOCK" <<'PY' || true
import json, socket, sys
sock_path = sys.argv[1]
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        s.connect(sock_path)
        s.sendall(b'{"id":0,"ping":true}\n')
        line = s.recv(4096).decode("utf-8", errors="replace").strip()
    print(f"Ping response: {line}")
except Exception as exc:
    print(f"Ping failed  : {exc}")
PY
    else
        echo "Socket absent: $DAEMON_SOCK"
    fi

    echo ""
    echo "=== Kernel Modules ==="
    if [[ -e /sys/module/ec_sys ]]; then
        ws="$(cat /sys/module/ec_sys/parameters/write_support 2>/dev/null || echo '?')"
        echo "ec_sys       : loaded (write_support=$ws)"
    else
        echo "ec_sys       : not loaded"
    fi

    echo ""
    echo "=== Persistent Configuration ==="
    /usr/bin/python3 - "$SCRIPT_DIR" "$TARGET_UID" "$TARGET_HOME/.cache" "$DAEMON_CONFIG_FILE" <<'PY' || true
import os, sys
script_dir, uid_s, cache_dir, default_cfg = sys.argv[1:5]
sys.path.insert(0, script_dir)
import config_store
cfg_path = config_store.resolve_config_path(
    explicit=os.environ.get("ACER_CONTROL_CONFIG") or None,
    uid=int(uid_s),
    cache_dir=cache_dir,
)
exists = cfg_path.is_file()
cfg = config_store.load_config(cfg_path, uid=int(uid_s))
print(f"Config Path          : {cfg_path} ({'exists' if exists else 'not created yet; defaults'})")
print(f"auto_apply_on_launch : {cfg.get('auto_apply_on_launch', True)}")
PY

    echo ""
    echo "=== User Tray Service ($TRAY_UNIT_NAME) ==="
    if user_unit_present; then
        run_user_systemctl --no-pager --full status "$TRAY_UNIT_NAME" 2>&1 || true
    else
        echo "Not installed ($TRAY_UNIT_DST absent)"
    fi

    echo ""
    echo "=== Session StatusNotifierWatcher (RegisteredStatusNotifierItems) ==="
    if command -v gdbus >/dev/null 2>&1; then
        gdbus call --session \
            --dest org.kde.StatusNotifierWatcher \
            --object-path /StatusNotifierWatcher \
            --method org.freedesktop.DBus.Properties.Get \
            org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems 2>&1 || true
    elif command -v busctl >/dev/null 2>&1; then
        busctl --user get-property \
            org.kde.StatusNotifierWatcher /StatusNotifierWatcher \
            org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems 2>&1 || true
    else
        echo "Neither gdbus nor busctl found."
    fi
}

do_tray() {
    if user_unit_present; then
        run_user_systemctl restart "$TRAY_UNIT_NAME"
        return 0
    fi

    local runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
    local lock_file="$runtime_dir/acer-a715-59g-tray.lock"
    if (( DRY_RUN )); then
        echo "[dry-run] restart detached /usr/bin/python3 $SCRIPT_DIR/tray.py (lock: $lock_file)"
        return 0
    fi

    if [[ -r "$lock_file" ]]; then
        local old_pid
        old_pid="$(tr -d '[:space:]' < "$lock_file" 2>/dev/null || true)"
        if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
            kill "$old_pid" 2>/dev/null || true
            for _ in $(seq 1 20); do
                kill -0 "$old_pid" 2>/dev/null || break
                sleep 0.1
            done
        fi
    fi

    nohup /usr/bin/python3 "$SCRIPT_DIR/tray.py" </dev/null >/dev/null 2>&1 &
    echo "Started detached tray.py (pid=$!)."
}

case "${CMD,,}" in
    install)
        install_user_files
        install_daemon_unit
        ;;
    install-user)
        install_user_files
        ;;
    uninstall)
        do_uninstall
        ;;
    status)
        do_status
        ;;
    start|stop|restart)
        if daemon_unit_present || (( DRY_RUN )); then
            run_root systemctl "${CMD,,}" "$DAEMON_UNIT_NAME"
        fi
        if user_unit_present || (( DRY_RUN )); then
            run_user_systemctl "${CMD,,}" "$TRAY_UNIT_NAME"
        fi
        ;;
    tray)
        do_tray
        ;;
    *)
        echo "Error: unknown command '$CMD'" >&2
        usage >&2
        exit 1
        ;;
esac
