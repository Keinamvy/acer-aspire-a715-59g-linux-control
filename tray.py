#!/usr/bin/env python3
"""PyQt5 StatusNotifierItem system tray indicator for Acer Aspire A715-59G."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Iterator, Optional

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def _configure_headless_qt_if_needed(argv: list[str]) -> None:
    """Use Qt offscreen plugin ONLY for --self-test when no display server is set."""
    if "--self-test" in argv and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


_configure_headless_qt_if_needed(sys.argv[1:])

from PyQt5.QtCore import QObject, QRectF, QSocketNotifier, QTimer, Qt, pyqtSignal, pyqtSlot  # noqa: E402
from PyQt5.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon  # noqa: E402

import config_store  # noqa: E402
import gui  # noqa: E402

LOCK_FILENAME = "acer-a715-59g-tray.lock"

# Profile palette: (top_hex, bottom_hex, border_hex, display_name)
PROFILE_BADGE_STYLES: dict[Optional[str], tuple[str, str, str, str]] = {
    "quiet": ("#0891b2", "#0e7490", "#67e8f9", "Quiet"),
    "powersaving": ("#16a34a", "#15803d", "#86efac", "Power Saving"),
    "entertainment": ("#2563eb", "#1d4ed8", "#93c5fd", "Entertainment"),
    "performance": ("#dc2626", "#b91c1c", "#fca5a5", "Performance"),
    None: ("#475569", "#334155", "#94a3b8", "Unknown"),
}

POWER_PROFILE_MENU_LABELS: dict[str, str] = {
    "quiet": "Quiet (15/30W • EC Silent Auto Fan • 25W dGPU)",
    "powersaving": "Power Saving (15/30W • EC Eco Auto Fan • dGPU Off/D3cold)",
    "entertainment": "Entertainment (45/115W • 60W dGPU)",
    "performance": "Performance (90/115W • 75W dGPU)",
}

TRAY_BRIGHTNESS_PRESETS: tuple[tuple[int, str], ...] = (
    (0, "Off (0)"),
    (47, "Low (47)"),
    (95, "Medium (95)"),
    (143, "High (143)"),
    (191, "Max (191)"),
    (255, "Full (255)"),
)

TRAY_RGB_COLOR_KEYS: tuple[tuple[str, str], ...] = (
    ("cyan", "Cyan"),
    ("blue", "Blue"),
    ("red", "Red"),
    ("green", "Green"),
    ("purple", "Purple"),
    ("orange", "Orange"),
    ("white", "White"),
)

TRAY_BATTERY_PRESETS: tuple[tuple[str, str, list[str]], ...] = (
    ("conservation", "Conservation (60%–80%)", ["limit", "60", "80"]),
    ("balanced", "Balanced (70%–85%)", ["limit", "70", "85"]),
    ("full", "Full Charge (100%)", ["full"]),
)

PERIPHERAL_TOGGLES: tuple[tuple[str, str, str, str, str], ...] = (
    ("webcam", "Webcam Power (USB)", "webcam", "on", "off"),
    ("touchpad", "Trackpad", "touchpad", "on", "off"),
    ("lid_wake", "Wake on Lid Open", "lid-wake", "on", "off"),
    ("lid_close", "Lid Close: Ignore (Clamshell)", "lid-close", "ignore", "suspend"),
    ("win_lock", "Win-Key Lock", "win-lock", "on", "off"),
    ("fn_lock", "Fn-Lock", "fn-lock", "on", "off"),
)


def acquire_single_instance_lock() -> Optional[int]:
    """Acquire non-blocking flock for normal tray mode; return fd or None if active."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    lock_path = os.path.join(runtime_dir, LOCK_FILENAME)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    return fd


def render_tray_icon(
    profile_key: Optional[str] = None,
    cpu_temp: Optional[int] = None,
    live_mode: bool = False,
    size: int = 64,
) -> QIcon:
    """Render a 64x64 rounded badge icon color-coded by profile with CPU temp and live dot."""
    top_hex, bot_hex, border_hex, _label = PROFILE_BADGE_STYLES.get(
        profile_key, PROFILE_BADGE_STYLES[None]
    )

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)

    margin = 3.0
    badge_rect = QRectF(margin, margin, size - 2.0 * margin, size - 2.0 * margin)
    radius = size * 0.22

    grad = QLinearGradient(badge_rect.topLeft(), badge_rect.bottomRight())
    grad.setColorAt(0.0, QColor(top_hex))
    grad.setColorAt(1.0, QColor(bot_hex))
    painter.setBrush(grad)
    painter.setPen(QPen(QColor(border_hex), 2.5))
    painter.drawRoundedRect(badge_rect, radius, radius)

    temp_text = f"{cpu_temp}°" if cpu_temp is not None else "--°"
    font = QFont("Sans Serif")
    font.setBold(True)
    font.setPixelSize(int(size * (0.36 if len(temp_text) <= 3 else 0.30)))
    painter.setFont(font)

    text_rect = QRectF(2.0, 2.0, size - 4.0, size - 4.0)
    painter.setPen(QColor(10, 15, 25, 190))
    painter.drawText(text_rect.translated(0.0, 1.5), Qt.AlignCenter, temp_text)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(text_rect, Qt.AlignCenter, temp_text)

    # Subtle status indicator dot (top-right): orange/amber for Live EC, gray for Snapshot/Locked
    dot_diameter = size * 0.20
    dot_x = size - margin - dot_diameter - 2.0
    dot_y = margin + 2.0
    dot_color = QColor("#fb923c") if live_mode else QColor("#94a3b8")
    painter.setBrush(dot_color)
    painter.setPen(QPen(QColor("#0f172a"), 2.0))
    painter.drawEllipse(QRectF(dot_x, dot_y, dot_diameter, dot_diameter))

    painter.end()
    return QIcon(pixmap)


def _set_action_checked(action: QAction, checked: bool) -> None:
    """Update a checkable QAction in-place without emitting triggered/toggled side effects."""
    prev = action.blockSignals(True)
    try:
        action.setChecked(checked)
    finally:
        action.blockSignals(prev)


def _format_profile_label(power: dict[str, Any], mode_label: str) -> str:
    key = power.get("profile_key")
    if key is not None and key in PROFILE_BADGE_STYLES:
        return PROFILE_BADGE_STYLES[key][3]
    if mode_label == "dry-run" or power.get("dry_run"):
        return "Dry-Run"
    text = str(power.get("profile_text") or "N/A")
    if "snapshot" in text.lower():
        return "Snapshot"
    return text


def _format_gpu_tgp_label(power: dict[str, Any]) -> str:
    pm_state = power.get("gpu_pm_state")
    if pm_state == "d3cold":
        return "Disabled (D3cold)"
    if pm_state == "d3cold_pending":
        return "RTD3 Auto (D3cold Pending)"
    tier_lbl = power.get("gpu_tier_label")
    if tier_lbl:
        return str(tier_lbl)
    cur_txt = str(power.get("gpu_power_cur_text") or "N/A")
    if cur_txt not in ("N/A", "[N/A]", "n/a"):
        return cur_txt
    def_txt = str(power.get("gpu_power_default_text") or "N/A")
    if def_txt not in ("N/A", "[N/A]", "n/a"):
        return f"Default {def_txt}"
    return "N/A"


def _format_oem_pl_label(power: dict[str, Any], profile_key: Optional[str]) -> Optional[str]:
    oem_table = power.get("oem_table_profiles") or {}
    ec_pl1 = power.get("ec_pl1_w")
    ec_pl2 = power.get("ec_pl2_w")
    if profile_key and profile_key in oem_table:
        pl1_w, pl2_w = oem_table[profile_key]
        if ec_pl1 is not None and ec_pl2 is not None:
            return f"OEM {pl1_w:g}W/{pl2_w:g}W (EC {ec_pl1:g}W/{ec_pl2:g}W)"
        return f"OEM {pl1_w:g}W/{pl2_w:g}W"
    if ec_pl1 is not None and ec_pl2 is not None:
        return f"EC {ec_pl1:g}W/{ec_pl2:g}W"
    msr_pl1 = power.get("rapl_msr_pl1")
    msr_pl2 = power.get("rapl_msr_pl2")
    if msr_pl1 is not None and msr_pl2 is not None:
        return f"RAPL {msr_pl1:g}W/{msr_pl2:g}W"
    return None


def _format_flexicharger_note(battery: dict[str, Any]) -> str:
    """Format FlexiCharger status from Direct EC 0xCB / XRAM 0x06D0..0x06D2 live readback."""
    bat_enabled = battery.get("f1_enabled")
    bat_start = battery.get("f1_start")
    bat_stop = battery.get("f1_stop")
    if bat_enabled is True and bat_start is not None and bat_stop is not None:
        return f"Limit {bat_start}%–{bat_stop}%"
    if bat_enabled is True:
        return "FlexiCharger Enabled"
    if bat_enabled is False:
        return "Full 100%"
    if "ready" in str(battery.get("f1_text") or "").lower():
        return "Ready (EC 0xCB / XRAM 0x06D0..0x06D2)"
    return "FlexiCharger Unavailable"


class TrayController(QObject):
    """Manages QSystemTrayIcon, static DBus-safe QMenu hierarchy, and threaded ScriptRunner IPC."""

    status_ready = pyqtSignal(object)
    action_finished = pyqtSignal(object)
    config_applied = pyqtSignal(object)
    auto_apply_saved = pyqtSignal(bool, str)

    def __init__(
        self,
        runner: gui.ScriptRunner,
        poll_sec: float = 4.0,
        parent: Optional[QObject] = None,
        show_tray: bool = True,
        on_action_done: Optional[Callable[[dict[str, Any]], None]] = None,
        no_auto_apply: bool = False,
    ) -> None:
        super().__init__(parent)
        self.runner = runner
        self.poll_sec = max(1.0, float(poll_sec))
        self._show_tray = show_tray
        self._on_action_done_cb = on_action_done
        self.no_auto_apply = bool(no_auto_apply)
        self._stopped = False

        self._refresh_lock = threading.Lock()
        self._refresh_in_flight = False
        self._refresh_pending = False
        self._refresh_thread: Optional[threading.Thread] = None
        self._action_threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._last_snapshot: Optional[dict[str, Any]] = None
        self._last_config_apply_result: Optional[gui.ApplyConfigResult] = None

        self.status_ready.connect(self._apply_status_snapshot, Qt.QueuedConnection)
        self.action_finished.connect(self._handle_action_finished, Qt.QueuedConnection)
        self.config_applied.connect(self._handle_config_applied, Qt.QueuedConnection)
        self.auto_apply_saved.connect(self._handle_auto_apply_saved, Qt.QueuedConnection)

        self.menu = QMenu()
        self.tray = QSystemTrayIcon(render_tray_icon(None, None, live_mode=False), self)
        self.tray.setToolTip("Acer Aspire A715-59G Control — Initializing…")

        self.power_actions: dict[str, QAction] = {}
        self.fan_actions: dict[str, QAction] = {}
        self.brightness_actions: dict[int, QAction] = {}
        self.rgb_color_actions: dict[str, QAction] = {}
        self.battery_actions: dict[str, QAction] = {}
        self.peripheral_actions: dict[str, QAction] = {}
        self.submenus: dict[str, QMenu] = {}

        self._build_menu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray_activated)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(int(self.poll_sec * 1000))
        self.poll_timer.timeout.connect(self.refresh_status)

        if self._show_tray:
            self.tray.show()

    def _add_info_action(self, menu: QMenu, text: str) -> QAction:
        act = menu.addAction(text)
        act.setEnabled(False)
        return act

    def _build_menu(self) -> None:
        """Build the full QMenu & submenu hierarchy once to preserve com.canonical.dbusmenu IDs."""
        self.act_header_thermals = self._add_info_action(
            self.menu, "CPU: --°C (---- RPM) • GPU: --°C (---- RPM)"
        )
        self.act_header_profile = self._add_info_action(
            self.menu, "Profile: Checking… • dGPU: --"
        )
        self.menu.addSeparator()

        self.act_open_gui = self.menu.addAction("Open Control Center")
        self.act_open_gui.triggered.connect(self.launch_control_center)
        self.menu.addSeparator()

        # 1. Power Profile
        m_power = self.menu.addMenu("Power Profile")
        self.submenus["power"] = m_power
        for key, label, _desc in gui.POWER_PROFILES:
            act = m_power.addAction(POWER_PROFILE_MENU_LABELS.get(key, label))
            act.setCheckable(True)
            act.triggered.connect(lambda _chk=False, p=key: self.dispatch_action("power_profile.sh", [p]))
            self.power_actions[key] = act

        # 2. Fan Mode
        m_fan = self.menu.addMenu("Fan Mode")
        self.submenus["fan"] = m_fan
        self.act_fan_info = self._add_info_action(m_fan, "Mode: Checking…")
        m_fan.addSeparator()
        for key, label, tip in gui.FAN_MODES:
            if key == "reset":
                m_fan.addSeparator()
            act = m_fan.addAction(label)
            act.setToolTip(tip)
            if key != "reset":
                act.setCheckable(True)
            act.triggered.connect(lambda _chk=False, m=key: self.dispatch_action("fan.sh", [m]))
            self.fan_actions[key] = act

        # 4. Keyboard Backlight & RGB (KBType=6 1-zone ONLY — no zones/no 0xC4)
        m_rgb = self.menu.addMenu("Keyboard Backlight & RGB")
        self.submenus["rgb"] = m_rgb
        self._add_info_action(m_rgb, "Brightness (KBType=6)")
        for level, label in TRAY_BRIGHTNESS_PRESETS:
            act = m_rgb.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(
                lambda _chk=False, lv=str(level): self.dispatch_action("brightness.sh", [lv])
            )
            self.brightness_actions[level] = act

        m_rgb.addSeparator()
        self._add_info_action(m_rgb, "Color (1-Zone Global)")
        for color_key, label in TRAY_RGB_COLOR_KEYS:
            act = m_rgb.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(
                lambda _chk=False, c=color_key: self.dispatch_action("rgb.sh", [c])
            )
            self.rgb_color_actions[color_key] = act

        m_rgb.addSeparator()
        self._add_info_action(m_rgb, "Effect (KBType=6)")
        self.act_rgb_breath = m_rgb.addAction("Breathing Effect")
        self.act_rgb_breath.setCheckable(True)
        self.act_rgb_breath.triggered.connect(
            lambda _chk=False: self.dispatch_action("rgb.sh", ["--effect", "breath"])
        )

        # 5. Battery FlexiCharger
        m_bat = self.menu.addMenu("Battery FlexiCharger")
        self.submenus["battery"] = m_bat
        self.act_bat_info = self._add_info_action(m_bat, "Charge: --")
        m_bat.addSeparator()
        for key, label, args in TRAY_BATTERY_PRESETS:
            act = m_bat.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(
                lambda _chk=False, a=list(args): self.dispatch_action("battery.sh", a)
            )
            self.battery_actions[key] = act

        # 6. Peripherals & Lid
        m_periph = self.menu.addMenu("Peripherals & Lid")
        self.submenus["peripherals"] = m_periph
        self.act_periph_mode = self._add_info_action(m_periph, "Mode: Checking…")
        m_periph.addSeparator()
        for key, label, subcmd, on_arg, off_arg in PERIPHERAL_TOGGLES:
            act = m_periph.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(
                lambda checked, sc=subcmd, on_a=on_arg, off_a=off_arg: self.dispatch_action(
                    "peripherals.sh", [sc, on_a if checked else off_a]
                )
            )
            self.peripheral_actions[key] = act

        self.menu.addSeparator()
        self.act_auto_apply = self.menu.addAction("Auto-Apply Config on Launch")
        self.act_auto_apply.setCheckable(True)
        self.act_auto_apply.setToolTip(f"Auto-apply saved settings on launch ({self.runner.config_path})")
        _set_action_checked(
            self.act_auto_apply,
            bool(self.runner.load_saved_config(ensure=False).get("auto_apply_on_launch", True)),
        )
        self.act_auto_apply.triggered.connect(self._on_auto_apply_toggled)

        self.act_reapply_config = self.menu.addAction("Reapply Saved Config")
        self.act_reapply_config.setToolTip(f"Reapply saved settings from {self.runner.config_path}")
        self.act_reapply_config.triggered.connect(lambda _chk=False: self.apply_saved_config(force=True))

        self.menu.addSeparator()
        self.act_refresh = self.menu.addAction("Refresh Status")
        self.act_refresh.triggered.connect(self.refresh_status)

        self.act_quit = self.menu.addAction("Quit Tray")
        self.act_quit.triggered.connect(self.quit_tray)

    def start(self) -> None:
        self.poll_timer.start()
        if (not self.runner.dry_run) and (not self.no_auto_apply):
            self.apply_saved_config(force=False, refresh_after=True)
        else:
            self.refresh_status()

    def stop(self) -> None:
        self._stopped = True
        self.poll_timer.stop()
        if self._show_tray:
            self.tray.hide()
        self.runner.stop_worker()

    def wait_background_tasks(self, timeout: float = 5.0) -> None:
        """Wait for any in-flight refresh/action threads (used cleanly in --self-test)."""
        with self._refresh_lock:
            r_th = self._refresh_thread
        if r_th is not None and r_th.is_alive():
            r_th.join(timeout=timeout)
        with self._threads_lock:
            threads = list(self._action_threads)
        for th in threads:
            if th.is_alive():
                th.join(timeout=timeout)

    @pyqtSlot()
    def quit_tray(self) -> None:
        self.stop()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    @pyqtSlot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.launch_control_center()

    @pyqtSlot()
    def launch_control_center(self) -> None:
        cmd = [sys.executable, os.path.join(SCRIPT_DIR, "gui.py")]
        if self.runner.dry_run:
            cmd.append("--dry-run")
        if self.runner.explicit_ec_file:
            cmd.extend(["--ec-file", self.runner.explicit_ec_file])
        if self.runner._explicit_config_path:
            cmd.extend(["--config", self.runner._explicit_config_path])
        if self.no_auto_apply:
            cmd.append("--no-auto-apply")
        try:
            subprocess.Popen(
                cmd,
                cwd=SCRIPT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            if self._show_tray:
                self.tray.showMessage(
                    "Acer Control Center",
                    f"Failed to launch gui.py: {exc}",
                    QSystemTrayIcon.Warning,
                    4000,
                )

    def collect_status_snapshot(self) -> dict[str, Any]:
        """Execute status queries via ScriptRunner (safe to call in background thread)."""
        mode_label = self.runner.mode_label()
        live_mode = mode_label in ("root", "worker", "daemon")
        raw_results: dict[str, gui.ScriptResult] = {}
        for script in (
            "power_profile.sh",
            "fan.sh",
            "brightness.sh",
            "rgb.sh",
            "battery.sh",
            "peripherals.sh",
        ):
            if self._stopped:
                break
            raw_results[script] = self.runner.run_script(script, ["status"])

        power = (
            gui.parse_power_status(raw_results["power_profile.sh"].stdout)
            if "power_profile.sh" in raw_results
            else {}
        )
        fan = (
            gui.parse_fan_status(raw_results["fan.sh"].stdout)
            if "fan.sh" in raw_results
            else gui.FanStatus()
        )
        brightness = (
            gui.parse_brightness_status(raw_results["brightness.sh"].stdout)
            if "brightness.sh" in raw_results
            else {}
        )
        rgb = (
            gui.parse_rgb_status(raw_results["rgb.sh"].stdout)
            if "rgb.sh" in raw_results
            else {}
        )
        battery = (
            gui.parse_battery_status(raw_results["battery.sh"].stdout)
            if "battery.sh" in raw_results
            else {}
        )
        peripherals = (
            gui.parse_peripherals_status(raw_results["peripherals.sh"].stdout)
            if "peripherals.sh" in raw_results
            else gui.PeripheralsStatus()
        )

        return {
            "mode_label": mode_label,
            "live_mode": live_mode,
            "power": power,
            "fan": fan,
            "brightness": brightness,
            "rgb": rgb,
            "battery": battery,
            "peripherals": peripherals,
            "auto_apply_on_launch": bool(
                self.runner.load_saved_config(ensure=False).get("auto_apply_on_launch", True)
            ),
            "raw_results": raw_results,
        }

    @pyqtSlot()
    def refresh_status(self) -> None:
        """Schedule a coalesced background status refresh without blocking the Qt UI thread."""
        with self._refresh_lock:
            if self._stopped:
                return
            if self._refresh_in_flight:
                self._refresh_pending = True
                return
            self._refresh_in_flight = True
            self._refresh_pending = False

        def worker() -> None:
            snapshot: Optional[dict[str, Any]] = None
            try:
                snapshot = self.collect_status_snapshot()
            finally:
                with self._refresh_lock:
                    self._refresh_in_flight = False
                    if self._refresh_thread is threading.current_thread():
                        self._refresh_thread = None
                    pending = self._refresh_pending and not self._stopped
                    self._refresh_pending = False
                if snapshot is not None and not self._stopped:
                    self.status_ready.emit(snapshot)
                if pending:
                    self.refresh_status()

        th = threading.Thread(target=worker, name="TrayStatusRefresh", daemon=True)
        with self._refresh_lock:
            self._refresh_thread = th
        th.start()

    def dispatch_action(self, script: str, args: list[str]) -> None:
        """Run a control action in a background thread and refresh menu state on completion."""
        def worker() -> None:
            try:
                res = self.runner.run_script(script, list(args))
                if res.rc == 0 and (not self.runner.dry_run) and res.mode in ("root", "worker", "daemon"):
                    try:
                        self.runner.record_action(script, list(args))
                    except Exception as exc:  # noqa: BLE001
                        res.note = (f"{res.note} " if res.note else "") + f"[Config save warning: {exc}]"
                if not self._stopped:
                    self.action_finished.emit({"script": script, "args": list(args), "result": res})
            finally:
                with self._threads_lock:
                    self._action_threads.discard(threading.current_thread())

        th = threading.Thread(target=worker, name=f"TrayAction-{script}", daemon=True)
        with self._threads_lock:
            self._action_threads.add(th)
        th.start()

    def apply_saved_config(self, force: bool = False, refresh_after: bool = True) -> None:
        """Apply saved config.json in a background thread via daemon/fallback, then optionally refresh."""
        def worker() -> None:
            res: Optional[gui.ApplyConfigResult] = None
            err: Optional[str] = None
            try:
                res = self.runner.apply_saved_config(force=force)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            finally:
                with self._threads_lock:
                    self._action_threads.discard(threading.current_thread())
            if not self._stopped:
                self.config_applied.emit(
                    {
                        "force": force,
                        "refresh_after": refresh_after,
                        "result": res,
                        "error": err,
                    }
                )

        th = threading.Thread(target=worker, name="TrayApplyConfig", daemon=True)
        with self._threads_lock:
            self._action_threads.add(th)
        th.start()

    @pyqtSlot(object)
    def _handle_config_applied(self, payload: object) -> None:
        if self._stopped or not isinstance(payload, dict):
            return
        res: Optional[gui.ApplyConfigResult] = payload.get("result")
        err = payload.get("error")
        force = bool(payload.get("force"))
        refresh_after = bool(payload.get("refresh_after", True))
        if res is not None:
            self._last_config_apply_result = res
            if res.config:
                _set_action_checked(
                    self.act_auto_apply,
                    bool(res.config.get("auto_apply_on_launch", True)),
                )
        if self._show_tray:
            if err:
                self.tray.showMessage("Acer Config Error", str(err)[:220], QSystemTrayIcon.Warning, 4000)
            elif res is not None and res.errors and not res.skipped:
                self.tray.showMessage(
                    "Acer Config Apply Error",
                    res.errors[0][:220],
                    QSystemTrayIcon.Warning,
                    4000,
                )
            elif res is not None and force and res.mode in ("dry-run", "snapshot-dry-run"):
                self.tray.showMessage(
                    "Preview Only (Daemon Offline)",
                    f"Dry-run previewed {len(res.applied)} saved action(s).",
                    QSystemTrayIcon.Information,
                    3000,
                )
        if refresh_after:
            self.refresh_status()

    def _on_auto_apply_toggled(self, checked: bool) -> None:
        """Persist Auto-Apply Config on Launch checkbox state cleanly in background thread."""
        def worker() -> None:
            actual = bool(checked)
            err = ""
            try:
                _ok, saved, _via = self.runner.set_auto_apply_on_launch(bool(checked))
                actual = bool(saved.get("auto_apply_on_launch", checked))
            except Exception as exc:  # noqa: BLE001
                actual = not bool(checked)
                err = str(exc)
            finally:
                with self._threads_lock:
                    self._action_threads.discard(threading.current_thread())
            if not self._stopped:
                self.auto_apply_saved.emit(actual, err)

        th = threading.Thread(target=worker, name="TrayAutoApplySave", daemon=True)
        with self._threads_lock:
            self._action_threads.add(th)
        th.start()

    @pyqtSlot(bool, str)
    def _handle_auto_apply_saved(self, enabled: bool, err: str) -> None:
        if self._stopped:
            return
        _set_action_checked(self.act_auto_apply, bool(enabled))
        if err and self._show_tray:
            self.tray.showMessage("Acer Config Error", err[:220], QSystemTrayIcon.Warning, 4000)

    @pyqtSlot(object)
    def _handle_action_finished(self, payload: object) -> None:
        if self._stopped or not isinstance(payload, dict):
            return
        res: gui.ScriptResult = payload["result"]
        if self._on_action_done_cb is not None:
            self._on_action_done_cb(payload)
        if self._show_tray:
            if res.rc != 0:
                msg = res.stderr.strip() or res.stdout.strip() or f"Command exited {res.rc}"
                self.tray.showMessage("Acer Control Error", msg[:220], QSystemTrayIcon.Warning, 4000)
            elif res.mode == "snapshot-dry-run":
                self.tray.showMessage(
                    "Preview Only (Daemon Offline)",
                    f"Dry-run preview: {res.script} {' '.join(res.args)}",
                    QSystemTrayIcon.Information,
                    3000,
                )
        self.refresh_status()

    @pyqtSlot(object)
    def _apply_status_snapshot(self, snapshot: object) -> None:
        if self._stopped or not isinstance(snapshot, dict):
            return
        self._last_snapshot = snapshot

        mode_label = str(snapshot.get("mode_label") or "locked")
        live_mode = bool(snapshot.get("live_mode"))
        power: dict[str, Any] = snapshot.get("power") or {}
        fan: gui.FanStatus = snapshot.get("fan") or gui.FanStatus()
        brightness: dict[str, Any] = snapshot.get("brightness") or {}
        rgb: dict[str, Any] = snapshot.get("rgb") or {}
        battery: dict[str, Any] = snapshot.get("battery") or {}
        peripherals: gui.PeripheralsStatus = snapshot.get("peripherals") or gui.PeripheralsStatus()

        cpu_temp = fan.cpu_temp
        gpu_temp = fan.dgpu_temp if fan.dgpu_temp is not None else fan.nvidia_temp
        cpu_t_str = f"{cpu_temp}°C" if cpu_temp is not None else "--°C"
        if gpu_temp is not None:
            gpu_t_str = f"{gpu_temp}°C"
        elif "d3cold" in (fan.nvidia_text or "").lower():
            gpu_t_str = "D3cold"
        else:
            gpu_t_str = "--°C"
        cpu_rpm_str = f"{fan.cpu_fan_rpm} RPM" if fan.cpu_fan_rpm is not None else "---- RPM"
        gpu_rpm_str = f"{fan.gpu_fan_rpm} RPM" if fan.gpu_fan_rpm is not None else "---- RPM"

        self.act_header_thermals.setText(
            f"CPU: {cpu_t_str} ({cpu_rpm_str}) • GPU: {gpu_t_str} ({gpu_rpm_str})"
        )

        profile_key = power.get("profile_key")
        profile_disp = _format_profile_label(power, mode_label)
        tgp_disp = _format_gpu_tgp_label(power)
        oem_pl_disp = _format_oem_pl_label(power, profile_key)
        header_profile = f"Profile: {profile_disp} • dGPU: {tgp_disp}"
        if oem_pl_disp:
            header_profile += f" • CPU PL: {oem_pl_disp}"
        self.act_header_profile.setText(header_profile)

        # Icon + Tooltip
        self.tray.setIcon(render_tray_icon(profile_key, cpu_temp, live_mode=live_mode))
        mode_human = {
            "root": "Live Root EC",
            "worker": "Live pkexec Worker",
            "daemon": "Live Daemon",
            "ec-file": "Snapshot Mode",
            "dry-run": "Dry-Run Mode",
            "locked": "Unprivileged Locked",
        }.get(mode_label, mode_label)
        bat_cap = battery.get("sysfs_capacity")
        bat_stat = battery.get("sysfs_status")
        bat_enabled = battery.get("f1_enabled")
        bat_start = battery.get("f1_start")
        bat_stop = battery.get("f1_stop")
        bat_flexi_note = _format_flexicharger_note(battery)

        if bat_cap is not None:
            bat_tip = f" • Battery: {bat_cap}% ({bat_stat or 'Unknown'}, {bat_flexi_note})"
        elif bat_enabled is not None or "ready" in str(battery.get("f1_text") or "").lower():
            bat_tip = f" • Battery: {bat_flexi_note}"
        else:
            bat_tip = ""
        oem_pl_line = f"\nOEM PL1/PL2: {oem_pl_disp}" if oem_pl_disp else ""
        self.tray.setToolTip(
            f"Acer Aspire A715-59G ({mode_human})\n"
            f"CPU: {cpu_t_str} ({cpu_rpm_str}) • GPU: {gpu_t_str} ({gpu_rpm_str})\n"
            f"Profile: {profile_disp} • dGPU: {tgp_disp}{bat_tip}{oem_pl_line}"
        )

        # Power Profile checkmarks
        for key, act in self.power_actions.items():
            _set_action_checked(act, profile_key == key)

        # Fan Mode locking & checkmarks (locked in Quiet / Power Saving)
        fan_locked = profile_key in ("quiet", "powersaving")
        if fan_locked:
            self.act_fan_info.setText(
                "Locked: EC Eco-Auto (0x02/0x1A • Read-Only)"
                if profile_key == "powersaving"
                else "Locked: EC Silent-Auto (0x80 • Read-Only)"
            )
        elif fan.fan_mode_text and fan.fan_mode_text != "N/A":
            self.act_fan_info.setText(f"Mode: {fan.fan_mode_text}")
        else:
            self.act_fan_info.setText("Mode: EC Fan Control")
        for key, act in self.fan_actions.items():
            act.setEnabled(not fan_locked)
            if act.isCheckable():
                _set_action_checked(act, (not fan_locked) and (fan.fan_mode_key == key))

        # Keyboard Backlight & RGB checkmarks
        bri_level = brightness.get("level")
        for lv, act in self.brightness_actions.items():
            _set_action_checked(act, bri_level == lv)

        rgb_tuple = rgb.get("rgb")
        rgb_effect = rgb.get("effect")
        preset_map = dict(gui.RGB_PRESETS)
        is_breath = str(rgb_effect or "").lower() == "breath"
        _set_action_checked(self.act_rgb_breath, is_breath)
        for color_key, act in self.rgb_color_actions.items():
            _set_action_checked(
                act, (not is_breath) and (rgb_tuple is not None) and (rgb_tuple == preset_map.get(color_key))
            )

        # Battery FlexiCharger status & checkmarks (Direct EC 0xCB / XRAM 0x06D0..0x06D2)
        bat_parts: list[str] = []
        if bat_cap is not None:
            bat_parts.append(f"{bat_cap}% ({bat_stat or 'Unknown'})")
        else:
            bat_parts.append("--")
        bat_parts.append(bat_flexi_note)
        self.act_bat_info.setText(f"Charge: {' • '.join(bat_parts)}")

        is_full_charge = bat_enabled is False
        is_conservation = (bat_enabled is True) and (bat_start == 60 and bat_stop == 80)
        is_balanced = (bat_enabled is True) and (bat_start == 70 and bat_stop == 85)
        _set_action_checked(self.battery_actions["conservation"], is_conservation)
        _set_action_checked(self.battery_actions["balanced"], is_balanced)
        _set_action_checked(self.battery_actions["full"], is_full_charge)

        # Peripherals & Lid mode banner + live checkmarks
        if self.runner.dry_run:
            self.act_periph_mode.setText("Mode: Dry-Run Preview (--dry-run)")
        elif live_mode:
            self.act_periph_mode.setText(f"Mode: Live Control ({mode_human})")
        else:
            self.act_periph_mode.setText("Mode: Preview Only (Daemon Offline — dry-run)")

        _set_action_checked(self.peripheral_actions["webcam"], peripherals.webcam.upper() == "ON")
        _set_action_checked(self.peripheral_actions["touchpad"], peripherals.trackpad.upper() == "ON")
        _set_action_checked(self.peripheral_actions["lid_wake"], peripherals.wake_on_lid.upper() == "ON")
        _set_action_checked(self.peripheral_actions["lid_close"], peripherals.lid_close_key == "ignore")
        _set_action_checked(self.peripheral_actions["win_lock"], peripherals.win_lock.upper() == "ON")
        _set_action_checked(self.peripheral_actions["fn_lock"], peripherals.fn_lock.upper() == "ON")
        if "auto_apply_on_launch" in snapshot:
            _set_action_checked(self.act_auto_apply, bool(snapshot["auto_apply_on_launch"]))


# ---------------------------------------------------------------------------
# Self-Test Suite (--self-test)
# ---------------------------------------------------------------------------

@contextmanager
def _temp_env(key: str, value: str) -> Iterator[None]:
    prev = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


def _pump_until(app: QApplication, predicate: Callable[[], bool], timeout: float = 4.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    return predicate()


def run_self_test() -> int:
    """Verify icon rendering, static QMenu structure, ec0_io.bin parsing, daemon IPC, and dry-run actions."""
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        tag = "PASS" if cond else "FAIL"
        suffix = f" ({detail})" if detail else ""
        print(f"[{tag}] {name}{suffix}")
        if not cond:
            failures.append(f"{name}: {detail}")

    app = QApplication.instance()
    if app is None:
        app = QApplication(["tray.py", "--self-test"])
    app.setQuitOnLastWindowClosed(False)

    # 1. render_tray_icon across all 4 profiles + live vs snapshot dot
    profile_byte_sigs: dict[str, bytes] = {}
    for prof_key, _lbl, _desc in gui.POWER_PROFILES:
        ic_live = render_tray_icon(prof_key, 54, live_mode=True)
        ic_snap = render_tray_icon(prof_key, 54, live_mode=False)
        pm_live = ic_live.pixmap(64, 64)
        pm_snap = ic_snap.pixmap(64, 64)
        img_live = pm_live.toImage()
        img_snap = pm_snap.toImage()
        bytes_live = img_live.bits().asstring(img_live.byteCount())
        bytes_snap = img_snap.bits().asstring(img_snap.byteCount())
        profile_byte_sigs[prof_key] = bytes_live
        check(
            f"render_tray_icon({prof_key}) 64x64 + live/snapshot indicator",
            pm_live.width() == 64
            and pm_live.height() == 64
            and not pm_live.isNull()
            and bytes_live != bytes_snap,
        )
    check(
        "render_tray_icon distinct colors across all 4 profiles",
        len(set(profile_byte_sigs.values())) == 4,
    )

    # 2. TrayController instantiation, QMenu structure & snapshot parsing (ec0_io.bin)
    with tempfile.TemporaryDirectory(prefix="acer_tray_selftest_") as tmp_dir, _temp_env(
        "ACER_CONTROL_CONFIG", os.path.join(tmp_dir, "isolated_config.json")
    ):
        ec_fixture = gui.DEFAULT_EC_SNAPSHOT
        if not os.path.isfile(ec_fixture):
            ec_buf = bytearray(256)
            ec_buf[0x03] = 0x05  # LSTE=1 (lid open), LWKE=1
            ec_buf[0x07] = 54    # CPU temp 54C
            ec_buf[0x0B] = 47    # dGPU temp 47C
            ec_buf[0x10] = 51    # BAT0 51%
            ec_buf[0xCE], ec_buf[0xCF] = 71, 71      # ~28% fan duty
            ec_buf[0xD0], ec_buf[0xD1] = 0x04, 0x1D  # CPU fan 2048 RPM (raw 1053)
            ec_buf[0xD2], ec_buf[0xD3] = 0x04, 0x29  # GPU fan 2025 RPM (raw 1065)
            ec_buf[0xE2] = 0x10  # WinLock ON, FnSwap OFF
            ec_buf[0xF0:0xF5] = bytes((0x18, 0x81, 0x98, 0x83, 0xDF))  # 35W/115W PECI
            ec_fixture = os.path.join(tmp_dir, "ec0_io.bin")
            with open(ec_fixture, "wb") as fh:
                fh.write(ec_buf)
        ec_snap_exists = os.path.isfile(ec_fixture)
        check("EC snapshot ec0_io.bin fixture present", ec_snap_exists, ec_fixture)
        with _temp_env("ACER_CONTROL_SOCK", os.path.join(tmp_dir, "absent.sock")):
            snap_runner = gui.ScriptRunner(
                dry_run=False,
                explicit_ec_file=ec_fixture if ec_snap_exists else None,
                cache_dir=tmp_dir,
            )
            ctrl = TrayController(snap_runner, poll_sec=4.0, show_tray=False)
            try:
                expected_submenus = {"power", "fan", "rgb", "battery", "peripherals"}
                power_texts = [a.text() for a in ctrl.submenus["power"].actions() if not a.isSeparator()]
                fan_texts = [a.text() for a in ctrl.submenus["fan"].actions() if not a.isSeparator()]
                rgb_texts = [a.text() for a in ctrl.submenus["rgb"].actions() if not a.isSeparator()]
                forbidden_3zone = (
                    "Zone 3",
                    "Zone 4",
                    "Zone 5",
                    "0xC4",
                    "3-Zone",
                    "Cycle",
                    "Wave",
                    "Flash",
                    "Random",
                    "Dance",
                    "Tempo",
                )
                has_forbidden = any(tok in txt for txt in rgb_texts for tok in forbidden_3zone)
                with open(os.path.realpath(__file__), "r", encoding="utf-8") as tray_src_fp:
                    tray_src_low = tray_src_fp.read().lower()
                forbidden_wmi_tokens = (
                    "acer_rth_" + "wmi",
                    "og" + "ns",
                    "np" + "cf",
                    "nv" + "pcf",
                    "dc" + "hu",
                )
                found_tray_wmi = [tok for tok in forbidden_wmi_tokens if tok in tray_src_low]
                check(
                    "TrayController QMenu submenus, stock Fan Mode menu, & zero legacy WMI tokens",
                    set(ctrl.submenus.keys()) == expected_submenus
                    and not hasattr(ctrl, "gpu_boost_actions")
                    and len(ctrl.power_actions) == 4
                    and len(ctrl.fan_actions) == 4
                    and set(ctrl.fan_actions.keys()) == {"auto", "max", "custom", "reset"}
                    and not any("perf-auto" in txt.lower() for txt in fan_texts)
                    and len(ctrl.brightness_actions) == 6
                    and len(ctrl.rgb_color_actions) == 7
                    and len(ctrl.battery_actions) == 3
                    and len(ctrl.peripheral_actions) == 6
                    and not has_forbidden
                    and len(found_tray_wmi) == 0,
                    f"submenus={sorted(ctrl.submenus.keys())}, fan={fan_texts}, wmi={found_tray_wmi}",
                )
                check(
                    "Power Profile menu labels updated (no Turbo, Quiet vs Power Saving dGPU)",
                    len(gui.POWER_PROFILES) == 4
                    and all(k != "turbo" for k, _t, _d in gui.POWER_PROFILES)
                    and "turbo" not in PROFILE_BADGE_STYLES
                    and "turbo" not in ctrl.power_actions
                    and not any("turbo" in txt.lower() for txt in power_texts)
                    and "Quiet (15/30W • EC Silent Auto Fan • 25W dGPU)" in power_texts
                    and "Power Saving (15/30W • EC Eco Auto Fan • dGPU Off/D3cold)" in power_texts
                    and "Entertainment (45/115W • 60W dGPU)" in power_texts
                    and "Performance (90/115W • 75W dGPU)" in power_texts,
                    f"power_texts={power_texts}",
                )

                # Capture QMenu/QAction identities before refresh to assert in-place updates
                menu_before = ctrl.menu
                actions_before = list(ctrl.menu.actions())

                ctrl.refresh_status()
                got_snap = _pump_until(app, lambda: ctrl._last_snapshot is not None, timeout=5.0)
                menu_after = ctrl.menu
                actions_after = list(ctrl.menu.actions())

                check(
                    "Snapshot refresh updates QMenu in-place without clearing/rebuilding",
                    got_snap and (menu_before is menu_after) and (actions_before == actions_after),
                )
                if ec_snap_exists and ctrl._last_snapshot is not None:
                    therm_txt = ctrl.act_header_thermals.text()
                    prof_txt = ctrl.act_header_profile.text()
                    bat_txt = ctrl.act_bat_info.text()
                    check(
                        "Snapshot ec0_io.bin telemetry, Ready FlexiCharger & peripherals parsed into tray menu",
                        "54°C" in therm_txt
                        and "2048 RPM" in therm_txt
                        and "47°C" in therm_txt
                        and "2025 RPM" in therm_txt
                        and "Snapshot" in prof_txt
                        and "Ready (EC 0xCB / XRAM 0x06D0..0x06D2)" in bat_txt
                        and ctrl.peripheral_actions["win_lock"].isChecked() is True
                        and ctrl.peripheral_actions["fn_lock"].isChecked() is False,
                        f"thermals={therm_txt!r}, profile={prof_txt!r}, bat={bat_txt!r}",
                    )

                quiet_power = gui.parse_power_status(
                    "Power Profile : quiet (0x02)\n"
                    "EC PL1T/PL2T  : 15.000 W (en=1) / 30.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
                    "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
                    "RAPL MSR      : 15.000 W / 30.000 W (OS ceiling; nominal TDP=45.000 W)\n"
                    "GPU Power     : 25.00 W / default 60.00 W / max 75.00 W "
                    "(nvidia-powerd=active, power/control=on, runtime=active) [Quiet 25W, cur=25.00 W]\n"
                )
                fan_auto_02 = gui.parse_fan_status(
                    "=== EC Thermal & Fan Telemetry (/sys/kernel/debug/ec/ec0/io) ===\n"
                    "  CPU Temp  (0x07) : 51 C  (Sensor 0x08: 44 C)\n"
                    "  CPU VR    (0x0A) : 46 C  (dGPU 0x0B  : 44 C)\n"
                    "  CPU Fan   (0xCE) : 2100 RPM  (duty= 75/255,  29%, raw_tach=1026)\n"
                    "  GPU Fan   (0xCF) : 2050 RPM  (duty= 72/255,  28%, raw_tach=1051)\n"
                    "  Fan Mode  (0xD7) : Auto (0x02)\n"
                )
                fan_silent_80 = gui.parse_fan_status(
                    "=== EC Thermal & Fan Telemetry (/sys/kernel/debug/ec/ec0/io) ===\n"
                    "  CPU Temp  (0x07) : 51 C  (Sensor 0x08: 44 C)\n"
                    "  CPU VR    (0x0A) : 46 C  (dGPU 0x0B  : 44 C)\n"
                    "  CPU Fan   (0xCE) : 2100 RPM  (duty= 75/255,  29%, raw_tach=1026)\n"
                    "  GPU Fan   (0xCF) : 2050 RPM  (duty= 72/255,  28%, raw_tach=1051)\n"
                    "  Fan Mode  (0xD7) : Silent-Auto (0x80, Quiet Profile)\n"
                )
                bat_cons = gui.parse_battery_status(
                    "Linux sysfs   : status=Not charging, capacity=79%\n"
                    "FlexiCharger  : Enabled (80% limit, Stop=80% Start=60% | Direct EC 0xCB / XRAM 0x06D0=0x1B)\n"
                )
                ctrl._apply_status_snapshot(
                    {
                        "mode_label": "daemon",
                        "live_mode": True,
                        "power": quiet_power,
                        "fan": fan_silent_80,
                        "brightness": {},
                        "rgb": {},
                        "battery": bat_cons,
                        "peripherals": gui.PeripheralsStatus(),
                    }
                )
                check(
                    "Tray Quiet (0x80 Silent-Auto) locks & unchecks Fan Mode submenu, shows Quiet 25W dGPU + FlexiCharger",
                    ctrl.power_actions["quiet"].isChecked() is True
                    and fan_silent_80.fan_mode_key == "auto"
                    and "0x80" in fan_silent_80.fan_mode_raw
                    and all(not act.isEnabled() for act in ctrl.fan_actions.values())
                    and all(not act.isChecked() for act in ctrl.fan_actions.values() if act.isCheckable())
                    and ctrl.act_fan_info.text() == "Locked: EC Silent-Auto (0x80 • Read-Only)"
                    and "Quiet" in ctrl.act_header_profile.text()
                    and "Quiet 25W" in ctrl.act_header_profile.text()
                    and "OEM 15W/30W" in ctrl.act_header_profile.text()
                    and "OEM PL1/PL2: OEM 15W/30W" in ctrl.tray.toolTip()
                    and "Limit 60%–80%" in ctrl.act_bat_info.text()
                    and ctrl.battery_actions["conservation"].isChecked() is True
                    and ctrl.battery_actions["balanced"].isChecked() is False
                    and ctrl.battery_actions["full"].isChecked() is False,
                    f"hdr={ctrl.act_header_profile.text()!r}, fan_info={ctrl.act_fan_info.text()!r}, bat={ctrl.act_bat_info.text()!r}",
                )

                ps_d3cold_power = gui.parse_power_status(
                    "Power Profile : powersaving (0x03)\n"
                    "EC PL1T/PL2T  : 15.000 W (en=1) / 30.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
                    "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
                    "GPU Power     : Disabled / Suspended (D3cold / Power Saving mode)\n"
                )
                fan_d3cold = gui.parse_fan_status(
                    "=== EC Thermal & Fan Telemetry (/sys/kernel/debug/ec/ec0/io) ===\n"
                    "  CPU Temp  (0x07) : 44 C  (Sensor 0x08: 38 C)\n"
                    "  CPU VR    (0x0A) : 40 C  (dGPU 0x0B  : N/A (D3cold))\n"
                    "  CPU Fan   (0xCE) : 1900 RPM  (duty= 65/255,  25%, raw_tach=1135)\n"
                    "  GPU Fan   (0xCF) :    0 RPM  (duty=  0/255,   0%, raw_tach=0)\n"
                    "  Fan Mode  (0xD7) : Auto (0x02, Power Saving Eco Policy)\n"
                    "=== NVIDIA GPU ===\n"
                    "  NVIDIA dGPU, Suspended (D3cold), 0 W\n"
                )
                bat_ready = gui.parse_battery_status(
                    "Linux sysfs   : status=Charging, capacity=52%\n"
                    "FlexiCharger  : Ready (Direct EC 0xCB / XRAM 0x06D0..0x06D2; run via root/daemon to read live XRAM)\n"
                )
                ctrl._apply_status_snapshot(
                    {
                        "mode_label": "daemon",
                        "live_mode": True,
                        "power": ps_d3cold_power,
                        "fan": fan_d3cold,
                        "brightness": {},
                        "rgb": {},
                        "battery": bat_ready,
                        "peripherals": gui.PeripheralsStatus(),
                    }
                )
                ps_ok = (
                    ctrl.power_actions["powersaving"].isChecked() is True
                    and fan_d3cold.fan_mode_key == "auto"
                    and "0x02" in fan_d3cold.fan_mode_raw
                    and "Disabled (D3cold)" in ctrl.act_header_profile.text()
                    and "GPU: D3cold" in ctrl.act_header_thermals.text()
                    and ctrl.act_fan_info.text() == "Locked: EC Eco-Auto (0x02/0x1A • Read-Only)"
                    and all(not act.isEnabled() for act in ctrl.fan_actions.values())
                    and all(not act.isChecked() for act in ctrl.fan_actions.values() if act.isCheckable())
                    and "Ready (EC 0xCB / XRAM 0x06D0..0x06D2)" in ctrl.act_bat_info.text()
                    and all(not act.isChecked() for act in ctrl.battery_actions.values())
                )

                ent_power = gui.parse_power_status(
                    "Power Profile : entertainment / balanced (0x00)\n"
                    "EC PL1T/PL2T  : 45.000 W (en=1) / 115.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
                    "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
                    "GPU Power     : 60.00 W / default 60.00 W / max 75.00 W "
                    "(nvidia-powerd=active, power/control=on, runtime=active) [Entertainment 60W, cur=60.00 W]\n"
                )
                perf_power = gui.parse_power_status(
                    "Power Profile : performance (0x01)\n"
                    "EC PL1T/PL2T  : 90.000 W (en=1) / 115.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
                    "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
                    "GPU Power     : 75.00 W / default 60.00 W / max 75.00 W "
                    "(nvidia-powerd=active, power/control=on, runtime=active) [Performance 75W, cur=75.00 W]\n"
                )
                fan_auto_08 = gui.parse_fan_status(
                    "=== EC Thermal & Fan Telemetry (/sys/kernel/debug/ec/ec0/io) ===\n"
                    "  CPU Temp  (0x07) : 64 C  (Sensor 0x08: 50 C)\n"
                    "  Fan Mode  (0xD7) : Auto (0x08, Performance Profile)\n"
                )
                bat_bal = gui.parse_battery_status(
                    "Linux sysfs   : status=Charging, capacity=72%\n"
                    "FlexiCharger  : Enabled (85% limit, Stop=85% Start=70% | Direct EC 0xCB / XRAM 0x06D0=0x1A)\n"
                )
                bat_full = gui.parse_battery_status(
                    "Linux sysfs   : status=Full, capacity=100%\n"
                    "FlexiCharger  : Disabled (100% full charge, Stop=100% Start=95% | Direct EC 0xCB / XRAM 0x06D0=0x18)\n"
                )
                bat_unavail = gui.parse_battery_status(
                    "Linux sysfs   : status=Discharging, capacity=52%\n"
                    "FlexiCharger  : (unavailable: unlock live EC)\n"
                )
                ctrl._apply_status_snapshot(
                    {
                        "mode_label": "daemon",
                        "live_mode": True,
                        "power": ent_power,
                        "fan": fan_auto_02,
                        "brightness": {},
                        "rgb": {},
                        "battery": bat_bal,
                        "peripherals": gui.PeripheralsStatus(),
                    }
                )
                bal_ok = (
                    "OEM 45W/115W" in ctrl.act_header_profile.text()
                    and all(act.isEnabled() for act in ctrl.fan_actions.values())
                    and ctrl.fan_actions["auto"].isChecked() is True
                    and "Limit 70%–85%" in ctrl.act_bat_info.text()
                    and ctrl.battery_actions["balanced"].isChecked() is True
                    and ctrl.battery_actions["conservation"].isChecked() is False
                    and ctrl.battery_actions["full"].isChecked() is False
                )
                ctrl._apply_status_snapshot(
                    {
                        "mode_label": "daemon",
                        "live_mode": True,
                        "power": perf_power,
                        "fan": fan_auto_08,
                        "brightness": {},
                        "rgb": {},
                        "battery": bat_full,
                        "peripherals": gui.PeripheralsStatus(),
                    }
                )
                full_ok = (
                    "OEM 90W/115W" in ctrl.act_header_profile.text()
                    and "Performance 75W" in ctrl.act_header_profile.text()
                    and all(act.isEnabled() for act in ctrl.fan_actions.values())
                    and ctrl.fan_actions["auto"].isChecked() is True
                    and ctrl.fan_actions["max"].isChecked() is False
                    and "Full 100%" in ctrl.act_bat_info.text()
                    and ctrl.battery_actions["full"].isChecked() is True
                    and ctrl.battery_actions["conservation"].isChecked() is False
                    and ctrl.battery_actions["balanced"].isChecked() is False
                )
                ctrl._apply_status_snapshot(
                    {
                        "mode_label": "ec-file",
                        "live_mode": False,
                        "power": perf_power,
                        "fan": gui.FanStatus(cpu_temp=55),
                        "brightness": {},
                        "rgb": {},
                        "battery": bat_unavail,
                        "peripherals": gui.PeripheralsStatus(),
                    }
                )
                unavail_ok = (
                    "FlexiCharger Unavailable" in ctrl.act_bat_info.text()
                    and "FlexiCharger Unavailable" in ctrl.tray.toolTip()
                    and all(not act.isChecked() for act in ctrl.battery_actions.values())
                )
                check(
                    "Tray D3cold PowerSaving lock, Entertainment/Performance 0x02/0x08 Auto unlock & Direct EC Battery checkmarks",
                    ps_ok and bal_ok and full_ok and unavail_ok,
                    f"ps={ps_ok}, bal={bal_ok}, full={full_ok}, unavail={unavail_ok}",
                )
            finally:
                ctrl.stop()
                ctrl.wait_background_tasks(timeout=3.0)

        # 3. Daemon socket IPC via temporary DaemonServer (ACER_CONTROL_SOCK)
        import daemon as daemon_mod

        sock_path = os.path.join(tmp_dir, "tray_daemon_test.sock")
        srv = daemon_mod.DaemonServer(
            socket_path=sock_path,
            uid=os.getuid(),
            gid=os.getgid(),
            cache_dir=tmp_dir,
        )
        srv.start_in_thread()
        try:
            with _temp_env("ACER_CONTROL_SOCK", sock_path):
                d_runner = gui.ScriptRunner(dry_run=False, cache_dir=tmp_dir)
                check(
                    "Daemon socket IPC recognized by ScriptRunner",
                    d_runner.daemon_available()
                    and d_runner.mode_label() == "daemon"
                    and d_runner.is_live_root() is True,
                )
                pp_res = d_runner.run_script("power_profile.sh", ["--dry-run", "status"])
                check(
                    "Daemon socket executes script via AF_UNIX IPC",
                    pp_res.mode == "daemon" and pp_res.rc == 0 and "[dry-run]" in pp_res.stdout,
                    f"mode={pp_res.mode}, rc={pp_res.rc}",
                )
                if ec_snap_exists:
                    fan_res = d_runner.run_script("fan.sh", ["--ec-file", ec_fixture, "status"])
                    parsed_fan = gui.parse_fan_status(fan_res.stdout)
                    check(
                        "Daemon socket fan.sh --ec-file status parsed cleanly",
                        fan_res.mode == "daemon" and fan_res.rc == 0 and parsed_fan.cpu_temp == 54,
                    )
        finally:
            srv.shutdown()

        # 4. Dry-run command dispatches via TrayController QAction triggers
        dispatched: list[tuple[str, list[str], str, int]] = []

        def on_done(payload: dict[str, Any]) -> None:
            res: gui.ScriptResult = payload["result"]
            dispatched.append((payload["script"], list(payload["args"]), res.mode, res.rc))

        dry_runner = gui.ScriptRunner(dry_run=True, cache_dir=tmp_dir)
        dry_ctrl = TrayController(dry_runner, poll_sec=4.0, show_tray=False, on_action_done=on_done)
        try:
            dry_ctrl.power_actions["quiet"].trigger()
            dry_ctrl.fan_actions["auto"].trigger()
            dry_ctrl.brightness_actions[143].trigger()
            dry_ctrl.rgb_color_actions["cyan"].trigger()
            dry_ctrl.act_rgb_breath.trigger()
            dry_ctrl.battery_actions["conservation"].trigger()
            _set_action_checked(dry_ctrl.peripheral_actions["webcam"], True)
            dry_ctrl.peripheral_actions["webcam"].trigger()
            _set_action_checked(dry_ctrl.peripheral_actions["lid_close"], False)
            dry_ctrl.peripheral_actions["lid_close"].trigger()

            done_ok = _pump_until(app, lambda: len(dispatched) == 8, timeout=8.0)
            dry_ctrl.wait_background_tasks(timeout=3.0)
            app.processEvents()

            expected_cmds = {
                ("power_profile.sh", ("quiet",)),
                ("fan.sh", ("auto",)),
                ("brightness.sh", ("143",)),
                ("rgb.sh", ("cyan",)),
                ("rgb.sh", ("--effect", "breath")),
                ("battery.sh", ("limit", "60", "80")),
                ("peripherals.sh", ("webcam", "off")),
                ("peripherals.sh", ("lid-close", "ignore")),
            }
            got_cmds = {(s, tuple(a)) for s, a, _m, _rc in dispatched}
            all_dry_ok = all(m == "dry-run" and rc == 0 for _s, _a, m, rc in dispatched)
            check(
                "Dry-run QAction dispatches (8 actions across all 5 submenus)",
                done_ok and got_cmds == expected_cmds and all_dry_ok,
                f"count={len(dispatched)}, cmds={sorted(got_cmds)}",
            )
            check(
                "Dry-run QAction dispatches do NOT create/mutate config.json",
                not os.path.exists(dry_runner.config_path),
                dry_runner.config_path,
            )
        finally:
            dry_ctrl.stop()
            dry_ctrl.wait_background_tasks(timeout=3.0)

        # 5. Config Store integration, CLI options, launch auto-apply, --no-auto-apply & menu controls
        tray_cli = parse_cli(["--config", "/tmp/tray_cli_test.json", "--no-auto-apply"])
        check(
            "Tray parse_cli parses --config and --no-auto-apply",
            tray_cli.config == "/tmp/tray_cli_test.json" and tray_cli.no_auto_apply is True,
            str(tray_cli),
        )

        tray_cfg_path = os.path.join(tmp_dir, "tray_isolated_config.json")
        tray_cfg = config_store.default_config()
        tray_cfg["power"]["profile"] = "entertainment"
        tray_cfg["keyboard"]["brightness"] = 191
        tray_cfg["keyboard"]["mode"] = "effect"
        tray_cfg["keyboard"]["effect"] = "breath"
        tray_cfg["battery"] = {"mode": "limit", "start_pct": 70, "stop_pct": 85}
        config_store.save_config(tray_cfg_path, tray_cfg, uid=os.getuid(), gid=os.getgid())

        tray_cfg_sock = os.path.join(tmp_dir, "tray_cfg_daemon.sock")
        tray_cfg_srv = daemon_mod.DaemonServer(
            socket_path=tray_cfg_sock,
            uid=os.getuid(),
            gid=os.getgid(),
            cache_dir=tmp_dir,
            config_path=tray_cfg_path,
        )
        tray_set_calls: list[tuple[str, list[str]]] = []

        def _mock_tray_exec(script: str, act_args: list[str]) -> tuple[int, str, str]:
            if act_args != ["status"]:
                tray_set_calls.append((script, list(act_args)))
            return (0, f"[mock] {script} {' '.join(act_args)}", "")

        tray_cfg_srv._execute_script = _mock_tray_exec  # type: ignore[method-assign]
        tray_cfg_srv.start_in_thread()
        try:
            with _temp_env("ACER_CONTROL_SOCK", tray_cfg_sock):
                cfg_runner = gui.ScriptRunner(dry_run=False, cache_dir=tmp_dir, config_path=tray_cfg_path)
                cfg_ctrl = TrayController(cfg_runner, poll_sec=4.0, show_tray=False, no_auto_apply=False)
                try:
                    check(
                        "Tray menu exposes Auto-Apply Config on Launch (checkable) & Reapply Saved Config",
                        cfg_ctrl.act_auto_apply.isCheckable()
                        and cfg_ctrl.act_auto_apply.isChecked() is True
                        and cfg_ctrl.act_auto_apply in cfg_ctrl.menu.actions()
                        and cfg_ctrl.act_reapply_config in cfg_ctrl.menu.actions()
                        and tray_cfg_path in cfg_ctrl.act_auto_apply.toolTip(),
                    )
                    cfg_ctrl.start()
                    start_ok = _pump_until(
                        app,
                        lambda: cfg_ctrl._last_config_apply_result is not None
                        and cfg_ctrl._last_snapshot is not None,
                        timeout=5.0,
                    )
                    cfg_ctrl.wait_background_tasks(timeout=3.0)
                    app.processEvents()
                    check(
                        "Tray start() auto-applies saved config via daemon and refreshes status",
                        start_ok
                        and cfg_ctrl._last_config_apply_result is not None
                        and cfg_ctrl._last_config_apply_result.mode == "daemon-apply"
                        and tray_set_calls
                        == [
                            ("power_profile.sh", ["entertainment"]),
                            ("brightness.sh", ["191"]),
                            ("rgb.sh", ["--effect", "breath"]),
                            ("battery.sh", ["limit", "70", "85"]),
                        ],
                        f"ok={start_ok}, calls={tray_set_calls}",
                    )

                    # Toggle Auto-Apply Config on Launch off -> updates config.json cleanly
                    cfg_ctrl.act_auto_apply.trigger()
                    toggled_off = _pump_until(
                        app,
                        lambda: config_store.load_config(tray_cfg_path)["auto_apply_on_launch"] is False,
                        timeout=4.0,
                    )
                    cfg_ctrl.wait_background_tasks(timeout=3.0)

                    # Reapply Saved Config (force=True) still applies when auto_apply_on_launch is False
                    tray_set_calls.clear()
                    cfg_ctrl._last_config_apply_result = None
                    cfg_ctrl.act_reapply_config.trigger()
                    reapply_ok = _pump_until(
                        app,
                        lambda: cfg_ctrl._last_config_apply_result is not None
                        and len(tray_set_calls) == 4,
                        timeout=5.0,
                    )
                    cfg_ctrl.wait_background_tasks(timeout=3.0)

                    # Toggle Auto-Apply Config on Launch back on
                    cfg_ctrl.act_auto_apply.trigger()
                    toggled_on = _pump_until(
                        app,
                        lambda: config_store.load_config(tray_cfg_path)["auto_apply_on_launch"] is True,
                        timeout=4.0,
                    )
                    cfg_ctrl.wait_background_tasks(timeout=3.0)

                    # dispatch_action records rc=0 daemon call into temp config.json
                    cfg_ctrl.dispatch_action("power_profile.sh", ["quiet"])
                    rec_ok = _pump_until(
                        app,
                        lambda: config_store.load_config(tray_cfg_path)["power"]["profile"] == "quiet",
                        timeout=4.0,
                    )
                    cfg_ctrl.wait_background_tasks(timeout=3.0)

                    check(
                        "Tray Auto-Apply toggle, Reapply Saved Config & dispatch_action recording work",
                        toggled_off and toggled_on and reapply_ok and rec_ok,
                        f"off={toggled_off}, on={toggled_on}, reapply={reapply_ok}, rec={rec_ok}",
                    )
                finally:
                    cfg_ctrl.stop()
                    cfg_ctrl.wait_background_tasks(timeout=3.0)

                # --no-auto-apply skips launch auto-apply on start()
                tray_set_calls.clear()
                noauto_runner = gui.ScriptRunner(dry_run=False, cache_dir=tmp_dir, config_path=tray_cfg_path)
                noauto_ctrl = TrayController(noauto_runner, poll_sec=4.0, show_tray=False, no_auto_apply=True)
                try:
                    noauto_ctrl.start()
                    noauto_snap = _pump_until(
                        app, lambda: noauto_ctrl._last_snapshot is not None, timeout=4.0
                    )
                    noauto_ctrl.wait_background_tasks(timeout=3.0)
                    check(
                        "Tray --no-auto-apply skips launch auto-apply on start()",
                        noauto_snap
                        and noauto_ctrl._last_config_apply_result is None
                        and len(tray_set_calls) == 0,
                        f"snap={noauto_snap}, calls={tray_set_calls}",
                    )
                finally:
                    noauto_ctrl.stop()
                    noauto_ctrl.wait_background_tasks(timeout=3.0)
        finally:
            tray_cfg_srv.shutdown()

    print(f"Tray self-test finished: {len(failures)} failure(s).")
    return 1 if failures else 0


def parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Acer Aspire A715-59G System Tray Indicator")
    parser.add_argument("--dry-run", action="store_true", help="Preview commands with --dry-run without modifying hardware")
    parser.add_argument("--ec-file", default=None, help="Explicit EC snapshot file for status fallback")
    parser.add_argument("--config", default=None, help="Explicit config.json path")
    parser.add_argument("--no-auto-apply", action="store_true", help="Skip auto-applying saved config on launch")
    parser.add_argument("--poll-sec", type=float, default=4.0, help="Status polling interval in seconds (default: 4)")
    parser.add_argument("--self-test", action="store_true", help="Run tray self-tests and exit")
    args = parser.parse_args(argv)
    if args.poll_sec < 1.0:
        parser.error("--poll-sec must be >= 1")
    return args


def _install_signal_wakeups(controller: TrayController) -> list[socket.socket]:
    """Wire SIGINT/SIGTERM through a socketpair so Qt quits cleanly without blocking."""
    r_sock, w_sock = socket.socketpair()
    r_sock.setblocking(False)
    w_sock.setblocking(False)
    prev_fd = signal.set_wakeup_fd(w_sock.fileno(), warn_on_full_buffer=False)
    _ = prev_fd

    notifier = QSocketNotifier(r_sock.fileno(), QSocketNotifier.Read, controller)

    def on_signal_readable(_fd: int) -> None:
        try:
            r_sock.recv(1024)
        except OSError:
            pass
        controller.quit_tray()

    notifier.activated.connect(on_signal_readable)
    signal.signal(signal.SIGINT, lambda *_a: None)
    signal.signal(signal.SIGTERM, lambda *_a: None)
    return [r_sock, w_sock]


def main(argv: Optional[list[str]] = None) -> int:
    cli_args = parse_cli(sys.argv[1:] if argv is None else argv)
    if cli_args.self_test:
        return run_self_test()

    lock_fd = acquire_single_instance_lock()
    if lock_fd is None:
        print("Another Acer Aspire A715-59G tray instance is already running; exiting cleanly.")
        return 0

    app = QApplication([sys.argv[0]])
    app.setApplicationName("acer-a715-59g-tray")
    app.setApplicationDisplayName("Acer Aspire A715-59G Control")
    app.setDesktopFileName("acer-a715-59g-control")
    app.setQuitOnLastWindowClosed(False)

    runner = gui.ScriptRunner(
        dry_run=cli_args.dry_run,
        explicit_ec_file=cli_args.ec_file,
        config_path=cli_args.config,
    )
    controller = TrayController(
        runner,
        poll_sec=cli_args.poll_sec,
        show_tray=True,
        no_auto_apply=cli_args.no_auto_apply,
    )
    sig_socks = _install_signal_wakeups(controller)
    controller.start()

    try:
        return int(app.exec_())
    finally:
        controller.stop()
        signal.set_wakeup_fd(-1)
        for sock in sig_socks:
            try:
                sock.close()
            except OSError:
                pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
