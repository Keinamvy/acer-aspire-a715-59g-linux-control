#!/usr/bin/env python3
"""Native GTK3 Control Center GUI for Acer Aspire A715-59G (Board RTH)."""

from __future__ import annotations

import argparse
import ast
from collections import deque
from contextlib import contextmanager
import html
import importlib.util
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, fields
from typing import Any, Iterator, Mapping, Optional, Sequence, Union

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import config_store  # noqa: E402

ALLOWED_SCRIPTS = frozenset({
    "power_profile.sh",
    "fan.sh",
    "brightness.sh",
    "rgb.sh",
    "battery.sh",
    "peripherals.sh",
})
EC_FILE_SCRIPTS = frozenset({"power_profile.sh", "fan.sh", "battery.sh", "peripherals.sh"})
DEFAULT_EC_SNAPSHOT = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "portable_tools", "acpi_dump", "ec0_io.bin")
)
DEFAULT_DAEMON_SOCKET = "/run/acer-a715-59g-control.sock"

_POWER_PROFILE_SPECS: tuple[tuple[str, str, tuple[float, float], str, str, str], ...] = (
    (
        "quiet",
        "Quiet (0x02)",
        (15.0, 30.0),
        "EC Silent Auto Fan",
        "25W Quiet dGPU",
        "EC 0x02 — Quiet",
    ),
    (
        "powersaving",
        "Power Saving (0x03)",
        (15.0, 30.0),
        "EC Eco Auto Fan",
        "dGPU Disabled (D3cold)",
        "EC 0x03 — Power Saving",
    ),
    (
        "entertainment",
        "Entertainment (0x00)",
        (45.0, 115.0),
        "Balanced Fan",
        "60W dGPU",
        "EC 0x00 — Entertainment / Balanced",
    ),
    (
        "performance",
        "Performance (0x01)",
        (90.0, 115.0),
        "Auto Performance Fan",
        "75W dGPU",
        "EC 0x01 — Performance",
    ),
)
_POWER_PROFILE_SPEC_BY_KEY = {row[0]: row for row in _POWER_PROFILE_SPECS}


def _format_power_profile_copy(key: str) -> tuple[str, str]:
    """Return (button_desc, tooltip) for the given static OEM power profile spec."""
    _key, _title, (pl1_w, pl2_w), fan_desc, gpu_desc, tip_prefix = (
        _POWER_PROFILE_SPEC_BY_KEY[key]
    )
    desc = f"{fan_desc} • {pl1_w:g}W/{pl2_w:g}W OEM PL1/PL2 • {gpu_desc}"
    return desc, f"{tip_prefix}: {desc}"


POWER_PROFILES = [
    (key, title, _format_power_profile_copy(key)[0])
    for key, title, *_rest in _POWER_PROFILE_SPECS
]

POWER_PROFILE_TOOLTIPS = {
    key: _format_power_profile_copy(key)[1]
    for key, *_rest in _POWER_PROFILE_SPECS
}

FAN_PRIMARY_MODES = [
    ("auto", "Auto", "EC automatic fan control (0x02, or 0x08 in Performance mode)"),
    ("max", "Max / Full Speed", "Full-speed cooling override (0x10)"),
    ("custom", "Custom Curve", "User 2-middle-node CPU/GPU fan curve (0x04)"),
]

FAN_RESET_MODE = (
    "reset",
    "Reset Factory",
    "Reset EC fan table to BIOS default (FCMD 0xD9)",
)

FAN_MODES = [*FAN_PRIMARY_MODES, FAN_RESET_MODE]

# Reference curves over 30..100 C -> 0..100% duty.
# Auto is a representative BIOS reference envelope;
# Custom reflects the Control Center 4-point curve (40/50%, 60/60%, 80/70%, 100/100%).
FAN_CURVE_SPECS: dict[str, tuple[str, bool, list[tuple[float, float]]]] = {
    "auto": (
        "Auto (0x02 / 0x08)",
        False,
        [(30, 20), (45, 24), (55, 30), (65, 45), (75, 62), (85, 80), (95, 95), (100, 100)],
    ),
    "max": (
        "Max / Full Speed (0x10)",
        False,
        [(30, 100), (100, 100)],
    ),
    "custom": (
        "Custom 4-Point (0x04)",
        True,
        [(30, 50), (40, 50), (60, 60), (80, 70), (100, 100)],
    ),
}

CUSTOM_4PT_WAYPOINTS: tuple[tuple[int, int], ...] = (
    (40, 50),
    (60, 60),
    (80, 70),
    (100, 100),
)

BRIGHTNESS_PRESETS = [
    ("off", "Off (0)", 0),
    ("low", "Low (47)", 47),
    ("med", "Med (95)", 95),
    ("high", "High (143)", 143),
    ("max", "Max (191)", 191),
    ("full", "Full (255)", 255),
]

RGB_PRESETS = [
    ("blue", (0, 0, 255)),
    ("green", (0, 255, 0)),
    ("red", (255, 0, 0)),
    ("cyan", (0, 255, 200)),
    ("skyblue", (0, 255, 229)),
    ("yellow", (255, 255, 0)),
    ("orange", (255, 127, 0)),
    ("purple", (162, 0, 255)),
    ("pink", (255, 0, 210)),
    ("white", (255, 255, 255)),
    ("off", (0, 0, 0)),
]

RGB_EFFECTS = [
    (
        "breath",
        "Breathing Effect (0xCA Sub 0x0F)",
        "KBType=6 15-color breathing via 0xCA sub 0x0F (0xF9=0x0F, 0xFA=0x07, 0xFB=0x10)",
    ),
]

BATTERY_PRESETS = [
    ("Conservation 60%–80%", ["limit", "60", "80"]),
    ("Balanced 70%–85%", ["limit", "70", "85"]),
    ("Travel / Full 100%", ["full"]),
    ("Disable FlexiCharger", ["disable"]),
]


# ---------------------------------------------------------------------------
# Status Parsers
# ---------------------------------------------------------------------------

def _parse_watts_token(tok: str) -> tuple[Optional[float], str]:
    """Parse a GPU power token such as '60.00 W', '60.00', '[N/A]', or 'n/a'."""
    cleaned = tok.strip()
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:W)?$", cleaned, re.IGNORECASE)
    if m:
        return float(m.group(1)), f"{m.group(1)} W"
    return None, cleaned or "N/A"


def parse_power_status(text: str) -> dict[str, Any]:
    """Parse output of `power_profile.sh status`."""
    data: dict[str, Any] = {
        "dry_run": "[dry-run]" in text,
        "profile_key": None,
        "profile_text": "N/A",
        "ec_peci_text": "N/A",
        "ec_pl1_w": None,
        "ec_pl2_w": None,
        "ec_tau": None,
        "oem_table_text": "N/A",
        "oem_table_profiles": {},
        "rapl_msr_text": "N/A",
        "rapl_msr_pl1": None,
        "rapl_msr_pl2": None,
        "rapl_mmio_text": "N/A",
        "rapl_mmio_pl1": None,
        "rapl_mmio_pl2": None,
        "gpu_power_text": "N/A",
        "gpu_pm_state": None,
        "gpu_power_cur_w": None,
        "gpu_power_cur_text": "N/A",
        "gpu_power_default_w": None,
        "gpu_power_default_text": "N/A",
        "gpu_power_max_w": None,
        "gpu_power_max_text": "N/A",
        "gpu_tier_text": None,
        "gpu_tier_label": None,
        "nvidia_powerd_state": None,
        "gpu_pci_control": None,
        "gpu_runtime_status": None,
    }
    hex_to_key = {
        "0x00": "entertainment",
        "0x01": "performance",
        "0x02": "quiet",
        "0x03": "powersaving",
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = re.match(r"^Power Profile\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            data["profile_text"] = val
            hm = re.search(r"\((0x[0-9A-Fa-f]{2})\)", val)
            if hm:
                data["profile_key"] = hex_to_key.get(hm.group(1).lower())
            elif "snapshot mode" in val.lower():
                data["profile_text"] = "Snapshot (0xD8 skipped)"
            continue

        m = re.match(
            r"^(?:EC PL1T/PL2T|EC PECI(?:\s+Limits)?)\s*:\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*W(?:\s*\(en=(\d+)\))?\s*/\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*W(?:\s*\(en=(\d+)\))?"
            r"(?:\s*\(TAUT=(0x[0-9A-Fa-f]+)\))?(.*)$",
            line,
        )
        if m:
            pl1_s, en1, pl2_s, en2, tau, extra = (
                m.group(1),
                m.group(2),
                m.group(3),
                m.group(4),
                m.group(5),
                m.group(6).strip(),
            )
            data["ec_pl1_w"] = float(pl1_s)
            data["ec_pl2_w"] = float(pl2_s)
            data["ec_tau"] = tau
            p1_txt = f"PL1T {pl1_s} W" + (f" (en={en1})" if en1 is not None else "")
            p2_txt = f"PL2T {pl2_s} W" + (f" (en={en2})" if en2 is not None else "")
            peci_parts = [f"{p1_txt} / {p2_txt}"]
            if tau:
                peci_parts.append(f"TAUT={tau}")
            if extra:
                paren_m = re.fullmatch(r"\(([^()]+)\)", extra)
                peci_parts.append(paren_m.group(1).strip() if paren_m else extra)
            data["ec_peci_text"] = " • ".join(peci_parts)
            continue

        m = re.match(r"^OEM Table\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            data["oem_table_text"] = val
            table_map: dict[str, tuple[float, float]] = {}
            for pname, p1_s, p2_s in re.findall(
                r"\b(Quiet|PowerSaving|Entertainment|Performance)\s*=\s*"
                r"([0-9]+(?:\.[0-9]+)?)\s*/\s*([0-9]+(?:\.[0-9]+)?)\s*W",
                val,
                re.IGNORECASE,
            ):
                table_map[pname.lower()] = (float(p1_s), float(p2_s))
            data["oem_table_profiles"] = table_map
            continue

        rapl_matched = False
        for prefix, key_prefix in (("MSR", "rapl_msr"), ("MMIO", "rapl_mmio")):
            m = re.match(
                rf"^(?:OS\s+)?RAPL\s+{prefix}\s*:\s*"
                r"([0-9]+(?:\.[0-9]+)?)\s*W\s*/\s*"
                r"([0-9]+(?:\.[0-9]+)?)\s*W(.*)$",
                line,
            )
            if m:
                data[f"{key_prefix}_pl1"] = float(m.group(1))
                data[f"{key_prefix}_pl2"] = float(m.group(2))
                extra = m.group(3).strip()
                data[f"{key_prefix}_text"] = f"{m.group(1)} W / {m.group(2)} W {extra}".strip()
                rapl_matched = True
                break
        if rapl_matched:
            continue

        m = re.match(r"^GPU Power\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            data["gpu_power_text"] = val
            low = val.lower()
            if "rtd3 auto" in low or "d3cold pending" in low:
                data["gpu_pm_state"] = "d3cold_pending"
                data["gpu_tier_label"] = "RTD3 Auto / D3cold Pending"
                data["gpu_pci_control"] = "auto"
                data["gpu_runtime_status"] = "active"
                continue
            if "disabled / suspended" in low or "d3cold" in low:
                data["gpu_pm_state"] = "d3cold"
                data["gpu_tier_label"] = "Disabled / Suspended (D3cold)"
                data["gpu_pci_control"] = "auto"
                data["gpu_runtime_status"] = "suspended"
                continue

            data["gpu_pm_state"] = "active"
            limits_part = val
            meta_part = ""
            lim_split = re.match(
                r"^(.+?/\s*default\s+.+?/\s*max\s+(?:\[N/A\]|[^(\[]+?))\s+((?:\(|\[(?!N/A\])).*)$",
                val,
                re.IGNORECASE,
            )
            if lim_split:
                limits_part = lim_split.group(1).strip()
                meta_part = lim_split.group(2).strip()

            lim_m = re.match(
                r"^(.+?)\s*/\s*default\s+(.+?)\s*/\s*max\s+(.+?)\s*$",
                limits_part,
                re.IGNORECASE,
            )
            if lim_m:
                cur_w, cur_txt = _parse_watts_token(lim_m.group(1))
                def_w, def_txt = _parse_watts_token(lim_m.group(2))
                max_w, max_txt = _parse_watts_token(lim_m.group(3))
                data["gpu_power_cur_w"] = cur_w
                data["gpu_power_cur_text"] = cur_txt
                data["gpu_power_default_w"] = def_w
                data["gpu_power_default_text"] = def_txt
                data["gpu_power_max_w"] = max_w
                data["gpu_power_max_text"] = max_txt

            meta_src = meta_part or val
            pd_m = re.search(r"nvidia-powerd=([^,)\s]+)", meta_src)
            if pd_m:
                data["nvidia_powerd_state"] = pd_m.group(1).strip()
            ctrl_m = re.search(r"power/control=([^,)\s]+)", meta_src)
            if ctrl_m:
                data["gpu_pci_control"] = ctrl_m.group(1).strip()
            rt_m = re.search(r"runtime=([^,)\s]+)", meta_src)
            if rt_m:
                data["gpu_runtime_status"] = rt_m.group(1).strip()

            tier_src = meta_part or re.sub(r"^\s*\[N/A\]", "", val, flags=re.IGNORECASE)
            tier_m = re.search(r"\[(?!N/A\])([^\]]+)\]", tier_src, re.IGNORECASE)
            if tier_m:
                tier_inner = tier_m.group(1).strip()
                data["gpu_tier_text"] = tier_inner
                data["gpu_tier_label"] = tier_inner.split(",", 1)[0].strip()
            continue

    if data["dry_run"] and data["profile_text"] == "N/A":
        data["profile_text"] = "Dry-Run Preview"
    return data


@dataclass
class FanStatus:
    """Parsed telemetry, fan mode, and 4-node CPU/GPU curves from `fan.sh status`."""

    dry_run: bool = False
    cpu_temp: Optional[int] = None
    ac1_temp: Optional[int] = None
    cpu_vr_temp: Optional[int] = None
    dgpu_temp: Optional[int] = None
    cpu_fan_rpm: Optional[int] = None
    cpu_fan_duty: Optional[int] = None
    cpu_fan_pct: Optional[int] = None
    cpu_fan_raw: Optional[int] = None
    gpu_fan_rpm: Optional[int] = None
    gpu_fan_duty: Optional[int] = None
    gpu_fan_pct: Optional[int] = None
    gpu_fan_raw: Optional[int] = None
    offset_raw: Optional[int] = None
    offset_pct: Optional[int] = None
    fan_mode_key: Optional[str] = None
    fan_mode_text: str = "N/A"
    fan_mode_raw: str = "N/A"
    cpu_curve: list[tuple[int, int]] = field(default_factory=lambda: list(CUSTOM_4PT_WAYPOINTS))
    gpu_curve: list[tuple[int, int]] = field(default_factory=lambda: list(CUSTOM_4PT_WAYPOINTS))
    nvidia_name: Optional[str] = None
    nvidia_temp: Optional[int] = None
    nvidia_power: Optional[str] = None
    nvidia_text: str = "N/A"

    @classmethod
    def _field_names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    def __getitem__(self, key: str) -> Any:
        if key in self._field_names():
            return getattr(self, key)
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._field_names():
            setattr(self, key, value)
            return
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._field_names()

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key) if key in self._field_names() else default

    def keys(self) -> tuple[str, ...]:
        return self._field_names()

    def values(self) -> list[Any]:
        return [getattr(self, k) for k in self._field_names()]

    def items(self) -> list[tuple[str, Any]]:
        return [(k, getattr(self, k)) for k in self._field_names()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._field_names())

    def copy(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self._field_names()}


def parse_fan_status(text: str) -> FanStatus:
    """Parse output of `fan.sh status` into a `FanStatus` dataclass."""
    data = FanStatus(
        dry_run="[dry-run]" in text,
        fan_mode_text="Snapshot (0xD7 skipped)" if "EC Thermal" in text else "N/A",
    )
    mode_hex_to_key = {
        "0x02": "auto",
        "0x04": "custom",
        "0x08": "auto",
        "0x10": "max",
        "0x80": "auto",
    }
    in_nvidia = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "=== NVIDIA GPU ===":
            in_nvidia = True
            continue
        if in_nvidia and line and not line.startswith("==="):
            is_d3cold = "d3cold" in line.lower()
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                data.nvidia_name = parts[0]
                t_match = re.match(r"^(-?\d+)(?:\s*°?C)?$", parts[1])
                if is_d3cold or not t_match:
                    data.nvidia_temp = None
                    temp_disp = parts[1]
                else:
                    data.nvidia_temp = int(t_match.group(1))
                    temp_disp = f"{t_match.group(1)} °C"
                data.nvidia_power = parts[2]
                data.nvidia_text = f"{parts[0]}\n{temp_disp} • {parts[2]}"
            else:
                if is_d3cold:
                    data.nvidia_temp = None
                data.nvidia_text = line
            in_nvidia = False
            continue

        m = re.match(r"^CPU Temp\s*\(0x07\)\s*:\s*(-?\d+)\s*C\s*\(Sensor 0x08:\s*(-?\d+)\s*C\)", line)
        if m:
            data.cpu_temp = int(m.group(1))
            data.ac1_temp = int(m.group(2))
            continue

        m = re.match(r"^CPU VR\s*\(0x0A\)\s*:\s*(-?\d+)\s*C\s*\(dGPU 0x0B\s*:\s*([^)]+)\)", line)
        if m:
            data.cpu_vr_temp = int(m.group(1))
            dgpu_raw = m.group(2).strip()
            dm = re.match(r"^(-?\d+)\s*C$", dgpu_raw)
            data.dgpu_temp = int(dm.group(1)) if dm and "d3cold" not in dgpu_raw.lower() else None
            continue

        m = re.match(
            r"^CPU Fan\s*\(0xCE\)\s*:\s*(\d+)\s*RPM\s*\(duty=\s*(\d+)/255,\s*(\d+)%,\s*raw_tach=(\d+)\)",
            line,
        )
        if m:
            data.cpu_fan_rpm = int(m.group(1))
            data.cpu_fan_duty = int(m.group(2))
            data.cpu_fan_pct = int(m.group(3))
            data.cpu_fan_raw = int(m.group(4))
            continue

        m = re.match(
            r"^GPU Fan\s*\(0xCF\)\s*:\s*(\d+)\s*RPM\s*\(duty=\s*(\d+)/255,\s*(\d+)%,\s*raw_tach=(\d+)\)",
            line,
        )
        if m:
            data.gpu_fan_rpm = int(m.group(1))
            data.gpu_fan_duty = int(m.group(2))
            data.gpu_fan_pct = int(m.group(3))
            data.gpu_fan_raw = int(m.group(4))
            continue

        m = re.match(r"^Fan Offset\s*\(0xE7\)\s*:\s*(\d+)/255\s*\((\d+)%\)", line)
        if m:
            data.offset_raw = int(m.group(1))
            data.offset_pct = int(m.group(2))
            continue

        m = re.match(r"^Fan Mode\s*\(0xD7\)\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            data.fan_mode_text = val
            data.fan_mode_raw = val
            hm = re.search(r"\((0x[0-9A-Fa-f]{2})\b", val)
            if hm:
                data.fan_mode_key = mode_hex_to_key.get(hm.group(1).lower())
            elif data.fan_mode_key is None:
                low = val.lower()
                # Check 0x80 / silent-auto before slow/silent (0x40) so there is no collision
                if "0x80" in low or "silent-auto" in low:
                    data.fan_mode_key = "auto"
                elif "slow" in low or "silent" in low or "noiseless" in low:
                    data.fan_mode_key = None
                elif "custom" in low:
                    data.fan_mode_key = "custom"
                elif "max" in low and "maxq" not in low:
                    data.fan_mode_key = "max"
                elif "auto" in low:
                    data.fan_mode_key = "auto"
            continue

        m = re.match(r"^(CPU|GPU)\s+Curve\s*\([^)]*\)\s*:\s*(.+)$", line)
        if m:
            pts = [
                (int(t), int(d))
                for t, d in re.findall(r"\(\s*(\d+)\s*°?C\s*,\s*(\d+)\s*%\s*\)", m.group(2))
            ]
            if len(pts) == 4:
                if m.group(1) == "CPU":
                    data.cpu_curve = pts
                else:
                    data.gpu_curve = pts
            continue

    if data.dry_run and data.fan_mode_text == "N/A":
        data.fan_mode_text = "Dry-Run Preview"
    return data


@dataclass
class PeripheralsStatus:
    """Parsed webcam, trackpad, lid/wake, lid-close mode, and keyboard locks from `peripherals.sh status`."""

    dry_run: bool = False
    webcam: str = "N/A"
    webcam_bit0: str = "N/A"
    video0: str = "N/A"
    webcam_detail: str = "N/A"
    trackpad: str = "N/A"
    trackpad_bit4: str = "N/A"
    input_inhibited: str = "N/A"
    trackpad_detail: str = "N/A"
    lid_state: str = "N/A"
    ec_03: str = "N/A"
    lste: str = "N/A"
    lid_state_detail: str = "N/A"
    wake_on_lid: str = "N/A"
    lwke: str = "N/A"
    kernel_wakeup: str = "N/A"
    wake_on_lid_detail: str = "N/A"
    lid_close_mode: str = "N/A"
    lid_close_key: Optional[str] = None
    inhibit_pid: str = "none"
    lid_close_detail: str = "N/A"
    win_lock: str = "N/A"
    ec_e2: str = "N/A"
    win_lock_bit4: str = "N/A"
    win_lock_detail: str = "N/A"
    fn_swap: str = "N/A"
    fn_swap_bit3: str = "N/A"
    fn_swap_detail: str = "N/A"
    fn_lock: str = "N/A"
    ec_ec: str = "N/A"
    fn_lock_bit5: str = "N/A"
    fn_lock_detail: str = "N/A"


def parse_peripherals_status(text: str) -> PeripheralsStatus:
    """Parse all 8 lines of `peripherals.sh status`."""
    data = PeripheralsStatus(dry_run="[dry-run]" in text)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("[dry-run]"):
            continue

        m = re.match(r"^Webcam\s*\(0xB8\)\s*:\s*(.+)$", line)
        if m:
            detail = m.group(1).strip()
            data.webcam_detail = detail
            vm = re.match(r"^([^\s(]+)\s*\(bit0=([^,]+),\s*video0=([^)]+)\)", detail)
            if vm:
                data.webcam = vm.group(1).strip()
                data.webcam_bit0 = vm.group(2).strip()
                data.video0 = vm.group(3).strip()
            else:
                data.webcam = detail.split()[0]
            continue

        m = re.match(r"^Trackpad\s*:\s*(.+)$", line)
        if m:
            detail = m.group(1).strip()
            data.trackpad_detail = detail
            tm = re.match(r"^([^\s(]+)\s*\(EC bit4=([^,]+),\s*input_inhibited=([^)]+)\)", detail)
            if tm:
                data.trackpad = tm.group(1).strip()
                data.trackpad_bit4 = tm.group(2).strip()
                data.input_inhibited = tm.group(3).strip()
            else:
                data.trackpad = detail.split()[0]
            continue

        m = re.match(r"^Lid State\s*:\s*(.+)$", line)
        if m:
            detail = m.group(1).strip()
            data.lid_state_detail = detail
            lm = re.match(r"^([^\s(]+)\s*\(EC 0x03=([^,]+),\s*LSTE=([^)]+)\)", detail)
            if lm:
                data.lid_state = lm.group(1).strip()
                data.ec_03 = lm.group(2).strip()
                data.lste = lm.group(3).strip()
            else:
                data.lid_state = detail.split()[0]
            continue

        m = re.match(r"^Wake on Lid\s*:\s*(.+)$", line)
        if m:
            detail = m.group(1).strip()
            data.wake_on_lid_detail = detail
            wm = re.match(r"^([^\s(]+)\s*\(EC LWKE=([^,]+),\s*kernel_wakeup=([^)]+)\)", detail)
            if wm:
                data.wake_on_lid = wm.group(1).strip()
                data.lwke = wm.group(2).strip()
                data.kernel_wakeup = wm.group(3).strip()
            else:
                data.wake_on_lid = detail.split()[0]
            continue

        m = re.match(r"^Lid Close Mode\s*:\s*(.+)$", line)
        if m:
            detail = m.group(1).strip()
            data.lid_close_detail = detail
            cm = re.match(r"^(.+?)\s*\(inhibit_pid=([^)]+)\)\s*$", detail)
            if cm:
                data.lid_close_mode = cm.group(1).strip()
                data.inhibit_pid = cm.group(2).strip()
            else:
                data.lid_close_mode = detail
            mode_low = data.lid_close_mode.lower()
            if "ignore" in mode_low:
                data.lid_close_key = "ignore"
            elif "suspend" in mode_low:
                data.lid_close_key = "suspend"
            continue

        m = re.match(r"^Win-Key Lock\s*:\s*(.+)$", line)
        if m:
            detail = m.group(1).strip()
            data.win_lock_detail = detail
            km = re.match(r"^([^\s(]+)\s*\(EC 0xE2=([^,]+),\s*bit4=([^)]+)\)", detail)
            if km:
                data.win_lock = km.group(1).strip()
                data.ec_e2 = km.group(2).strip()
                data.win_lock_bit4 = km.group(3).strip()
            else:
                data.win_lock = detail.split()[0]
            continue

        m = re.match(r"^Fn/Win Swap\s*:\s*(.+)$", line)
        if m:
            detail = m.group(1).strip()
            data.fn_swap_detail = detail
            sm = re.match(r"^([^\s(]+)\s*\(EC 0xE2=([^,]+),\s*bit3=([^)]+)\)", detail)
            if sm:
                data.fn_swap = sm.group(1).strip()
                if data.ec_e2 == "N/A":
                    data.ec_e2 = sm.group(2).strip()
                data.fn_swap_bit3 = sm.group(3).strip()
            else:
                data.fn_swap = detail.split()[0]
            continue

        m = re.match(r"^Fn-Lock\s*:\s*(.+)$", line)
        if m:
            detail = m.group(1).strip()
            data.fn_lock_detail = detail
            fm = re.match(r"^([^\s(]+)\s*\(EC 0xEC=([^,]+),\s*bit5=([^)]+)\)", detail)
            if fm:
                data.fn_lock = fm.group(1).strip()
                data.ec_ec = fm.group(2).strip()
                data.fn_lock_bit5 = fm.group(3).strip()
            else:
                data.fn_lock = detail.split()[0]
            continue

    return data


def parse_brightness_status(text: str) -> dict[str, Any]:
    """Parse output of `brightness.sh status`."""
    data: dict[str, Any] = {
        "dry_run": "[dry-run]" in text,
        "cached_text": "(none recorded yet)",
        "level": None,
        "mode_text": None,
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = re.match(r"^Cached state \(KBType=6 RGB\):\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            data["cached_text"] = val
            lm = re.search(r"brightness=(\d+)", val)
            if lm:
                data["level"] = max(0, min(255, int(lm.group(1))))
            continue

        m = re.match(r"^Mode\s*\(KBType=6\)\s*:\s*(.+)$", line)
        if m:
            data["mode_text"] = m.group(1).strip()
    return data


def parse_rgb_status(text: str) -> dict[str, Any]:
    """Parse output of `rgb.sh status`."""
    data: dict[str, Any] = {
        "dry_run": "[dry-run]" in text,
        "target_mode": None,
        "cached_text": "(none recorded yet)",
        "rgb": None,
        "cc_scale": None,
        "effect": None,
        "timeout": None,
        "mailbox_text": None,
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = re.match(r"^Target mode\s*:\s*(.+)$", line)
        if m:
            data["target_mode"] = m.group(1).strip()
            continue

        m = re.match(r"^Cached state\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            data["cached_text"] = val
            cm = re.search(r"color=\((\d+),(\d+),(\d+)\)", val)
            if cm:
                data["rgb"] = (
                    max(0, min(255, int(cm.group(1)))),
                    max(0, min(255, int(cm.group(2)))),
                    max(0, min(255, int(cm.group(3)))),
                )
            if "cc_scale=1" in val:
                data["cc_scale"] = True
            elif cm:
                data["cc_scale"] = False
            em = re.search(r"effect=(\S+)", val)
            if em:
                data["effect"] = em.group(1)
            tm = re.search(r"timeout=(\S+)", val)
            if tm:
                data["timeout"] = tm.group(1)
            continue

        m = re.match(r"^EC Mailbox\s*:\s*(.+)$", line)
        if m:
            data["mailbox_text"] = m.group(1).strip()
    return data


def parse_battery_status(text: str) -> dict[str, Any]:
    """Parse output of `battery.sh status`."""
    data: dict[str, Any] = {
        "dry_run": "[dry-run]" in text,
        "sysfs_status": None,
        "sysfs_capacity": None,
        "sysfs_text": "N/A",
        "ac_online": None,
        "bat_present": None,
        "ec_remaining": None,
        "ec_cycles": None,
        "ec_power_text": "N/A",
        "f1_text": "N/A",
        "f1_enabled": None,
        "f1_start": None,
        "f1_stop": None,
        "f1_raw": None,
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = re.match(r"^Linux sysfs\s*:\s*status=([^,]+),\s*capacity=(\d+)%", line)
        if m:
            data["sysfs_status"] = m.group(1).strip()
            data["sysfs_capacity"] = int(m.group(2))
            data["sysfs_text"] = f"{data['sysfs_status']} • {data['sysfs_capacity']}%"
            continue

        m = re.match(
            r"^EC Power\s*:\s*AC=(\d+),\s*BAT0=(\d+),\s*Remaining=(\d+)%,\s*Cycles=(\d+)",
            line,
        )
        if m:
            data["ac_online"] = int(m.group(1))
            data["bat_present"] = int(m.group(2))
            data["ec_remaining"] = int(m.group(3))
            data["ec_cycles"] = int(m.group(4))
            ac_str = "Plugged" if data["ac_online"] else "On Battery"
            bat_str = "Present" if data["bat_present"] else "Absent"
            data["ec_power_text"] = (
                f"AC: {ac_str} • BAT0: {bat_str}\n"
                f"Remaining: {data['ec_remaining']}% • Cycles: {data['ec_cycles']}"
            )
            continue

        m = re.match(r"^(?:FlexiCharger|EC FCMD 0xF1)\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            data["f1_text"] = val
            pipe_m = re.search(r"\|\s*([^)]+)\)", val)
            if pipe_m:
                data["f1_raw"] = pipe_m.group(1).strip()
            else:
                raw_m = re.search(r"\[(?:raw:\s*)?([^\]]+)\]", val, re.IGNORECASE)
                if raw_m:
                    data["f1_raw"] = raw_m.group(1).strip()

            start_m = re.search(r"\bstart\s*=\s*(\d+)%", val, re.IGNORECASE)
            stop_m = re.search(r"\bstop\s*=\s*(\d+)%", val, re.IGNORECASE)

            if re.match(r"^Enabled\b", val, re.IGNORECASE):
                data["f1_enabled"] = True
                if start_m:
                    data["f1_start"] = int(start_m.group(1))
                if stop_m:
                    data["f1_stop"] = int(stop_m.group(1))
                else:
                    lim_m = re.search(r"\b(\d+)%\s+limit\b", val, re.IGNORECASE)
                    if lim_m:
                        data["f1_stop"] = int(lim_m.group(1))
            elif re.match(r"^Disabled\b", val, re.IGNORECASE):
                data["f1_enabled"] = False
                data["f1_start"] = None
                if start_m:
                    start_val = int(start_m.group(1))
                    if 1 <= start_val <= 99:
                        data["f1_start"] = start_val
                data["f1_stop"] = int(stop_m.group(1)) if stop_m else 100
            elif re.match(r"^Ready\b", val, re.IGNORECASE):
                data["f1_enabled"] = None
                data["f1_start"] = None
                data["f1_stop"] = None
            elif re.match(r"^Unknown\b", val, re.IGNORECASE):
                data["f1_enabled"] = None
                if start_m:
                    data["f1_start"] = int(start_m.group(1))
                if stop_m:
                    data["f1_stop"] = int(stop_m.group(1))
            elif "unavailable" in val.lower() or "snapshot mode" in val.lower():
                data["f1_enabled"] = None
                data["f1_start"] = None
                data["f1_stop"] = None
            continue
    return data


# ---------------------------------------------------------------------------
# Persistent Root Worker Mode (--worker)
# ---------------------------------------------------------------------------

def _chown_cache_tree(cache_dir: Optional[str], uid: Optional[int], gid: Optional[int]) -> None:
    """Recursively restore ownership of <cache_dir>/acer-a715-59g to uid:gid."""
    if not cache_dir or uid is None or gid is None:
        return
    target = os.path.join(cache_dir, "acer-a715-59g")
    if not os.path.exists(target):
        return
    try:
        os.chown(target, uid, gid, follow_symlinks=False)
        for root, dirs, files in os.walk(target):
            for name in dirs:
                os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)
            for name in files:
                os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)
    except OSError:
        pass


def _emit_worker_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run_worker(cache_dir: Optional[str], uid: Optional[int], gid: Optional[int]) -> int:
    """Persistent privileged worker reading single-line JSON requests from stdin."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        req_id: Any = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("Request must be a JSON object.")
            req_id = req.get("id")

            if req.get("shutdown"):
                _emit_worker_response({"id": req_id, "rc": 0, "stdout": "", "stderr": ""})
                return 0

            if req.get("ping"):
                _emit_worker_response({"id": req_id, "rc": 0, "stdout": "pong", "stderr": ""})
                continue

            script = req.get("script")
            args = req.get("args", [])
            if not isinstance(script, str) or script not in ALLOWED_SCRIPTS:
                raise ValueError(f"Disallowed or invalid script: {script!r}")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ValueError("Field 'args' must be a list of strings.")

            env = os.environ.copy()
            if cache_dir:
                env["XDG_CACHE_HOME"] = cache_dir

            script_path = os.path.join(SCRIPT_DIR, script)
            proc = subprocess.run(
                [script_path, *args],
                cwd=SCRIPT_DIR,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            _chown_cache_tree(cache_dir, uid, gid)
            _emit_worker_response({
                "id": req_id,
                "rc": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            })
        except Exception as exc:  # noqa: BLE001
            _emit_worker_response({
                "id": req_id,
                "rc": 1,
                "stdout": "",
                "stderr": f"Worker error: {exc}",
            })
    return 0


# ---------------------------------------------------------------------------
# Script Execution Engine
# ---------------------------------------------------------------------------

@dataclass
class ScriptResult:
    script: str
    args: list[str]
    effective_cmd: list[str]
    mode: str  # "root" | "worker" | "daemon" | "dry-run" | "ec-file" | "snapshot-dry-run" | "error"
    rc: int
    stdout: str
    stderr: str
    note: str = ""
    duration_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApplyConfigResult:
    config_path: str
    mode: str  # "daemon-apply" | "daemon-replay" | "root" | "worker" | "dry-run" | "snapshot-dry-run" | "skipped" | "noop" | "error"
    rc: int
    skipped: bool = False
    applied: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    message: str = ""
    config: dict[str, Any] = field(default_factory=dict)


class ScriptRunner:
    """Thread-safe executor supporting root, pkexec worker, daemon socket, dry-run, and ec-file fallback."""

    def __init__(
        self,
        dry_run: bool = False,
        explicit_ec_file: Optional[str] = None,
        cache_dir: Optional[str] = None,
        daemon_socket_path: Optional[str] = None,
        config_path: Optional[str] = None,
    ) -> None:
        self.is_root: bool = os.geteuid() == 0
        self.dry_run: bool = dry_run
        self.explicit_ec_file: Optional[str] = explicit_ec_file
        self.auto_ec_file: Optional[str] = (
            DEFAULT_EC_SNAPSHOT
            if os.path.isfile(DEFAULT_EC_SNAPSHOT) and os.access(DEFAULT_EC_SNAPSHOT, os.R_OK)
            else None
        )
        self.cache_dir: str = cache_dir or (
            os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        )
        self._explicit_config_path: Optional[str] = (
            str(config_path).strip() if config_path is not None and str(config_path).strip() else None
        )
        self.config_path: str = str(
            config_store.resolve_config_path(
                explicit=self._explicit_config_path,
                cache_dir=self.cache_dir,
            )
        )
        env_sock = os.environ.get("ACER_CONTROL_SOCK")
        self.daemon_socket_path: str = (
            daemon_socket_path
            if daemon_socket_path is not None
            else (env_sock if env_sock is not None else DEFAULT_DAEMON_SOCKET)
        )
        self._daemon_euid: Optional[int] = None
        self._daemon_version: Optional[str] = None
        self._worker_proc: Optional[subprocess.Popen[str]] = None
        self._starting_proc: Optional[subprocess.Popen[str]] = None
        self._state_lock = threading.Lock()
        self._worker_io_lock = threading.Lock()
        self._exec_lock = threading.Lock()
        self._next_id = 1

    def _alloc_id(self) -> int:
        with self._state_lock:
            req_id = self._next_id
            self._next_id += 1
            return req_id

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["XDG_CACHE_HOME"] = self.cache_dir
        return env

    @staticmethod
    def _close_proc(proc: subprocess.Popen[str], terminate: bool = False) -> None:
        try:
            if terminate and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
            elif proc.poll() is None:
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        except OSError:
            pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def effective_ec_file(self) -> Optional[str]:
        return self.explicit_ec_file or self.auto_ec_file

    def worker_alive(self) -> bool:
        with self._state_lock:
            proc = self._worker_proc
            if proc is None:
                return False
            if proc.poll() is not None:
                self._worker_proc = None
                return False
            return True

    def _socket_exchange(self, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        sock_path = self.daemon_socket_path
        if not sock_path:
            raise FileNotFoundError("Daemon socket path is empty.")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(sock_path)
            raw_req = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            client.sendall(raw_req)
            buf = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    if not buf:
                        raise BrokenPipeError("Daemon closed socket without responding.")
                    line = bytes(buf).decode("utf-8", errors="replace").strip()
                    break
                buf.extend(chunk)
                nl = buf.find(b"\n")
                if nl != -1:
                    line = bytes(buf[:nl]).decode("utf-8", errors="replace").strip()
                    break
        resp = json.loads(line)
        if not isinstance(resp, dict):
            raise ValueError("Daemon returned non-object JSON response.")
        return resp

    def ping_daemon(self, timeout: float = 0.4) -> tuple[bool, str]:
        """Probe the privileged UNIX domain socket daemon via JSON ping."""
        sock_path = self.daemon_socket_path
        if not sock_path or not os.path.exists(sock_path):
            self._daemon_euid = None
            self._daemon_version = None
            return (False, f"Daemon socket absent: {sock_path or '(disabled)'}")
        try:
            resp = self._socket_exchange({"id": 0, "ping": True}, timeout=timeout)
        except (OSError, ValueError, TimeoutError) as exc:
            self._daemon_euid = None
            self._daemon_version = None
            return (False, f"Daemon ping failed: {exc}")

        if resp.get("rc") == 0 and resp.get("stdout") == "pong":
            euid_val = resp.get("euid")
            ver_val = resp.get("version")
            self._daemon_euid = int(euid_val) if isinstance(euid_val, int) else None
            self._daemon_version = str(ver_val) if ver_val is not None else None
            return (
                True,
                f"pong (euid={self._daemon_euid}, version={self._daemon_version or 'unknown'})",
            )
        self._daemon_euid = None
        self._daemon_version = None
        return (False, str(resp.get("stderr") or "Unexpected daemon ping response."))

    def daemon_available(self) -> bool:
        """Return True if the control daemon UNIX socket responds to ping."""
        ok, _detail = self.ping_daemon(timeout=0.4)
        return ok

    def _call_daemon(self, script: str, args: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
        req_id = self._alloc_id()
        resp = self._socket_exchange({"id": req_id, "script": script, "args": args}, timeout=timeout)
        return int(resp.get("rc", 1)), str(resp.get("stdout", "")), str(resp.get("stderr", ""))

    def is_live_ec(self) -> bool:
        if self.dry_run or self.explicit_ec_file:
            return False
        return self.is_root or self.worker_alive() or self.daemon_available()

    def is_live_root(self) -> bool:
        return self.is_live_ec()

    def mode_label(self) -> str:
        """Return short active execution mode identifier."""
        if self.dry_run:
            return "dry-run"
        if self.is_root:
            return "root"
        if self.worker_alive():
            return "worker"
        if not self.explicit_ec_file and self.daemon_available():
            return "daemon"
        if self.effective_ec_file():
            return "ec-file"
        return "locked"

    def current_mode_label(self) -> tuple[str, str]:
        """Return (mode_kind, human_description)."""
        if self.dry_run:
            return ("dry-run", "Dry-Run Mode — printing EC/RAPL commands without touching hardware")
        if self.is_root:
            return ("live", "Live Root EC (Direct UID 0) — hardware reads/writes active")
        if self.worker_alive():
            return ("live", "Live Root EC (pkexec Worker Unlocked) — hardware reads/writes active")
        if not self.explicit_ec_file and self.daemon_available():
            euid_info = f", euid={self._daemon_euid}" if self._daemon_euid is not None else ""
            return ("live", f"Daemon Connected (Live EC{euid_info}) — hardware reads/writes active")
        ec_file = self.effective_ec_file()
        if ec_file:
            return (
                "snapshot",
                f"Snapshot Mode ({os.path.basename(ec_file)}) — read-only snapshot; setters preview via --dry-run",
            )
        return (
            "snapshot",
            "Unprivileged Locked Mode — no snapshot file; commands preview via --dry-run",
        )

    def start_worker(self, use_pkexec: bool = True) -> tuple[bool, str]:
        """Spawn persistent root worker via pkexec (or local subprocess for tests)."""
        if self.is_root:
            return (True, "Already running as root.")

        # Ensure cache_dir exists with user ownership before privileged worker runs mkdir -p.
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except OSError:
            pass

        with self._state_lock:
            if self._worker_proc is not None and self._worker_proc.poll() is None:
                return (True, "Root worker is already active.")
            if self._starting_proc is not None and self._starting_proc.poll() is None:
                return (False, "Root worker startup is already in progress.")

        cmd = [
            "/usr/bin/python3",
            os.path.join(SCRIPT_DIR, "gui.py"),
            "--worker",
            "--cache-dir",
            self.cache_dir,
            "--uid",
            str(os.getuid()),
            "--gid",
            str(os.getgid()),
        ]
        if use_pkexec:
            cmd = ["pkexec", *cmd]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=SCRIPT_DIR,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            return (False, f"Failed to start root worker: {exc}")

        with self._state_lock:
            self._starting_proc = proc

        try:
            assert proc.stdin is not None and proc.stdout is not None
            req_id = self._alloc_id()
            try:
                proc.stdin.write(json.dumps({"id": req_id, "ping": True}) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except BrokenPipeError:
                line = ""

            if not line:
                err_out = ""
                if proc.stderr is not None:
                    try:
                        err_out = proc.stderr.read().strip()
                    except OSError:
                        err_out = ""
                try:
                    rc = proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._close_proc(proc, terminate=True)
                    return (False, err_out or "pkexec/worker exited without responding.")
                self._close_proc(proc, terminate=False)
                if rc == 126:
                    msg = "pkexec authentication cancelled by user (rc=126)."
                    if err_out:
                        msg = f"{msg} [{err_out}]"
                elif rc == 127:
                    msg = "pkexec authorization failed or polkit agent unavailable (rc=127)."
                    if err_out:
                        msg = f"{msg} [{err_out}]"
                else:
                    msg = err_out or f"pkexec/worker exited before handshake (rc={rc})."
                return (False, msg)

            resp = json.loads(line)
            if not isinstance(resp, dict) or resp.get("rc") != 0:
                err_detail = resp.get("stderr") if isinstance(resp, dict) else "Invalid worker response."
                self._close_proc(proc, terminate=True)
                return (False, str(err_detail or "Worker handshake failed."))

            with self._state_lock:
                if self._starting_proc is not proc:
                    self._close_proc(proc, terminate=True)
                    return (False, "Root worker startup was cancelled.")
                self._starting_proc = None
                self._worker_proc = proc
            return (True, "Live EC root worker unlocked and ready.")
        except Exception as exc:  # noqa: BLE001
            self._close_proc(proc, terminate=True)
            return (False, f"Worker handshake error: {exc}")
        finally:
            with self._state_lock:
                if self._starting_proc is proc:
                    self._starting_proc = None

    def stop_worker(self) -> None:
        """Gracefully shut down persistent root worker (and cancel pending startup)."""
        with self._state_lock:
            proc = self._worker_proc
            self._worker_proc = None
            starting = self._starting_proc
            self._starting_proc = None

        if starting is not None:
            self._close_proc(starting, terminate=True)

        if proc is None:
            return

        with self._worker_io_lock:
            try:
                if proc.poll() is None and proc.stdin is not None and proc.stdout is not None:
                    req_id = self._alloc_id()
                    proc.stdin.write(json.dumps({"id": req_id, "shutdown": True}) + "\n")
                    proc.stdin.flush()
                    proc.stdout.readline()
                proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                self._close_proc(proc, terminate=True)
                return
            self._close_proc(proc, terminate=False)

    def _call_worker(self, script: str, args: list[str]) -> tuple[int, str, str]:
        with self._worker_io_lock:
            with self._state_lock:
                proc = self._worker_proc
                if proc is None or proc.poll() is not None or proc.stdin is None or proc.stdout is None:
                    self._worker_proc = None
                    raise RuntimeError("Root worker is not running.")
            req_id = self._alloc_id()
            req = {"id": req_id, "script": script, "args": args}
            try:
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    err = ""
                    if proc.stderr is not None:
                        try:
                            err = proc.stderr.read().strip()
                        except OSError:
                            err = ""
                    with self._state_lock:
                        if self._worker_proc is proc:
                            self._worker_proc = None
                    self._close_proc(proc, terminate=True)
                    raise RuntimeError(err or "Root worker closed stdout unexpectedly.")
                resp = json.loads(line)
                if not isinstance(resp, dict):
                    raise ValueError("Worker returned non-object JSON response.")
                return int(resp.get("rc", 1)), str(resp.get("stdout", "")), str(resp.get("stderr", ""))
            except (OSError, ValueError) as exc:
                with self._state_lock:
                    if self._worker_proc is proc:
                        self._worker_proc = None
                self._close_proc(proc, terminate=True)
                raise RuntimeError(f"Root worker pipe error: {exc}") from exc

    @staticmethod
    def _is_status_call(args: list[str]) -> bool:
        return len(args) == 0 or (len(args) == 1 and args[0].lower() in ("status", "get"))

    def run_script(self, script: str, args: list[str]) -> ScriptResult:
        """Execute an allowed control script using the active privilege/fallback mode."""
        if script not in ALLOWED_SCRIPTS:
            raise ValueError(f"Script {script!r} is not in ALLOWED_SCRIPTS.")
        if not all(isinstance(a, str) for a in args):
            raise TypeError("All script arguments must be strings.")

        t0 = time.monotonic()
        script_path = os.path.join(SCRIPT_DIR, script)
        env = self._subprocess_env()

        with self._exec_lock:
            # 1. Explicit Dry-Run Mode applies cleanly to all 5 scripts
            if self.dry_run:
                eff_args = list(args)
                if "--dry-run" not in eff_args:
                    eff_args.insert(0, "--dry-run")
                cmd = [script_path, *eff_args]
                proc = subprocess.run(
                    cmd, cwd=SCRIPT_DIR, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
                )
                return ScriptResult(
                    script=script,
                    args=list(args),
                    effective_cmd=cmd,
                    mode="dry-run",
                    rc=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            # 2. Running directly as root (euid == 0)
            if self.is_root:
                cmd = [script_path, *args]
                proc = subprocess.run(
                    cmd, cwd=SCRIPT_DIR, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
                )
                return ScriptResult(
                    script=script,
                    args=list(args),
                    effective_cmd=cmd,
                    mode="root",
                    rc=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            # 3. Persistent pkexec root worker unlocked
            if self.worker_alive():
                try:
                    rc, out, err = self._call_worker(script, list(args))
                    return ScriptResult(
                        script=script,
                        args=list(args),
                        effective_cmd=["pkexec-worker", script, *args],
                        mode="worker",
                        rc=rc,
                        stdout=out,
                        stderr=err,
                        duration_ms=(time.monotonic() - t0) * 1000.0,
                    )
                except RuntimeError as exc:
                    return ScriptResult(
                        script=script,
                        args=list(args),
                        effective_cmd=["pkexec-worker", script, *args],
                        mode="error",
                        rc=1,
                        stdout="",
                        stderr=str(exc),
                        note="Root worker disconnected — click 'Unlock Live EC (pkexec)' to reconnect.",
                        duration_ms=(time.monotonic() - t0) * 1000.0,
                    )

            # 3b. Privileged EC/ACPI UNIX domain socket daemon (when explicit --ec-file is not forced)
            daemon_drop_note = ""
            if not self.explicit_ec_file and self.daemon_available():
                try:
                    rc, out, err = self._call_daemon(script, list(args))
                    return ScriptResult(
                        script=script,
                        args=list(args),
                        effective_cmd=["daemon", script, *args],
                        mode="daemon",
                        rc=rc,
                        stdout=out,
                        stderr=err,
                        duration_ms=(time.monotonic() - t0) * 1000.0,
                    )
                except (
                    ConnectionRefusedError,
                    BrokenPipeError,
                    TimeoutError,
                    ConnectionResetError,
                    OSError,
                    ValueError,
                ) as exc:
                    self._daemon_euid = None
                    self._daemon_version = None
                    daemon_drop_note = (
                        f"Daemon socket connection dropped ({exc}); fell back to snapshot/locked mode."
                    )

            # 4. Snapshot / Unprivileged Fallback Mode
            is_status = self._is_status_call(args)
            ec_file = self.effective_ec_file()
            if is_status and script in EC_FILE_SCRIPTS and ec_file:
                eff_args = ["--ec-file", ec_file, *args]
                cmd = [script_path, *eff_args]
                proc = subprocess.run(
                    cmd, cwd=SCRIPT_DIR, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
                )
                return ScriptResult(
                    script=script,
                    args=list(args),
                    effective_cmd=cmd,
                    mode="ec-file",
                    rc=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    note=daemon_drop_note,
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            # Clean --dry-run fallback for brightness.sh/rgb.sh status or locked setter actions
            eff_args = list(args)
            if "--dry-run" not in eff_args:
                eff_args.insert(0, "--dry-run")
            cmd = [script_path, *eff_args]
            proc = subprocess.run(
                cmd, cwd=SCRIPT_DIR, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
            )
            note = (
                "[DRY-RUN FALLBACK - Unlock Live EC to apply to hardware] Previewed with --dry-run; hardware NOT modified."
                if not is_status
                else "Snapshot fallback: using --dry-run (script does not accept --ec-file)."
            )
            if daemon_drop_note:
                note = f"{daemon_drop_note} {note}"
            return ScriptResult(
                script=script,
                args=list(args),
                effective_cmd=cmd,
                mode="snapshot-dry-run",
                rc=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                note=note,
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    def resolve_target_config_path(self, config_path: Optional[str] = None) -> str:
        """Resolve an explicit config_path override or fall back to self.config_path."""
        if config_path is not None and str(config_path).strip():
            return str(
                config_store.resolve_config_path(
                    explicit=config_path,
                    cache_dir=self.cache_dir,
                )
            )
        return self.config_path

    def load_saved_config(
        self,
        config_path: Optional[str] = None,
        ensure: bool = False,
    ) -> dict[str, Any]:
        """Load (and optionally initialize) config.json cleanly."""
        target = self.resolve_target_config_path(config_path)
        uid = os.getuid()
        gid = os.getgid()
        if ensure:
            return config_store.ensure_config_file(target, cache_dir=self.cache_dir, uid=uid, gid=gid)
        return config_store.load_config(target, uid=uid, gid=gid)

    def record_action(
        self,
        script: str,
        args: Sequence[str],
        config_path: Optional[str] = None,
    ) -> bool:
        """Persist a successful hardware action into config.json."""
        target = self.resolve_target_config_path(config_path)
        return config_store.record_action_file(
            target,
            script,
            list(args),
            uid=os.getuid(),
            gid=os.getgid(),
            cache_dir=self.cache_dir,
        )

    def _daemon_matches_config_path(self, target_path: str) -> Optional[bool]:
        """Return True/False if daemon reports config_path via get_config, or None if unsupported."""
        req_id = self._alloc_id()
        resp = self._socket_exchange({"id": req_id, "get_config": True}, timeout=5.0)
        daemon_cfg = resp.get("config_path")
        if resp.get("rc") == 0 and isinstance(daemon_cfg, str) and daemon_cfg.strip():
            return os.path.realpath(daemon_cfg) == os.path.realpath(target_path)
        return None

    def set_auto_apply_on_launch(
        self,
        enabled: bool,
        config_path: Optional[str] = None,
    ) -> tuple[bool, dict[str, Any], str]:
        """Persist auto_apply_on_launch via daemon set_config (when aligned) or local save_config."""
        target = self.resolve_target_config_path(config_path)
        uid = os.getuid()
        gid = os.getgid()
        if (not self.dry_run) and (not self.explicit_ec_file) and self.daemon_available():
            try:
                if self._daemon_matches_config_path(target) is True:
                    req_id = self._alloc_id()
                    resp = self._socket_exchange(
                        {"id": req_id, "set_config": {"auto_apply_on_launch": bool(enabled)}},
                        timeout=5.0,
                    )
                    if resp.get("rc") == 0 and isinstance(resp.get("config"), dict):
                        return (True, dict(resp["config"]), "daemon")
            except (OSError, ValueError, TimeoutError):
                self._daemon_euid = None
                self._daemon_version = None

        cfg = config_store.ensure_config_file(target, cache_dir=self.cache_dir, uid=uid, gid=gid)
        cfg["auto_apply_on_launch"] = bool(enabled)
        saved = config_store.save_config(target, cfg, uid=uid, gid=gid)
        return (True, saved, "local")

    def _replay_planned_actions(
        self,
        planned: list[tuple[str, list[str]]],
        target: str,
        cfg: dict[str, Any],
        mode_override: Optional[str] = None,
    ) -> ApplyConfigResult:
        """Sequentially execute planned config actions via run_script."""
        applied: list[dict[str, Any]] = []
        errors: list[str] = []
        last_mode = mode_override or self.mode_label()
        for script, act_args in planned:
            res = self.run_script(script, list(act_args))
            applied.append(
                {
                    "script": script,
                    "args": list(act_args),
                    "rc": res.rc,
                    "mode": res.mode,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                }
            )
            if not mode_override:
                last_mode = res.mode
            if res.rc != 0 or res.mode == "error":
                detail = res.stderr.strip() or res.stdout.strip() or res.note or f"rc={res.rc}"
                errors.append(f"{script} {' '.join(act_args)}: {detail}")
        summary = f"Applied {len(applied)}/{len(planned)} action(s) via {last_mode}."
        if errors:
            summary = f"{summary} ({len(errors)} error(s))"
        return ApplyConfigResult(
            config_path=target,
            mode=last_mode,
            rc=0 if not errors else 1,
            skipped=False,
            applied=applied,
            errors=errors,
            message=summary,
            config=cfg,
        )

    def apply_saved_config(
        self,
        config_path: Optional[str] = None,
        force: bool = False,
    ) -> ApplyConfigResult:
        """Apply saved config.json via daemon IPC (with older-daemon replay fallback), root, or worker."""
        target = self.resolve_target_config_path(config_path)
        cfg = self.load_saved_config(config_path=target, ensure=True)

        if not force and not cfg.get("auto_apply_on_launch", True):
            return ApplyConfigResult(
                config_path=target,
                mode="skipped",
                rc=0,
                skipped=True,
                message="Auto-apply skipped (auto_apply_on_launch=false in config).",
                config=cfg,
            )

        planned = config_store.plan_apply_actions(cfg, force=force)
        if not planned:
            return ApplyConfigResult(
                config_path=target,
                mode="noop",
                rc=0,
                skipped=False,
                message="No saved settings configured to apply.",
                config=cfg,
            )

        if self.dry_run:
            if not force:
                return ApplyConfigResult(
                    config_path=target,
                    mode="skipped",
                    rc=0,
                    skipped=True,
                    message="Auto-apply skipped in dry-run mode.",
                    config=cfg,
                )
            return self._replay_planned_actions(planned, target, cfg, mode_override="dry-run")

        if (not self.explicit_ec_file) and self.daemon_available():
            try:
                same_cfg = self._daemon_matches_config_path(target)
                if same_cfg is not False:
                    req_id = self._alloc_id()
                    resp = self._socket_exchange(
                        {
                            "id": req_id,
                            "cmd": "apply_config",
                            "apply_config": {"force": bool(force), "dry_run": False},
                            "force": bool(force),
                        },
                        timeout=30.0,
                    )
                    if isinstance(resp.get("applied"), list):
                        applied_items = list(resp["applied"])
                        errors = [str(e) for e in (resp.get("errors") or [])]
                        rc = int(resp.get("rc", 0 if not errors else 1))
                        msg = (
                            f"Daemon applied {len(applied_items)} action(s) from {os.path.basename(target)}."
                            if not errors
                            else f"Daemon applied {len(applied_items)} action(s) with {len(errors)} error(s)."
                        )
                        return ApplyConfigResult(
                            config_path=target,
                            mode="daemon-apply",
                            rc=rc,
                            skipped=False,
                            applied=applied_items,
                            errors=errors,
                            message=msg,
                            config=cfg,
                        )
                # Older daemon rejecting non-"script" requests or daemon with a different config_path:
                return self._replay_planned_actions(planned, target, cfg, mode_override="daemon-replay")
            except (OSError, ValueError, TimeoutError) as exc:
                self._daemon_euid = None
                self._daemon_version = None
                if not force and not self.is_root and not self.worker_alive():
                    return ApplyConfigResult(
                        config_path=target,
                        mode="skipped",
                        rc=0,
                        skipped=True,
                        message=f"Auto-apply skipped: daemon communication failed ({exc}).",
                        config=cfg,
                    )

        if self.is_root:
            return self._replay_planned_actions(planned, target, cfg, mode_override="root")

        if self.worker_alive():
            return self._replay_planned_actions(planned, target, cfg, mode_override="worker")

        if not force:
            return ApplyConfigResult(
                config_path=target,
                mode="skipped",
                rc=0,
                skipped=True,
                message=(
                    "Auto-apply skipped: unprivileged mode without daemon/worker "
                    "(avoiding multiple pkexec prompts on startup)."
                ),
                config=cfg,
            )

        return self._replay_planned_actions(planned, target, cfg, mode_override="snapshot-dry-run")


# ---------------------------------------------------------------------------
# Modern Dark CSS Styling
# ---------------------------------------------------------------------------

APP_CSS = """
window.acer-cc-window {
    background-color: #191d24;
    color: #e6ebf2;
}

headerbar.acer-header {
    background: #14181f;
    border-bottom: 1px solid #2b3240;
    color: #eef2f7;
}

.mode-banner {
    background-color: #202632;
    border: 1px solid #303949;
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 12px;
}

.mode-live {
    background-color: rgba(34, 155, 92, 0.16);
    border-color: rgba(46, 204, 113, 0.55);
    color: #9ff0bf;
}

.mode-dry {
    background-color: rgba(217, 138, 30, 0.16);
    border-color: rgba(243, 156, 18, 0.55);
    color: #ffd992;
}

.mode-snapshot {
    background-color: rgba(58, 123, 213, 0.15);
    border-color: rgba(78, 154, 241, 0.45);
    color: #b7d7ff;
}

.mode-warn {
    background-color: rgba(217, 68, 68, 0.18);
    border-color: rgba(235, 92, 92, 0.65);
    color: #ffb8b8;
}

notebook > header {
    background-color: #161a21;
    border-bottom: 1px solid #2b3240;
}

notebook > header > tabs > tab {
    padding: 8px 16px;
    color: #9ba7b8;
    font-weight: 600;
    border-radius: 6px 6px 0 0;
}

notebook > header > tabs > tab:checked {
    color: #ffffff;
    background-color: #232934;
    border-bottom: 2px solid #4d94ff;
}

notebook > stack {
    background-color: #191d24;
}

.section-box {
    background-color: #20252f;
    border: 1px solid #2f3746;
    border-radius: 10px;
    padding: 12px 14px;
}

.section-title {
    font-weight: 700;
    font-size: 13px;
    color: #c8d3e3;
}

.card {
    background-color: #242a36;
    border: 1px solid #343d4e;
    border-radius: 10px;
    padding: 10px 12px;
}

.card-title {
    font-size: 11px;
    font-weight: 700;
    color: #8c99ad;
    letter-spacing: 0.4px;
}

.card-value {
    font-size: 14px;
    font-weight: 700;
    color: #f0f4fa;
}

.card-sub {
    font-size: 11px;
    color: #95a2b8;
}

button.choice-btn {
    background-image: none;
    background-color: #29303e;
    border: 1px solid #3c4659;
    border-radius: 8px;
    color: #dce3ee;
    padding: 8px 12px;
    font-weight: 600;
}

button.choice-btn:hover {
    background-color: #323b4d;
    border-color: #556480;
}

button.choice-btn.active-choice {
    background-image: none;
    background-color: #245ec9;
    border-color: #5b9bff;
    color: #ffffff;
}

button.primary-action {
    background-image: none;
    background-color: #2364d2;
    border: 1px solid #4e8df5;
    border-radius: 8px;
    color: #ffffff;
    font-weight: 700;
    padding: 7px 14px;
}

button.primary-action:hover {
    background-color: #2e73e6;
}

button.unlock-btn {
    border-radius: 7px;
    font-weight: 600;
    padding: 5px 11px;
}

button.unlock-btn.unlocked {
    background-image: none;
    background-color: #1f7a49;
    border-color: #35ba73;
    color: #ffffff;
}

button.color-chip {
    background-image: none;
    background-color: #262c38;
    border: 1px solid #3b4457;
    border-radius: 14px;
    padding: 4px 10px;
    color: #e6ebf2;
    font-size: 12px;
    font-weight: 600;
}

button.color-chip:hover {
    background-color: #31394a;
    border-color: #6b7d9e;
}

progressbar trough {
    min-height: 10px;
    border-radius: 6px;
    background-color: #151921;
    border: 1px solid #2d3544;
}

progressbar progress {
    min-height: 10px;
    border-radius: 6px;
    background-color: #3e8ef7;
}

progressbar.temp-warm progress {
    background-color: #e69524;
}

progressbar.temp-hot progress {
    background-color: #e04f4f;
}

progressbar.fan-bar progress {
    background-color: #2fb888;
}

textview.cmd-log, textview.cmd-log text {
    background-color: #12151b;
    color: #cfd8e3;
    font-family: monospace;
    font-size: 11.5px;
}

.warn-label {
    color: #ff8e8e;
    font-weight: 600;
    font-size: 12px;
}

.cpu-accent {
    color: #38bdf8;
    font-weight: 700;
}

.gpu-accent {
    color: #34d399;
    font-weight: 700;
}

.lid-badge-open {
    color: #86efac;
    font-weight: 700;
}

.lid-badge-closed {
    color: #fcd34d;
    font-weight: 700;
}
"""


def apply_css() -> None:
    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(APP_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


# ---------------------------------------------------------------------------
# Pure-Python SVG -> GdkPixbuf Rendering (Zero cairo.Context dependency)
# ---------------------------------------------------------------------------

def _render_svg_pixbuf(svg_str: str) -> Optional[GdkPixbuf.Pixbuf]:
    """Render an SVG XML document into a GdkPixbuf.Pixbuf via PixbufLoader('svg')."""
    loader: Optional[GdkPixbuf.PixbufLoader] = None
    closed = False
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.write(svg_str.encode("utf-8"))
        loader.close()
        closed = True
        return loader.get_pixbuf()
    except GLib.Error:
        if loader is not None and not closed:
            try:
                loader.close()
            except GLib.Error:
                pass
        return None


def _build_swatch_svg(r: int, g: int, b: int, size: int = 14) -> str:
    """Return an SVG circular color swatch icon string."""
    rr = max(0, min(255, int(r)))
    gg = max(0, min(255, int(g)))
    bb = max(0, min(255, int(b)))
    c = size / 2.0
    rad = max(1.0, c - 1.5)
    hex_color = f"#{rr:02x}{gg:02x}{bb:02x}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">'
        f'<circle cx="{c:.1f}" cy="{c:.1f}" r="{rad:.1f}" '
        f'fill="{hex_color}" stroke="#d9e6f2" stroke-opacity="0.65" stroke-width="1"/>'
        f"</svg>"
    )


def _apply_curve_offset(
    points: Sequence[tuple[float, float]], offset_pct: int
) -> list[tuple[float, float]]:
    """Shift a piecewise-linear (temp, duty) curve by offset_pct, preserving boundary crossings."""
    if not points:
        return []
    raw = [(float(t), float(d) + float(offset_pct)) for t, d in points]
    out: list[tuple[float, float]] = [(raw[0][0], max(0.0, min(100.0, raw[0][1])))]
    for (t0, d0), (t1, d1) in zip(raw, raw[1:]):
        if d1 != d0:
            crossings: list[tuple[float, float]] = []
            for bound in (0.0, 100.0):
                if (d0 < bound < d1) or (d1 < bound < d0):
                    frac = (bound - d0) / (d1 - d0)
                    crossings.append((t0 + frac * (t1 - t0), bound))
            crossings.sort(key=lambda item: item[0])
            out.extend(crossings)
        out.append((t1, max(0.0, min(100.0, d1))))
    return out


def _place_point_label(
    px: float,
    py: float,
    plot_x0: float,
    plot_x1: float,
    plot_y0: float,
    plot_y1: float,
    prefer_above: bool,
) -> tuple[float, float, str]:
    """Compute non-clipping SVG label coordinates and text-anchor near a plotted point."""
    if px >= plot_x1 - 68:
        anchor = "end"
        lx = min(plot_x1 - 4, px + 4)
    elif px <= plot_x0 + 68:
        anchor = "start"
        lx = max(plot_x0 + 4, px - 4)
    else:
        anchor = "middle"
        lx = px

    if prefer_above:
        ly = py - 9.0
        if ly < plot_y0 + 22:
            ly = min(plot_y1 - 6, py + 16.0)
    else:
        ly = py + 16.0
        if ly > plot_y1 - 5:
            ly = max(plot_y0 + 22, py - 9.0)
    return lx, ly, anchor


FAN_GRAPH_WIDTH = 640
FAN_GRAPH_HEIGHT = 260
FAN_GRAPH_PAD = (48.0, 20.0, 56.0, 30.0)  # left, right, top, bottom


def _fan_graph_bounds(
    width: int = FAN_GRAPH_WIDTH, height: int = FAN_GRAPH_HEIGHT
) -> tuple[float, float, float, float, float, float]:
    """Return (plot_x0, plot_x1, plot_w, plot_y0, plot_y1, plot_h) for the fan graph SVG."""
    pad_left, pad_right, pad_top, pad_bottom = FAN_GRAPH_PAD
    plot_x0 = pad_left
    plot_x1 = float(width) - pad_right
    plot_w = plot_x1 - plot_x0
    plot_y0 = pad_top
    plot_y1 = float(height) - pad_bottom
    plot_h = plot_y1 - plot_y0
    return plot_x0, plot_x1, plot_w, plot_y0, plot_y1, plot_h


def fan_curve_point_to_px(
    temp_c: float,
    duty_pct: float,
    width: int = FAN_GRAPH_WIDTH,
    height: int = FAN_GRAPH_HEIGHT,
) -> tuple[float, float]:
    """Map (temp_c in 30..100, duty_pct in 0..100) to SVG pixel coordinates."""
    plot_x0, _x1, plot_w, _y0, plot_y1, plot_h = _fan_graph_bounds(width, height)
    clamped_t = max(30.0, min(100.0, float(temp_c)))
    clamped_d = max(0.0, min(100.0, float(duty_pct)))
    px = plot_x0 + ((clamped_t - 30.0) / 70.0) * plot_w
    py = plot_y1 - (clamped_d / 100.0) * plot_h
    return px, py


def fan_graph_px_to_temp_duty(
    px: float,
    py: float,
    width: int = FAN_GRAPH_WIDTH,
    height: int = FAN_GRAPH_HEIGHT,
) -> tuple[int, int]:
    """Map SVG/EventBox pixel coordinates (px, py) to rounded (temp_c, duty_pct)."""
    plot_x0, plot_x1, plot_w, plot_y0, plot_y1, plot_h = _fan_graph_bounds(width, height)
    cx = max(plot_x0, min(plot_x1, float(px)))
    cy = max(plot_y0, min(plot_y1, float(py)))
    temp = int(round(30.0 + ((cx - plot_x0) / plot_w) * 70.0))
    duty = int(round(((plot_y1 - cy) / plot_h) * 100.0))
    return max(30, min(100, temp)), max(0, min(100, duty))


def _normalize_4pt_curve(curve: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return a clean 4-point [(T1,D1),(T2,D2),(T3,D3),(T4,D4)] curve list."""
    if len(curve) != 4:
        return list(CUSTOM_4PT_WAYPOINTS)
    pts = [(int(t), int(d)) for t, d in curve]
    t1 = max(30, min(96, pts[0][0]))
    d1 = max(0, min(97, pts[0][1]))
    t4, d4 = 100, 100
    t3 = max(t1 + 2, min(99, pts[2][0]))
    d3 = max(d1 + 2, min(100, pts[2][1]))
    t2 = max(t1 + 1, min(98, t3 - 1, pts[1][0]))
    d2 = max(d1 + 1, min(99, d3 - 1, pts[1][1]))
    return [(t1, d1), (t2, d2), (t3, d3), (t4, d4)]


def clamp_fan_curve_node(
    curve: Sequence[tuple[int, int]],
    node_idx: int,
    target_temp: float,
    target_duty: float,
) -> list[tuple[int, int]]:
    """Clamp middle node (node_idx=1 or 2) to T1+1 <= T2 <= T3-1 <= 98, D1+1 <= D2 <= D3-1 <= 99."""
    pts = _normalize_4pt_curve(curve)
    t1, d1 = pts[0]
    t2, d2 = pts[1]
    t3, d3 = pts[2]
    t4, d4 = pts[3]
    tt = int(round(target_temp))
    dd = int(round(target_duty))

    if node_idx == 1:
        t2 = max(t1 + 1, min(98, t3 - 1, tt))
        d2 = max(d1 + 1, min(99, d3 - 1, dd))
    elif node_idx == 2:
        t3 = max(t1 + 2, t2 + 1, min(99, tt))
        d3 = max(d1 + 2, d2 + 1, min(100, dd))
    else:
        raise ValueError(f"Only middle nodes 1 and 2 are editable (got {node_idx}).")
    return [(t1, d1), (t2, d2), (t3, d3), (t4, d4)]


def hit_test_fan_curve_node(
    px: float,
    py: float,
    cpu_curve: Sequence[tuple[int, int]],
    gpu_curve: Sequence[tuple[int, int]],
    hit_radius: float = 15.0,
    width: int = FAN_GRAPH_WIDTH,
    height: int = FAN_GRAPH_HEIGHT,
) -> Optional[tuple[str, int]]:
    """Return ('cpu'|'gpu', 1|2) for the closest draggable middle node within hit_radius."""
    cpu_pts = _normalize_4pt_curve(cpu_curve)
    gpu_pts = _normalize_4pt_curve(gpu_curve)
    best: Optional[tuple[str, int]] = None
    best_dist = float(hit_radius) + 1e-6
    for which, pts in (("cpu", cpu_pts), ("gpu", gpu_pts)):
        for idx in (1, 2):
            nx, ny = fan_curve_point_to_px(pts[idx][0], pts[idx][1], width=width, height=height)
            dist = ((px - nx) ** 2 + (py - ny) ** 2) ** 0.5
            if dist < best_dist - 1e-9:
                best_dist = dist
                best = (which, idx)
    return best


def build_fan_curve_svg(
    fan_mode_key: Optional[str],
    fan_mode_label: str,
    offset_pct: int,
    cpu_temp: Optional[int],
    cpu_duty_pct: Optional[int],
    cpu_rpm: Optional[int],
    gpu_temp: Optional[int],
    gpu_duty_pct: Optional[int],
    gpu_rpm: Optional[int],
    cpu_history: Sequence[tuple[float, float]] = (),
    gpu_history: Sequence[tuple[float, float]] = (),
    offset_is_preview: bool = False,
    cpu_curve: Sequence[tuple[int, int]] = CUSTOM_4PT_WAYPOINTS,
    gpu_curve: Sequence[tuple[int, int]] = CUSTOM_4PT_WAYPOINTS,
    active_node: Optional[tuple[str, int]] = None,
    width: int = FAN_GRAPH_WIDTH,
    height: int = FAN_GRAPH_HEIGHT,
    fan_locked: bool = False,
) -> str:
    """Build a dark-themed dual-curve (CPU F1 & GPU F2) SVG fan curve & live telemetry graph."""
    eff_offset = max(0, min(100, int(offset_pct)))
    effective_active_node = None if fan_locked else active_node
    plot_x0, plot_x1, plot_w, plot_y0, plot_y1, plot_h = _fan_graph_bounds(width, height)

    def map_x(temp_c: float) -> float:
        clamped = max(30.0, min(100.0, float(temp_c)))
        return plot_x0 + ((clamped - 30.0) / 70.0) * plot_w

    def map_y(duty: float) -> float:
        clamped = max(0.0, min(100.0, float(duty)))
        return plot_y1 - (clamped / 100.0) * plot_h

    cpu_pts = _normalize_4pt_curve(cpu_curve)
    gpu_pts = _normalize_4pt_curve(gpu_curve)
    resolved_key = fan_mode_key if fan_mode_key in FAN_CURVE_SPECS else "auto"
    mode_name, _is_custom_4pt, ref_points = FAN_CURVE_SPECS[resolved_key]

    if fan_locked:
        if "0x80" in (fan_mode_label or "").lower():
            mode_summary = "Mode: EC Silent-Auto Curve (0x80 / XRAM 0x038C•0x039A • Read-Only)"
        else:
            mode_summary = "Mode: EC Silent / Eco Auto Curve (XRAM 0x038C•0x039A • Read-Only)"
    elif fan_mode_key == "custom":
        mode_summary = "Mode: Custom Curve (0x04) • CPU F1 & GPU F2"
    elif fan_mode_key == "max":
        mode_summary = "Mode: Max / Full Speed (0x10) • 100% Ceiling"
    elif fan_mode_key == "auto":
        if "0x08" in (fan_mode_label or ""):
            mode_summary = "Mode: Auto (0x08, Performance) • Firmware Curve"
        else:
            mode_summary = "Mode: Auto (0x02) • Firmware Curve"
    else:
        raw_lbl = fan_mode_label if fan_mode_label and fan_mode_label != "N/A" else "Default"
        if len(raw_lbl) > 22:
            raw_lbl = raw_lbl[:21] + "…"
        mode_summary = f"Mode: {raw_lbl} -> Auto Ref"

    if fan_locked:
        subtitle_note = (
            "Quiet (0xD7=0x80) / Power Saving (0xD7=0x02, 0xDC=0x1A) active — showing read-only EC auto curve"
        )
    elif resolved_key == "auto":
        subtitle_note = (
            "Auto runs internal EC firmware table + Fan Offset (0xE7); "
            "Custom (0x04) applies CPU F1 & GPU F2 curves"
        )
    elif fan_mode_key == "custom":
        subtitle_note = (
            "Custom (0x04): Drag Node 2 or Node 3 on CPU F1 / GPU F2 curves, "
            "then click Apply Custom CPU & GPU Curve"
        )
    elif fan_mode_key == "max":
        subtitle_note = (
            "Max / Full Speed (0x10) enforces a 100% ceiling; "
            "Custom (0x04) applies the plotted CPU F1 & GPU F2 curves"
        )
    else:
        subtitle_note = (
            f"{mode_name} active on EC (0xD7); "
            "Custom (0x04) applies the plotted CPU F1 & GPU F2 curves"
        )

    cpu_valid = cpu_temp is not None and cpu_duty_pct is not None
    gpu_valid = gpu_temp is not None and gpu_duty_pct is not None
    if cpu_valid:
        c_rpm = f" ({cpu_rpm} RPM)" if cpu_rpm is not None else ""
        cpu_hdr = f"CPU {cpu_temp}°C {cpu_duty_pct}%{c_rpm}"
        cpu_hdr_fill = "#38bdf8"
    else:
        cpu_hdr = "CPU: N/A"
        cpu_hdr_fill = "#7c879b"
    if gpu_valid:
        g_rpm = f" ({gpu_rpm} RPM)" if gpu_rpm is not None else ""
        gpu_hdr = f"GPU {gpu_temp}°C {gpu_duty_pct}%{g_rpm}"
        gpu_hdr_fill = "#34d399"
    else:
        gpu_hdr = "GPU: N/A"
        gpu_hdr_fill = "#7c879b"

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="8" '
        f'fill="#161b24" stroke="#2f3746" stroke-width="1"/>',
        # Header row 1: Mode & Live summary
        f'<text x="14" y="16" fill="#e6ebf2" font-family="sans-serif" font-size="11" '
        f'font-weight="bold">{html.escape(mode_summary)}</text>',
        f'<text x="462" y="16" text-anchor="end" fill="{cpu_hdr_fill}" '
        f'font-family="sans-serif" font-size="10.5" font-weight="bold">{html.escape(cpu_hdr)}</text>',
        f'<text x="{width - 14}" y="16" text-anchor="end" fill="{gpu_hdr_fill}" '
        f'font-family="sans-serif" font-size="10.5" font-weight="bold">{html.escape(gpu_hdr)}</text>',
    ]

    # Header row 2: Dual Curve + Offset / Max / Read-Only + Live Telemetry Legend
    parts.append(
        '<line x1="14" y1="30" x2="29" y2="30" stroke="#38bdf8" stroke-width="2.4"/>'
        '<text x="33" y="33.5" fill="#bae6fd" font-family="sans-serif" font-size="10">CPU F1</text>'
        '<line x1="82" y1="30" x2="97" y2="30" stroke="#34d399" stroke-width="2.4"/>'
        '<text x="101" y="33.5" fill="#a7f3d0" font-family="sans-serif" font-size="10">GPU F2</text>'
    )
    if fan_locked:
        parts.append(
            '<rect x="152" y="22.5" width="136" height="14" rx="3" '
            'fill="#252e3f" stroke="#f59e0b" stroke-opacity="0.55" stroke-width="0.9"/>'
            '<text x="220" y="32.5" text-anchor="middle" fill="#fde68a" '
            'font-family="sans-serif" font-size="9.5" font-weight="bold">'
            'Read-Only EC Auto</text>'
        )
    elif fan_mode_key == "max":
        parts.append(
            '<line x1="152" y1="30" x2="169" y2="30" stroke="#f87171" stroke-width="2.2" stroke-dasharray="5,3"/>'
            '<text x="174" y="33.5" fill="#fca5a5" font-family="sans-serif" font-size="10">Max 100% Ceiling</text>'
        )
    elif eff_offset > 0 and resolved_key != "custom":
        off_lbl = f"+{eff_offset}% Offset ({'Preview' if offset_is_preview else '0xE7'})"
        parts.append(
            '<line x1="152" y1="30" x2="169" y2="30" stroke="#fbbf24" stroke-width="2.2" stroke-dasharray="5,3"/>'
            f'<text x="174" y="33.5" fill="#fcd34d" font-family="sans-serif" font-size="10">'
            f'{html.escape(off_lbl)}</text>'
        )
    else:
        parts.append(
            '<line x1="152" y1="30" x2="169" y2="30" stroke="#64748b" stroke-width="1.6" stroke-dasharray="4,3"/>'
            f'<text x="174" y="33.5" fill="#8c99ad" font-family="sans-serif" font-size="10">'
            f'Offset: {eff_offset}% (0xE7)</text>'
        )
    parts.append(
        '<circle cx="332" cy="30" r="3.8" fill="#38bdf8" stroke="#ffffff" stroke-width="1.2"/>'
        '<text x="341" y="33.5" fill="#cbd5e1" font-family="sans-serif" font-size="10">'
        'CPU (0x07/0xCE)</text>'
        '<polygon points="472,25 477,30 472,35 467,30" fill="#34d399" stroke="#ffffff" stroke-width="1.2"/>'
        '<text x="482" y="33.5" fill="#cbd5e1" font-family="sans-serif" font-size="10">'
        'GPU (0x0B/0xCF)</text>'
    )

    # Header row 3: Explicit mode subtitle note
    parts.append(
        f'<text x="{width / 2.0:.1f}" y="47" text-anchor="middle" fill="#9fb0c7" '
        f'font-family="sans-serif" font-size="9.5">{html.escape(subtitle_note)}</text>'
    )

    # Zone background bands (<60 Cool, 60-80 Warm, >80 Hot)
    x30, x60, x80, x100 = map_x(30), map_x(60), map_x(80), map_x(100)
    parts.extend([
        f'<rect x="{x30:.1f}" y="{plot_y0:.1f}" width="{x60 - x30:.1f}" '
        f'height="{plot_h:.1f}" fill="#2d7cf6" fill-opacity="0.06"/>',
        f'<rect x="{x60:.1f}" y="{plot_y0:.1f}" width="{x80 - x60:.1f}" '
        f'height="{plot_h:.1f}" fill="#f59e0b" fill-opacity="0.07"/>',
        f'<rect x="{x80:.1f}" y="{plot_y0:.1f}" width="{x100 - x80:.1f}" '
        f'height="{plot_h:.1f}" fill="#ef4444" fill-opacity="0.08"/>',
        f'<text x="{(x30 + x60) / 2.0:.1f}" y="{plot_y0 + 12:.1f}" text-anchor="middle" '
        f'fill="#6b93c9" font-family="sans-serif" font-size="9.5">Cool (&lt;60°C)</text>',
        f'<text x="{(x60 + x80) / 2.0:.1f}" y="{plot_y0 + 12:.1f}" text-anchor="middle" '
        f'fill="#c99746" font-family="sans-serif" font-size="9.5">Warm (60–80°C)</text>',
        f'<text x="{(x80 + x100) / 2.0:.1f}" y="{plot_y0 + 12:.1f}" text-anchor="middle" '
        f'fill="#c96b6b" font-family="sans-serif" font-size="9.5">Hot (&gt;80°C)</text>',
    ])

    # Grid lines & axis ticks
    for pct in (0, 25, 50, 75, 100):
        yy = map_y(pct)
        parts.append(
            f'<line x1="{plot_x0:.1f}" y1="{yy:.1f}" x2="{plot_x1:.1f}" y2="{yy:.1f}" '
            f'stroke="#2b3344" stroke-width="1"/>'
            f'<text x="{plot_x0 - 6:.1f}" y="{yy + 3.5:.1f}" text-anchor="end" '
            f'fill="#8c99ad" font-family="sans-serif" font-size="10">{pct}%</text>'
        )
    for temp in (30, 40, 50, 60, 70, 80, 90, 100):
        xx = map_x(temp)
        stroke = "#475569" if temp in (60, 80) else "#2b3344"
        parts.append(
            f'<line x1="{xx:.1f}" y1="{plot_y0:.1f}" x2="{xx:.1f}" y2="{plot_y1:.1f}" '
            f'stroke="{stroke}" stroke-width="1"/>'
            f'<text x="{xx:.1f}" y="{plot_y1 + 15:.1f}" text-anchor="middle" '
            f'fill="#8c99ad" font-family="sans-serif" font-size="10">{temp}°C</text>'
        )

    # Firmware reference envelope & 0xE7 offset preview (Auto / non-stock / locked EC auto) or 100% ceiling (Max)
    if not fan_locked and fan_mode_key == "max":
        mx0, my0 = map_x(30), map_y(100)
        mx1, my1 = map_x(100), map_y(100)
        parts.append(
            f'<path d="M {mx0:.1f} {my0:.1f} L {mx1:.1f} {my1:.1f}" fill="none" '
            f'stroke="#f87171" stroke-width="2.4" stroke-dasharray="7,4" stroke-linecap="round"/>'
        )
    elif fan_locked or resolved_key == "auto":
        ref_xy = [(map_x(t), map_y(d)) for t, d in ref_points]
        ref_str = " ".join(f"{px:.1f},{py:.1f}" for px, py in ref_xy)
        parts.append(
            f'<polyline points="{ref_str}" fill="none" stroke="#64748b" '
            f'stroke-opacity="0.5" stroke-width="1.5" stroke-dasharray="3,3" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )
        if not fan_locked and eff_offset > 0:
            shifted_pts = _apply_curve_offset(ref_points, eff_offset)
            shifted_xy = [(map_x(t), map_y(d)) for t, d in shifted_pts]
            if shifted_xy:
                d_cmds = [f"M {shifted_xy[0][0]:.1f} {shifted_xy[0][1]:.1f}"] + [
                    f"L {sx:.1f} {sy:.1f}" for sx, sy in shifted_xy[1:]
                ]
                parts.append(
                    f'<path d="{" ".join(d_cmds)}" fill="none" stroke="#fbbf24" '
                    f'stroke-width="2.2" stroke-dasharray="6,4" '
                    f'stroke-linecap="round" stroke-linejoin="round"/>'
                )

    # Dual CPU F1 (#38bdf8) and GPU F2 (#34d399) 4-point curves
    def curve_plot_points(pts: list[tuple[int, int]]) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        if pts[0][0] > 30:
            out.append((map_x(30), map_y(pts[0][1])))
        for t, d in pts:
            out.append((map_x(t), map_y(d)))
        return out

    gpu_xy = curve_plot_points(gpu_pts)
    cpu_xy = curve_plot_points(cpu_pts)
    curves_identical = cpu_pts == gpu_pts

    gpu_poly = [(plot_x0, plot_y1), *gpu_xy, (plot_x1, plot_y1)]
    cpu_poly = [(plot_x0, plot_y1), *cpu_xy, (plot_x1, plot_y1)]
    parts.append(
        f'<polygon points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in gpu_poly)}" '
        f'fill="#34d399" fill-opacity="0.07"/>'
    )
    parts.append(
        f'<polygon points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in cpu_poly)}" '
        f'fill="#38bdf8" fill-opacity="0.08"/>'
    )
    parts.append(
        f'<polyline points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in gpu_xy)}" fill="none" '
        f'stroke="#34d399" stroke-width="{"4.0" if curves_identical else "2.3"}" '
        f'stroke-opacity="{"0.72" if curves_identical else "0.95"}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
    )
    parts.append(
        f'<polyline points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in cpu_xy)}" fill="none" '
        f'stroke="#38bdf8" stroke-width="{"2.0" if curves_identical else "2.4"}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
    )

    def append_node_value_label(
        nt: int, nd: int, color: str, prefer_above: bool
    ) -> None:
        nx, ny = map_x(nt), map_y(nd)
        lx, ly, anchor = _place_point_label(
            nx, ny, plot_x0, plot_x1, plot_y0, plot_y1, prefer_above=prefer_above
        )
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" fill="{color}" '
            f'fill-opacity="0.92" font-family="sans-serif" font-size="8.5">'
            f'({nt}°C, {nd}%)</text>'
        )

    # Numbered Node Handles: fixed endpoints (Node 1 & 4) and middle nodes (Node 2 & 3)
    for idx in (0, 3):
        node_num = str(idx + 1)
        shared_ep = cpu_pts[idx] == gpu_pts[idx]
        endpoints: list[tuple[str, tuple[int, int]]] = (
            [("shared", cpu_pts[idx])]
            if shared_ep
            else [("gpu", gpu_pts[idx]), ("cpu", cpu_pts[idx])]
        )
        cpu_ep_above = cpu_pts[idx][1] >= gpu_pts[idx][1]
        for kind, (nt, nd) in endpoints:
            nx, ny = map_x(nt), map_y(nd)
            stroke_col = "#34d399" if kind == "gpu" else ("#38bdf8" if kind == "cpu" else "#94a3b8")
            parts.append(
                f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="5.0" fill="#1e293b" '
                f'stroke="{stroke_col}" stroke-width="1.5"/>'
                f'<text x="{nx:.1f}" y="{ny + 3.0:.1f}" text-anchor="middle" '
                f'fill="#cbd5e1" font-family="sans-serif" font-size="8.5" font-weight="bold">{node_num}</text>'
            )
            lbl_col = "#cbd5e1" if kind == "shared" else ("#bae6fd" if kind == "cpu" else "#a7f3d0")
            pref_above = False if kind == "shared" else (cpu_ep_above if kind == "cpu" else not cpu_ep_above)
            append_node_value_label(nt, nd, lbl_col, prefer_above=pref_above)

    def draw_middle_handle(
        which: str,
        idx: int,
        pt: tuple[int, int],
        prefer_above: bool,
        shared_overlap: bool = False,
    ) -> None:
        nx, ny = map_x(pt[0]), map_y(pt[1])
        is_active = effective_active_node == (which, idx) or (
            shared_overlap and effective_active_node in (("cpu", idx), ("gpu", idx))
        )
        node_num = str(idx + 1)
        fill_col = "#38bdf8" if which == "cpu" else "#34d399"
        ring_col = "#ffffff" if is_active else ("#e0f2fe" if which == "cpu" else "#d1fae5")

        if is_active:
            parts.append(
                f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="12.5" fill="{fill_col}" '
                f'fill-opacity="0.26" stroke="#ffffff" stroke-width="1.5"/>'
            )
        if shared_overlap:
            parts.append(
                f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="9.0" fill="#34d399" '
                f'stroke="#0f172a" stroke-width="1.2"/>'
            )
        parts.append(
            f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="{"6.6" if shared_overlap else "7.2"}" '
            f'fill="{fill_col}" stroke="{ring_col}" stroke-width="1.6"/>'
            f'<text x="{nx:.1f}" y="{ny + 3.0:.1f}" text-anchor="middle" '
            f'fill="#0b1220" font-family="sans-serif" font-size="9" font-weight="bold">{node_num}</text>'
        )
        if is_active:
            tag_prefix = (
                ("CPU" if effective_active_node and effective_active_node[0] == "cpu" else "GPU")
                if shared_overlap
                else ("CPU" if which == "cpu" else "GPU")
            )
            pill_lbl = f"{tag_prefix} N{node_num}: {pt[0]}°C, {pt[1]}%"
            lx, ly, anchor = _place_point_label(
                nx, ny, plot_x0, plot_x1, plot_y0, plot_y1, prefer_above=(ny > plot_y0 + 36)
            )
            parts.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" fill="#f8fafc" '
                f'font-family="sans-serif" font-size="9.5" font-weight="bold">{html.escape(pill_lbl)}</text>'
            )
        else:
            lbl_col = "#cbd5e1" if shared_overlap else ("#bae6fd" if which == "cpu" else "#a7f3d0")
            append_node_value_label(pt[0], pt[1], lbl_col, prefer_above=prefer_above)

    for idx in (1, 2):
        if cpu_pts[idx] == gpu_pts[idx]:
            ny_mid = map_y(cpu_pts[idx][1])
            draw_middle_handle(
                "cpu",
                idx,
                cpu_pts[idx],
                prefer_above=(ny_mid > plot_y0 + 36),
                shared_overlap=True,
            )
        else:
            cpu_above = cpu_pts[idx][1] >= gpu_pts[idx][1]
            if effective_active_node == ("gpu", idx):
                draw_middle_handle("cpu", idx, cpu_pts[idx], prefer_above=cpu_above)
                draw_middle_handle("gpu", idx, gpu_pts[idx], prefer_above=not cpu_above)
            else:
                draw_middle_handle("gpu", idx, gpu_pts[idx], prefer_above=not cpu_above)
                draw_middle_handle("cpu", idx, cpu_pts[idx], prefer_above=cpu_above)

    # Live temperature vertical telemetry markers
    for temp_val, marker_col in ((cpu_temp, "#38bdf8"), (gpu_temp, "#34d399")):
        if temp_val is not None:
            tx_line = map_x(float(temp_val))
            parts.append(
                f'<line x1="{tx_line:.1f}" y1="{plot_y0:.1f}" x2="{tx_line:.1f}" y2="{plot_y1:.1f}" '
                f'stroke="{marker_col}" stroke-opacity="0.42" stroke-width="1.2" stroke-dasharray="3,3"/>'
            )

    # Rolling telemetry history trails (up to 20 poll samples)
    for trail, color in ((cpu_history, "#38bdf8"), (gpu_history, "#34d399")):
        if not trail:
            continue
        trail_xy = [(map_x(t), map_y(d)) for t, d in trail]
        if len(trail_xy) >= 2:
            t_str = " ".join(f"{tx:.1f},{ty:.1f}" for tx, ty in trail_xy)
            parts.append(
                f'<polyline points="{t_str}" fill="none" stroke="{color}" '
                f'stroke-opacity="0.55" stroke-width="1.8" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
            )
        if len(trail_xy) > 1:
            total = len(trail_xy) - 1
            for idx, (tx, ty) in enumerate(trail_xy[:-1]):
                op = 0.25 + 0.45 * ((idx + 1) / max(1, total))
                parts.append(
                    f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="2.1" '
                    f'fill="{color}" fill-opacity="{op:.2f}"/>'
                )

    # Current live CPU & GPU operating points + RPM labels
    cpu_lbl_box: Optional[tuple[float, float]] = None
    if cpu_valid:
        cx, cy = map_x(float(cpu_temp)), map_y(float(cpu_duty_pct))  # type: ignore[arg-type]
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5.2" fill="#38bdf8" '
            f'stroke="#ffffff" stroke-width="1.5"/>'
        )
        lx, ly, anchor = _place_point_label(cx, cy, plot_x0, plot_x1, plot_y0, plot_y1, prefer_above=True)
        cpu_lbl_box = (lx, ly)
        c_tag = f"{cpu_rpm} RPM" if cpu_rpm is not None else f"{cpu_duty_pct}%"
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" fill="#bae6fd" '
            f'font-family="sans-serif" font-size="10" font-weight="bold">{html.escape(c_tag)}</text>'
        )

    if gpu_valid:
        gx, gy = map_x(float(gpu_temp)), map_y(float(gpu_duty_pct))  # type: ignore[arg-type]
        parts.append(
            f'<polygon points="{gx:.1f},{gy - 6.0:.1f} {gx + 6.0:.1f},{gy:.1f} '
            f'{gx:.1f},{gy + 6.0:.1f} {gx - 6.0:.1f},{gy:.1f}" '
            f'fill="#34d399" stroke="#ffffff" stroke-width="1.5"/>'
        )
        lx, ly, anchor = _place_point_label(gx, gy, plot_x0, plot_x1, plot_y0, plot_y1, prefer_above=False)
        if cpu_lbl_box is not None and abs(lx - cpu_lbl_box[0]) < 62 and abs(ly - cpu_lbl_box[1]) < 13:
            alt_ly = gy - 11.0 if ly > gy else gy + 18.0
            if plot_y0 + 22 <= alt_ly <= plot_y1 - 5:
                ly = alt_ly
        g_tag = f"{gpu_rpm} RPM" if gpu_rpm is not None else f"{gpu_duty_pct}%"
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" fill="#a7f3d0" '
            f'font-family="sans-serif" font-size="10" font-weight="bold">{html.escape(g_tag)}</text>'
        )

    # Outer plot border
    parts.append(
        f'<rect x="{plot_x0:.1f}" y="{plot_y0:.1f}" width="{plot_w:.1f}" '
        f'height="{plot_h:.1f}" fill="none" stroke="#3c4659" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Main GTK3 Control Center Window
# ---------------------------------------------------------------------------

class ControlCenterWindow(Gtk.Window):
    """Native GTK3 Control Center for Acer Aspire A715-59G."""

    def __init__(
        self,
        runner: ScriptRunner,
        auto_refresh: bool = True,
        no_auto_apply: bool = False,
    ) -> None:
        super().__init__(title="Acer Aspire A715-59G Control Center")
        self.runner = runner
        self.no_auto_apply = bool(no_auto_apply)
        self._updating_ui_depth = 0
        self._refresh_in_flight = False
        self._refresh_pending = False
        self._refresh_pending_quiet = True
        self._refresh_pending_poll = True
        self._refresh_lock = threading.Lock()
        self._bg_threads: set[threading.Thread] = set()
        self._bg_threads_lock = threading.Lock()
        self._last_apply_config_result: Optional[ApplyConfigResult] = None
        self._poll_source_id: Optional[int] = None
        self._selected_power_profile = "entertainment"
        self._active_power_profile: Optional[str] = None
        self._fan_mode_key: Optional[str] = None
        self._fan_mode_text: str = "N/A"
        self._live_offset_pct: Optional[int] = None
        self._preview_offset_pct: int = 0
        self._cpu_curve: list[tuple[int, int]] = list(CUSTOM_4PT_WAYPOINTS)
        self._gpu_curve: list[tuple[int, int]] = list(CUSTOM_4PT_WAYPOINTS)
        self._curve_dirty: bool = False
        self._dragging_curve_node: Optional[tuple[str, int]] = None
        self._hovered_curve_node: Optional[tuple[str, int]] = None
        self._cpu_telemetry_history: deque[tuple[float, float]] = deque(maxlen=20)
        self._gpu_telemetry_history: deque[tuple[float, float]] = deque(maxlen=20)
        self._latest_cpu_telemetry: Optional[tuple[Optional[int], Optional[int], Optional[int]]] = None
        self._latest_gpu_telemetry: Optional[tuple[Optional[int], Optional[int], Optional[int]]] = None
        self._banner_alert: Optional[tuple[str, str]] = None
        self._last_backend_inputs: dict[str, Any] = {}
        self._unlock_btn_daemon_mode = False
        self._destroyed = False

        self.set_default_size(980, 760)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.get_style_context().add_class("acer-cc-window")
        self.connect("delete-event", self._on_delete_event)

        self._build_header_bar()

        root_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root_vbox.set_border_width(12)
        self.add(root_vbox)

        # Top mode banner
        self.mode_banner_box, self.mode_banner_label = self._build_mode_banner()
        root_vbox.pack_start(self.mode_banner_box, False, False, 0)

        # 5-Tab Notebook
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        root_vbox.pack_start(self.notebook, True, True, 0)

        self.notebook.append_page(self._build_power_tab(), Gtk.Label(label="Power & RAPL"))
        self.notebook.append_page(self._build_fan_tab(), Gtk.Label(label="Fans & Thermals"))
        self.notebook.append_page(self._build_rgb_tab(), Gtk.Label(label="Keyboard RGB"))
        self.notebook.append_page(self._build_battery_tab(), Gtk.Label(label="Battery & FlexiCharger"))
        self.notebook.append_page(self._build_peripherals_tab(), Gtk.Label(label="Peripherals & Lid"))

        # Collapsible Command Log
        root_vbox.pack_start(self._build_log_expander(), False, False, 0)

        self._update_mode_banner()
        self._sync_unlock_button()

        if auto_refresh:
            if (not self.runner.dry_run) and (not self.no_auto_apply):
                GLib.idle_add(self._start_launch_auto_apply)
            else:
                if self.no_auto_apply and not self.runner.dry_run:
                    self._append_log("Launch auto-apply skipped (--no-auto-apply).")
                GLib.idle_add(self.refresh_all)

    # ---- Widget Helpers ----

    @staticmethod
    def _make_section(title: str) -> tuple[Gtk.Box, Gtk.Box]:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.get_style_context().add_class("section-box")
        lbl = Gtk.Label(label=title, xalign=0.0)
        lbl.get_style_context().add_class("section-title")
        outer.pack_start(lbl, False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.pack_start(body, True, True, 0)
        return outer, body

    @staticmethod
    def _make_card(title: str, initial_val: str = "—", subtitle: str = "") -> tuple[Gtk.Box, Gtk.Label, Gtk.Label]:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.get_style_context().add_class("card")
        t_lbl = Gtk.Label(label=title.upper(), xalign=0.0)
        t_lbl.get_style_context().add_class("card-title")
        v_lbl = Gtk.Label(label=initial_val, xalign=0.0)
        v_lbl.get_style_context().add_class("card-value")
        v_lbl.set_line_wrap(True)
        v_lbl.set_selectable(True)
        s_lbl = Gtk.Label(label=subtitle, xalign=0.0)
        s_lbl.get_style_context().add_class("card-sub")
        s_lbl.set_line_wrap(True)
        box.pack_start(t_lbl, False, False, 0)
        box.pack_start(v_lbl, False, False, 0)
        box.pack_start(s_lbl, False, False, 0)
        if not subtitle:
            s_lbl.set_no_show_all(True)
            s_lbl.hide()
        return box, v_lbl, s_lbl

    @staticmethod
    def _set_active_choice(buttons: dict[str, Gtk.Button], active_key: Optional[str]) -> None:
        for key, btn in buttons.items():
            ctx = btn.get_style_context()
            if active_key and key == active_key:
                ctx.add_class("active-choice")
            else:
                ctx.remove_class("active-choice")

    @staticmethod
    def _update_temp_bar(bar: Gtk.ProgressBar, temp_c: Optional[int]) -> None:
        ctx = bar.get_style_context()
        ctx.remove_class("temp-warm")
        ctx.remove_class("temp-hot")
        if temp_c is None:
            bar.set_fraction(0.0)
            bar.set_text("—")
            return
        frac = max(0.0, min(1.0, temp_c / 105.0))
        bar.set_fraction(frac)
        bar.set_text(f"{temp_c} °C")
        if temp_c >= 85:
            ctx.add_class("temp-hot")
        elif temp_c >= 70:
            ctx.add_class("temp-warm")

    # ---- HeaderBar & Banner ----

    def _build_header_bar(self) -> None:
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title("Acer Aspire A715-59G Control Center")
        hb.set_subtitle("Board RTH • EC Mailbox & Linux RAPL")
        hb.get_style_context().add_class("acer-header")

        # Left side: Unlock Live EC toggle
        self.unlock_btn = Gtk.ToggleButton(label="Unlock Live EC (pkexec)")
        self.unlock_btn.get_style_context().add_class("unlock-btn")
        self.unlock_btn.connect("toggled", self._on_unlock_toggled)
        hb.pack_start(self.unlock_btn)

        # Right side: Auto-Poll (2s), Refresh, Apply Config, Auto-apply, Dry-Run
        poll_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        poll_lbl = Gtk.Label(label="Auto-Poll (2s)")
        self.poll_switch = Gtk.Switch()
        self.poll_switch.set_valign(Gtk.Align.CENTER)
        self.poll_switch.connect("notify::active", self._on_poll_switch_toggled)
        poll_box.pack_start(poll_lbl, False, False, 0)
        poll_box.pack_start(self.poll_switch, False, False, 0)
        hb.pack_end(poll_box)

        self.refresh_btn = Gtk.Button(label="Refresh")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh_all())
        hb.pack_end(self.refresh_btn)

        self.apply_config_btn = Gtk.Button(label="Apply Config")
        self.apply_config_btn.set_tooltip_text(f"Apply saved settings from {self.runner.config_path}")
        self.apply_config_btn.connect("clicked", self._on_apply_config_clicked)
        hb.pack_end(self.apply_config_btn)

        auto_apply_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        auto_apply_tip = f"Auto-apply saved settings on launch ({self.runner.config_path})"
        auto_apply_box.set_tooltip_text(auto_apply_tip)
        auto_apply_lbl = Gtk.Label(label="Auto-apply")
        self.auto_apply_switch = Gtk.Switch()
        self.auto_apply_switch.set_valign(Gtk.Align.CENTER)
        self.auto_apply_switch.set_tooltip_text(auto_apply_tip)
        with self._ui_update_guard():
            init_cfg = self.runner.load_saved_config(ensure=False)
            self.auto_apply_switch.set_active(bool(init_cfg.get("auto_apply_on_launch", True)))
        self.auto_apply_switch.connect("notify::active", self._on_auto_apply_switch_toggled)
        auto_apply_box.pack_start(auto_apply_lbl, False, False, 0)
        auto_apply_box.pack_start(self.auto_apply_switch, False, False, 0)
        hb.pack_end(auto_apply_box)

        dry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dry_lbl = Gtk.Label(label="Dry-Run")
        self.dry_switch = Gtk.Switch()
        self.dry_switch.set_valign(Gtk.Align.CENTER)
        self.dry_switch.set_active(self.runner.dry_run)
        self.dry_switch.connect("notify::active", self._on_dry_switch_toggled)
        dry_box.pack_start(dry_lbl, False, False, 0)
        dry_box.pack_start(self.dry_switch, False, False, 0)
        hb.pack_end(dry_box)

        self.set_titlebar(hb)

    def _build_mode_banner(self) -> tuple[Gtk.Box, Gtk.Label]:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.get_style_context().add_class("mode-banner")
        lbl = Gtk.Label(xalign=0.0)
        lbl.set_line_wrap(True)
        box.pack_start(lbl, True, True, 0)
        return box, lbl

    @property
    def _updating_ui(self) -> bool:
        return self._updating_ui_depth > 0

    @contextmanager
    def _ui_update_guard(self) -> Iterator[None]:
        self._updating_ui_depth += 1
        try:
            yield
        finally:
            self._updating_ui_depth = max(0, self._updating_ui_depth - 1)

    def _update_mode_banner(self, override_note: Optional[str] = None) -> None:
        kind, text = self.runner.current_mode_label()
        style_kind = kind
        extra_note = override_note
        if extra_note is None and self._banner_alert is not None:
            style_kind, extra_note = self._banner_alert
        if extra_note:
            text = f"{text}  |  {extra_note}"
        self.mode_banner_label.set_text(text)
        ctx = self.mode_banner_box.get_style_context()
        for cls in ("mode-live", "mode-dry", "mode-snapshot", "mode-warn"):
            ctx.remove_class(cls)
        if style_kind == "live":
            ctx.add_class("mode-live")
        elif style_kind == "dry-run":
            ctx.add_class("mode-dry")
        elif style_kind == "warn":
            ctx.add_class("mode-warn")
        else:
            ctx.add_class("mode-snapshot")

    def _sync_unlock_button(self) -> None:
        with self._ui_update_guard():
            ctx = self.unlock_btn.get_style_context()
            self._unlock_btn_daemon_mode = False
            if self.runner.is_root:
                self.unlock_btn.set_active(True)
                self.unlock_btn.set_sensitive(False)
                self.unlock_btn.set_label("Live EC (Root)")
                ctx.add_class("unlocked")
            elif self.runner.worker_alive():
                self.unlock_btn.set_active(True)
                self.unlock_btn.set_sensitive(True)
                self.unlock_btn.set_label("Live EC Unlocked")
                ctx.add_class("unlocked")
            elif not self.runner.explicit_ec_file and self.runner.daemon_available():
                self._unlock_btn_daemon_mode = True
                self.unlock_btn.set_active(True)
                self.unlock_btn.set_sensitive(True)
                self.unlock_btn.set_label("Daemon Connected (Live EC)")
                ctx.add_class("unlocked")
            else:
                self.unlock_btn.set_active(False)
                self.unlock_btn.set_sensitive(True)
                self.unlock_btn.set_label("Unlock Live EC (pkexec)")
                ctx.remove_class("unlocked")

    # ---- Tab 1: Power Profiles & CPU RAPL ----

    def _build_power_tab(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_border_width(12)
        scroll.add(vbox)

        # Profile selector section
        prof_sec, prof_body = self._make_section("EC Power Profiles (Mailbox 0xD8)")
        vbox.pack_start(prof_sec, False, False, 0)

        self.lbl_power_profile_note = Gtk.Label(
            label=(
                "Switching a profile sets APRD=1 (EC 0xEB |= 0x40) + EC 0xD8, applies the static OEM PL1 / PL2 "
                "table (15/30W Quiet & Power Saving, 45/115W Entertainment, 90/115W Performance) to Linux RAPL, "
                "and configures dGPU power via nvidia-smi -pl + PCI Runtime PM (D3cold in Power Saving)."
            ),
            xalign=0.0,
        )
        self.lbl_power_profile_note.get_style_context().add_class("card-sub")
        self.lbl_power_profile_note.set_line_wrap(True)
        prof_body.pack_start(self.lbl_power_profile_note, False, False, 0)

        btn_grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        btn_grid.set_column_homogeneous(True)
        self.power_profile_btns: dict[str, Gtk.Button] = {}
        self.power_profile_desc_labels: dict[str, Gtk.Label] = {}
        for idx, (key, title, desc) in enumerate(POWER_PROFILES):
            btn = Gtk.Button()
            btn.get_style_context().add_class("choice-btn")
            tip = POWER_PROFILE_TOOLTIPS.get(key)
            if tip:
                btn.set_tooltip_text(tip)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t_lbl = Gtk.Label(label=title)
            d_lbl = Gtk.Label(label=desc)
            d_lbl.get_style_context().add_class("card-sub")
            d_lbl.set_justify(Gtk.Justification.CENTER)
            d_lbl.set_line_wrap(True)
            inner.pack_start(t_lbl, False, False, 0)
            inner.pack_start(d_lbl, False, False, 0)
            btn.add(inner)
            btn.connect("clicked", self._on_power_profile_clicked, key)
            btn_grid.attach(btn, idx % 2, idx // 2, 1, 1)
            self.power_profile_btns[key] = btn
            self.power_profile_desc_labels[key] = d_lbl
        prof_body.pack_start(btn_grid, False, False, 0)

        # OS RAPL Ceiling & Sync Options
        rapl_sec, rapl_body = self._make_section("OS RAPL Ceiling & Sync Options")
        vbox.pack_start(rapl_sec, False, False, 0)

        self.lbl_rapl_note = Gtk.Label(
            label=(
                "EC + BIOS control hardware PECI limits (0xF0..0xF4). power_profile.sh applies the static "
                "OEM PL1 / PL2 table (15/30W Quiet, 15/30W Power Saving, 45/115W Entertainment, "
                "90/115W Performance) to Linux RAPL MSR & MMIO, unless overridden below."
            ),
            xalign=0.0,
        )
        self.lbl_rapl_note.get_style_context().add_class("card-sub")
        self.lbl_rapl_note.set_line_wrap(True)
        rapl_body.pack_start(self.lbl_rapl_note, False, False, 0)

        self.rapl_advanced_expander = Gtk.Expander(
            label="Advanced: Optional OS RAPL Software Cap (--pl1 / --pl2)"
        )
        self.rapl_advanced_expander.set_expanded(False)

        rapl_grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        rapl_grid.set_margin_top(6)
        self.rapl_advanced_expander.add(rapl_grid)
        rapl_body.pack_start(self.rapl_advanced_expander, False, False, 0)

        # PL1 row
        self.pl1_check = Gtk.CheckButton(label="Override PL1 (Sustained Watts)")
        self.pl1_check.set_active(False)
        self.pl1_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 5, 115, 1)
        self.pl1_scale.set_value(90)
        self.pl1_scale.set_draw_value(False)
        self.pl1_scale.set_hexpand(True)
        self.pl1_spin = Gtk.SpinButton.new_with_range(5, 115, 1)
        self.pl1_spin.set_value(90)
        self._link_scale_spin(self.pl1_scale, self.pl1_spin)
        self.pl1_check.connect("toggled", lambda *_: self._sync_rapl_sensitivity())
        rapl_grid.attach(self.pl1_check, 0, 0, 1, 1)
        rapl_grid.attach(self.pl1_scale, 1, 0, 1, 1)
        rapl_grid.attach(self.pl1_spin, 2, 0, 1, 1)

        # PL2 row
        self.pl2_check = Gtk.CheckButton(label="Override PL2 (Burst Watts)")
        self.pl2_check.set_active(False)
        self.pl2_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 10, 150, 1)
        self.pl2_scale.set_value(115)
        self.pl2_scale.set_draw_value(False)
        self.pl2_scale.set_hexpand(True)
        self.pl2_spin = Gtk.SpinButton.new_with_range(10, 150, 1)
        self.pl2_spin.set_value(115)
        self._link_scale_spin(self.pl2_scale, self.pl2_spin)
        self.pl2_check.connect("toggled", lambda *_: self._sync_rapl_sensitivity())
        rapl_grid.attach(self.pl2_check, 0, 1, 1, 1)
        rapl_grid.attach(self.pl2_scale, 1, 1, 1, 1)
        rapl_grid.attach(self.pl2_spin, 2, 1, 1, 1)

        # Checkboxes + Apply button row
        opts_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.sync_rapl_check = Gtk.CheckButton(label="Sync Linux RAPL")
        self.sync_rapl_check.set_active(True)
        self.sync_rapl_check.set_tooltip_text(
            "Apply static OEM PL1/PL2 (15/30W, 45/115W, 90/115W) or optional cap above to OS RAPL MSR & MMIO on profile switch."
        )
        self.sync_rapl_check.connect("toggled", lambda *_: self._sync_rapl_sensitivity())
        self.sync_os_check = Gtk.CheckButton(label="Sync OS powerprofilesctl")
        self.sync_os_check.set_active(True)

        self.apply_power_btn = Gtk.Button(label="Apply Selected / Custom RAPL")
        self.apply_power_btn.get_style_context().add_class("primary-action")
        self.apply_power_btn.connect("clicked", self._on_apply_custom_power_clicked)

        opts_box.pack_start(self.sync_rapl_check, False, False, 0)
        opts_box.pack_start(self.sync_os_check, False, False, 0)
        opts_box.pack_end(self.apply_power_btn, False, False, 0)
        rapl_body.pack_start(opts_box, False, False, 0)
        self._sync_rapl_sensitivity()

        # Live Telemetry Cards
        tel_sec, tel_body = self._make_section("Power & RAPL Telemetry")
        vbox.pack_start(tel_sec, False, False, 0)

        cards_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        cards_grid.set_column_homogeneous(True)
        c1, self.card_ec_profile, _ = self._make_card("Active EC Profile (0xD8)")
        c2, self.card_ec_peci, _ = self._make_card("EC PL1T/PL2T Hardware PECI (0xF0..0xF4)")
        c3, self.card_rapl_msr, _ = self._make_card("OS RAPL Ceiling (MSR)")
        c4, self.card_rapl_mmio, _ = self._make_card("OS RAPL Ceiling (MMIO)")
        c5, self.card_gpu_power, self.sub_gpu_power = self._make_card(
            "dGPU Power & Runtime PM (nvidia-smi / PCI PM)",
            "—",
            "PCI Runtime PM / nvidia-powerd telemetry",
        )
        c6, self.card_oem_table, self.sub_oem_table = self._make_card(
            "OEM Table (Direct EC + RAPL)",
            "—",
            "Static OEM PL1/PL2 & EC PL1T/PL2T readout",
        )
        cards_grid.attach(c1, 0, 0, 1, 1)
        cards_grid.attach(c2, 1, 0, 1, 1)
        cards_grid.attach(c3, 0, 1, 1, 1)
        cards_grid.attach(c4, 1, 1, 1, 1)
        cards_grid.attach(c5, 0, 2, 2, 1)
        cards_grid.attach(c6, 0, 3, 2, 1)
        tel_body.pack_start(cards_grid, False, False, 0)

        return scroll

    def _sync_rapl_sensitivity(self) -> None:
        rapl_on = self.sync_rapl_check.get_active()
        self.pl1_check.set_sensitive(rapl_on)
        self.pl2_check.set_sensitive(rapl_on)
        self.pl1_scale.set_sensitive(rapl_on and self.pl1_check.get_active())
        self.pl1_spin.set_sensitive(rapl_on and self.pl1_check.get_active())
        self.pl2_scale.set_sensitive(rapl_on and self.pl2_check.get_active())
        self.pl2_spin.set_sensitive(rapl_on and self.pl2_check.get_active())

    def _build_power_args(self, profile_key: str) -> list[str]:
        args: list[str] = []
        if not self.sync_rapl_check.get_active():
            args.append("--no-rapl")
        else:
            if self.pl1_check.get_active():
                self.pl1_spin.update()
                args.extend(["--pl1", str(self.pl1_spin.get_value_as_int())])
            if self.pl2_check.get_active():
                self.pl2_spin.update()
                args.extend(["--pl2", str(self.pl2_spin.get_value_as_int())])
        if not self.sync_os_check.get_active():
            args.append("--no-os")
        args.append(profile_key)
        return args

    # ---- Tab 2: Fans & Thermals ----

    _FAN_LOCK_BANNER_TEXT = (
        "Quiet / Power Saving Profile Active — Showing read-only EC auto fan curve "
        "(0x038C/0x039A; switch to Entertainment or Performance to edit Fan Modes & Custom Curve)"
    )
    _FAN_GRAPH_NOTE_UNLOCKED = (
        "Drag Node 2 or Node 3 directly on CPU F1 (Cyan) / GPU F2 (Emerald), "
        "or adjust the spinbuttons below — edits preview live until Apply Custom CPU & GPU Curve is clicked. "
        "Dragging Fan Offset previews 0xE7 shifting over firmware Auto curve."
    )
    _FAN_GRAPH_NOTE_LOCKED = _FAN_LOCK_BANNER_TEXT

    def _build_fan_tab(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_border_width(12)
        scroll.add(vbox)

        # Quiet / Power Saving Fan Lock Banner
        self.fan_lock_banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.fan_lock_banner_box.get_style_context().add_class("mode-banner")
        self.fan_lock_banner_box.get_style_context().add_class("mode-dry")
        self.fan_lock_banner_label = Gtk.Label(
            label=self._FAN_LOCK_BANNER_TEXT,
            xalign=0.0,
        )
        self.fan_lock_banner_label.set_line_wrap(True)
        self.fan_lock_banner_box.pack_start(self.fan_lock_banner_label, True, True, 0)
        self.fan_lock_banner_box.set_no_show_all(True)
        self.fan_lock_banner_box.hide()
        vbox.pack_start(self.fan_lock_banner_box, False, False, 0)

        # Thermal & Fan telemetry section
        tel_sec, tel_body = self._make_section("Live Thermals & Fan Speeds")
        vbox.pack_start(tel_sec, False, False, 0)

        # 4 temperature readouts with bars
        temp_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        temp_grid.set_column_homogeneous(True)
        self.temp_bars: dict[str, Gtk.ProgressBar] = {}
        self.temp_vals: dict[str, Gtk.Label] = {}
        temp_items = [
            ("cpu", "CPU Temp (0x07)"),
            ("ac1", "Sensor AC1 (0x08)"),
            ("vr", "CPU VR (0x0A)"),
            ("dgpu", "dGPU Board (0x0B)"),
        ]
        for idx, (tkey, title) in enumerate(temp_items):
            card, val_lbl, _ = self._make_card(title)
            bar = Gtk.ProgressBar()
            bar.set_show_text(True)
            bar.set_text("—")
            card.pack_start(bar, False, False, 4)
            temp_grid.attach(card, idx % 2, idx // 2, 1, 1)
            self.temp_bars[tkey] = bar
            self.temp_vals[tkey] = val_lbl
        tel_body.pack_start(temp_grid, False, False, 0)

        # Fans + NVIDIA GPU + Fan Mode cards
        fan_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        fan_grid.set_column_homogeneous(True)

        c_cpu_fan, self.lbl_cpu_fan, self.sub_cpu_fan = self._make_card("CPU Fan (0xCE)", "—", "Duty / Tach")
        self.bar_cpu_fan = Gtk.ProgressBar()
        self.bar_cpu_fan.get_style_context().add_class("fan-bar")
        self.bar_cpu_fan.set_show_text(True)
        self.bar_cpu_fan.set_text("0%")
        c_cpu_fan.pack_start(self.bar_cpu_fan, False, False, 4)

        c_gpu_fan, self.lbl_gpu_fan, self.sub_gpu_fan = self._make_card("GPU Fan (0xCF)", "—", "Duty / Tach")
        self.bar_gpu_fan = Gtk.ProgressBar()
        self.bar_gpu_fan.get_style_context().add_class("fan-bar")
        self.bar_gpu_fan.set_show_text(True)
        self.bar_gpu_fan.set_text("0%")
        c_gpu_fan.pack_start(self.bar_gpu_fan, False, False, 4)

        c_nv, self.lbl_nvidia_gpu, _ = self._make_card("NVIDIA GPU")
        c_fmode, self.lbl_fan_mode, self.sub_fan_offset = self._make_card("Active Fan Mode (0xD7)", "—", "Offset (0xE7): —")

        fan_grid.attach(c_cpu_fan, 0, 0, 1, 1)
        fan_grid.attach(c_gpu_fan, 1, 0, 1, 1)
        fan_grid.attach(c_nv, 0, 1, 1, 1)
        fan_grid.attach(c_fmode, 1, 1, 1, 1)
        tel_body.pack_start(fan_grid, False, False, 0)

        # Fan Curve & Live Telemetry Graph Card
        graph_sec, graph_body = self._make_section("Fan Curve & Live Telemetry Graph")
        vbox.pack_start(graph_sec, False, False, 0)

        graph_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        graph_card.get_style_context().add_class("card")
        self.fan_graph_image = Gtk.Image()
        self.fan_graph_image.set_size_request(FAN_GRAPH_WIDTH, FAN_GRAPH_HEIGHT)
        self.fan_graph_eventbox = Gtk.EventBox()
        self.fan_graph_eventbox.set_halign(Gtk.Align.CENTER)
        self.fan_graph_eventbox.set_valign(Gtk.Align.CENTER)
        self.fan_graph_eventbox.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON1_MOTION_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.fan_graph_eventbox.add(self.fan_graph_image)
        self.fan_graph_eventbox.connect("button-press-event", self._on_fan_graph_button_press)
        self.fan_graph_eventbox.connect("motion-notify-event", self._on_fan_graph_motion)
        self.fan_graph_eventbox.connect("button-release-event", self._on_fan_graph_button_release)
        graph_card.pack_start(self.fan_graph_eventbox, False, False, 0)

        self.lbl_fan_graph_note = Gtk.Label(
            label=self._FAN_GRAPH_NOTE_UNLOCKED,
            xalign=0.0,
        )
        self.lbl_fan_graph_note.get_style_context().add_class("card-sub")
        self.lbl_fan_graph_note.set_line_wrap(True)
        graph_card.pack_start(self.lbl_fan_graph_note, False, False, 0)
        graph_body.pack_start(graph_card, False, False, 0)

        # Custom Fan Curve (2 Middle Nodes: CPU & GPU) Control Card
        curve_sec, curve_body = self._make_section("Custom Fan Curve (2 Middle Nodes: CPU & GPU)")
        self.fan_curve_sec = curve_sec
        vbox.pack_start(curve_sec, False, False, 0)

        curve_cols = Gtk.Grid(column_spacing=10, row_spacing=10)
        curve_cols.set_column_homogeneous(True)

        def build_curve_panel(
            which: str, title: str, accent_cls: str
        ) -> tuple[Gtk.Box, Gtk.Label, Gtk.SpinButton, Gtk.SpinButton, Gtk.SpinButton, Gtk.SpinButton]:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.get_style_context().add_class("card")
            hdr = Gtk.Label(label=title, xalign=0.0)
            hdr.get_style_context().add_class(accent_cls)
            card.pack_start(hdr, False, False, 0)

            fixed_lbl = Gtk.Label(
                label="Fixed: Node 1 = 40°C / 50%  •  Node 4 = 100°C / 100%",
                xalign=0.0,
            )
            fixed_lbl.get_style_context().add_class("card-sub")
            card.pack_start(fixed_lbl, False, False, 0)

            grid = Gtk.Grid(column_spacing=8, row_spacing=6)
            t2_spin = Gtk.SpinButton.new_with_range(41, 79, 1)
            t2_spin.set_value(60)
            d2_spin = Gtk.SpinButton.new_with_range(51, 69, 1)
            d2_spin.set_value(60)
            t3_spin = Gtk.SpinButton.new_with_range(61, 99, 1)
            t3_spin.set_value(80)
            d3_spin = Gtk.SpinButton.new_with_range(61, 100, 1)
            d3_spin.set_value(70)

            t2_spin.connect("value-changed", self._on_curve_spin_changed, which, 1)
            d2_spin.connect("value-changed", self._on_curve_spin_changed, which, 1)
            t3_spin.connect("value-changed", self._on_curve_spin_changed, which, 2)
            d3_spin.connect("value-changed", self._on_curve_spin_changed, which, 2)

            grid.attach(Gtk.Label(label="Node 2 Temp (°C):", xalign=0.0), 0, 0, 1, 1)
            grid.attach(t2_spin, 1, 0, 1, 1)
            grid.attach(Gtk.Label(label="Duty (%):", xalign=0.0), 2, 0, 1, 1)
            grid.attach(d2_spin, 3, 0, 1, 1)

            grid.attach(Gtk.Label(label="Node 3 Temp (°C):", xalign=0.0), 0, 1, 1, 1)
            grid.attach(t3_spin, 1, 1, 1, 1)
            grid.attach(Gtk.Label(label="Duty (%):", xalign=0.0), 2, 1, 1, 1)
            grid.attach(d3_spin, 3, 1, 1, 1)

            card.pack_start(grid, False, False, 0)
            return card, fixed_lbl, t2_spin, d2_spin, t3_spin, d3_spin

        (
            cpu_card,
            self.lbl_cpu_fixed_nodes,
            self.cpu_t2_spin,
            self.cpu_d2_spin,
            self.cpu_t3_spin,
            self.cpu_d3_spin,
        ) = build_curve_panel("cpu", "CPU Fan (F1 • 0x038C)", "cpu-accent")
        (
            gpu_card,
            self.lbl_gpu_fixed_nodes,
            self.gpu_t2_spin,
            self.gpu_d2_spin,
            self.gpu_t3_spin,
            self.gpu_d3_spin,
        ) = build_curve_panel("gpu", "GPU Fan (F2 • 0x039A)", "gpu-accent")

        curve_cols.attach(cpu_card, 0, 0, 1, 1)
        curve_cols.attach(gpu_card, 1, 0, 1, 1)
        curve_body.pack_start(curve_cols, False, False, 0)

        curve_btns_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.copy_cpu_to_gpu_btn = Gtk.Button(label="Copy CPU -> GPU")
        self.copy_cpu_to_gpu_btn.get_style_context().add_class("choice-btn")
        self.copy_cpu_to_gpu_btn.connect("clicked", self._on_copy_cpu_to_gpu_clicked)

        self.reset_curve_nodes_btn = Gtk.Button(label="Reset Default Nodes")
        self.reset_curve_nodes_btn.get_style_context().add_class("choice-btn")
        self.reset_curve_nodes_btn.connect("clicked", self._on_reset_curve_nodes_clicked)

        self.apply_custom_curve_btn = Gtk.Button(label="Apply Custom CPU & GPU Curve")
        self.apply_custom_curve_btn.get_style_context().add_class("primary-action")
        self.apply_custom_curve_btn.connect("clicked", self._on_apply_custom_curve_clicked)

        curve_btns_row.pack_start(self.copy_cpu_to_gpu_btn, False, False, 0)
        curve_btns_row.pack_start(self.reset_curve_nodes_btn, False, False, 0)
        curve_btns_row.pack_end(self.apply_custom_curve_btn, False, False, 0)
        curve_body.pack_start(curve_btns_row, False, False, 0)

        # Fan Mode Controls
        mode_sec, mode_body = self._make_section("Fan Curve Modes (0xD7 / 0xD9)")
        vbox.pack_start(mode_sec, False, False, 0)

        self.fan_primary_grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        self.fan_primary_grid.set_column_homogeneous(True)
        self.fan_mode_btns: dict[str, Gtk.Button] = {}
        for idx, (cmd, label, tip) in enumerate(FAN_MODES):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("choice-btn")
            btn.set_tooltip_text(tip)
            btn.connect("clicked", self._on_fan_mode_clicked, cmd)
            self.fan_primary_grid.attach(btn, idx, 0, 1, 1)
            self.fan_mode_btns[cmd] = btn
        mode_body.pack_start(self.fan_primary_grid, False, False, 0)

        # Fan Offset Section
        off_sec, off_body = self._make_section("Fan Speed Offset (0xE7 GFOF)")
        vbox.pack_start(off_sec, False, False, 0)

        off_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        off_row.pack_start(Gtk.Label(label="Offset (%):"), False, False, 0)
        self.fan_offset_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.fan_offset_scale.set_draw_value(False)
        self.fan_offset_scale.set_hexpand(True)
        self.fan_offset_spin = Gtk.SpinButton.new_with_range(0, 100, 1)
        self._link_scale_spin(self.fan_offset_scale, self.fan_offset_spin)
        self.fan_offset_scale.connect("value-changed", self._on_fan_offset_preview_changed)
        self.fan_offset_spin.connect("value-changed", self._on_fan_offset_preview_changed)
        self.apply_fan_offset_btn = Gtk.Button(label="Apply Offset")
        self.apply_fan_offset_btn.get_style_context().add_class("primary-action")
        self.apply_fan_offset_btn.connect("clicked", self._on_apply_fan_offset_clicked)
        off_row.pack_start(self.fan_offset_scale, True, True, 0)
        off_row.pack_start(self.fan_offset_spin, False, False, 0)
        off_row.pack_start(self.apply_fan_offset_btn, False, False, 0)
        off_body.pack_start(off_row, False, False, 0)

        curve_sec.show_all()
        self._sync_fan_lock_state()
        return scroll

    # ---- Tab 3: Keyboard RGB & Backlight ----

    def _build_rgb_tab(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_border_width(12)
        scroll.add(vbox)

        # Status Cards
        stat_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        stat_grid.set_column_homogeneous(True)
        c_bri, self.lbl_bri_state, self.sub_bri_mode = self._make_card(
            "Backlight State (brightness.sh)",
            "—",
            "Mode (KBType=6): 0xCA sub 0x06 (write-only; cached)",
        )
        c_rgb, self.lbl_rgb_state, self.sub_rgb_mailbox = self._make_card(
            "RGB / Effect State (rgb.sh)",
            "—",
            "KBType=6 1-zone (0xF9=0x03) • Mailbox 0xF8..0xFD: —",
        )
        stat_grid.attach(c_bri, 0, 0, 1, 1)
        stat_grid.attach(c_rgb, 1, 0, 1, 1)
        vbox.pack_start(stat_grid, False, False, 0)

        # Brightness Section
        bri_sec, bri_body = self._make_section("Keyboard Backlight Brightness (0xCA Sub 0x06)")
        vbox.pack_start(bri_sec, False, False, 0)

        bri_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bri_row.pack_start(Gtk.Label(label="Level (0..255):"), False, False, 0)
        self.bri_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 255, 1)
        self.bri_scale.set_value(191)
        self.bri_scale.set_draw_value(False)
        self.bri_scale.set_hexpand(True)
        self.bri_spin = Gtk.SpinButton.new_with_range(0, 255, 1)
        self.bri_spin.set_value(191)
        self._link_scale_spin(self.bri_scale, self.bri_spin)
        apply_bri_btn = Gtk.Button(label="Apply Brightness")
        apply_bri_btn.get_style_context().add_class("primary-action")
        apply_bri_btn.connect("clicked", self._on_apply_brightness_clicked)
        bri_row.pack_start(self.bri_scale, True, True, 0)
        bri_row.pack_start(self.bri_spin, False, False, 0)
        bri_row.pack_start(apply_bri_btn, False, False, 0)
        bri_body.pack_start(bri_row, False, False, 0)

        bri_presets_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bri_presets_box.set_homogeneous(True)
        for cmd, label, val in BRIGHTNESS_PRESETS:
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("choice-btn")
            btn.connect("clicked", self._on_brightness_preset_clicked, cmd, val)
            bri_presets_box.pack_start(btn, True, True, 0)
        bri_body.pack_start(bri_presets_box, False, False, 0)

        # Static RGB Color Section (KBType=6 1-zone global 0xCA 0xF9=0x03)
        rgb_sec, rgb_body = self._make_section("Static RGB Color (KBType=6 1-Zone, 0xCA)")
        vbox.pack_start(rgb_sec, False, False, 0)

        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.color_btn = Gtk.ColorButton()
        self.color_btn.set_rgba(Gdk.RGBA(0.0, 0.0, 1.0, 1.0))
        self.color_btn.connect("color-set", self._on_color_button_set)
        color_row.pack_start(self.color_btn, False, False, 0)

        self.r_spin = Gtk.SpinButton.new_with_range(0, 255, 1)
        self.g_spin = Gtk.SpinButton.new_with_range(0, 255, 1)
        self.b_spin = Gtk.SpinButton.new_with_range(0, 255, 1)
        self.b_spin.set_value(255)
        for lbl_text, spin in (("R:", self.r_spin), ("G:", self.g_spin), ("B:", self.b_spin)):
            spin.connect("value-changed", self._on_rgb_spin_changed)
            color_row.pack_start(Gtk.Label(label=lbl_text), False, False, 0)
            color_row.pack_start(spin, False, False, 0)

        self.apply_rgb_btn = Gtk.Button(label="Apply RGB Color")
        self.apply_rgb_btn.get_style_context().add_class("primary-action")
        self.apply_rgb_btn.connect("clicked", self._on_apply_rgb_clicked)
        color_row.pack_start(self.apply_rgb_btn, False, False, 0)
        rgb_body.pack_start(color_row, False, False, 0)

        self.cc_scale_check = Gtk.CheckButton(label="Control Center /2 Scale (--cc-scale)")
        self.cc_scale_check.set_tooltip_text(
            "Apply FnKey/CC40 KBType=6 halving (R/2, G/2, B/2; (0,254,129)/(0,255,127)->B=35)"
        )
        rgb_body.pack_start(self.cc_scale_check, False, False, 0)

        # 11 Quick Preset Chips
        chips_wrap = Gtk.FlowBox()
        chips_wrap.set_selection_mode(Gtk.SelectionMode.NONE)
        chips_wrap.set_row_spacing(6)
        chips_wrap.set_column_spacing(6)
        self.rgb_preset_btns: dict[str, Gtk.Button] = {}
        for preset_name, (pr, pg, pb) in RGB_PRESETS:
            chip = Gtk.Button()
            chip.get_style_context().add_class("color-chip")
            chip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            swatch_pb = _render_svg_pixbuf(_build_swatch_svg(pr, pg, pb, size=14))
            dot = Gtk.Image.new_from_pixbuf(swatch_pb) if swatch_pb is not None else Gtk.Image()
            dot.set_size_request(14, 14)
            dot.set_valign(Gtk.Align.CENTER)
            chip_box.pack_start(dot, False, False, 0)
            chip_box.pack_start(Gtk.Label(label=preset_name), False, False, 0)
            chip.add(chip_box)
            chip.connect("clicked", self._on_rgb_preset_clicked, preset_name, pr, pg, pb)
            self.rgb_preset_btns[preset_name] = chip
            chips_wrap.add(chip)
        rgb_body.pack_start(chips_wrap, False, False, 0)

        # Effects & Sleep Timeout Section
        fx_sec, fx_body = self._make_section("Lighting Effect (0xCA Sub 0x0F) & Backlight Idle Timeout (0xD4)")
        vbox.pack_start(fx_sec, False, False, 0)

        fx_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.rgb_effect_btns: dict[str, Gtk.Button] = {}
        for fx_name, fx_label, fx_tip in RGB_EFFECTS:
            btn = Gtk.Button(label=fx_label)
            btn.set_tooltip_text(fx_tip)
            btn.get_style_context().add_class("choice-btn")
            btn.connect(
                "clicked",
                lambda _b, n=fx_name: self._execute_action("rgb.sh", ["--effect", n], refresh_after=True),
            )
            self.rgb_effect_btns[fx_name] = btn
            fx_row.pack_start(btn, False, False, 0)
        fx_hint = Gtk.Label(
            label="15-color hardware breathing (applying a static color restores 1-zone solid mode)",
            xalign=0.0,
        )
        fx_hint.get_style_context().add_class("card-sub")
        fx_hint.set_line_wrap(True)
        fx_row.pack_start(fx_hint, True, True, 0)
        fx_body.pack_start(fx_row, False, False, 0)

        to_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        to_row.pack_start(Gtk.Label(label="Backlight Idle Timeout (sec, 5..3600):"), False, False, 0)
        self.timeout_spin = Gtk.SpinButton.new_with_range(5, 3600, 5)
        self.timeout_spin.set_value(60)
        self.set_timeout_btn = Gtk.Button(label="Set Timeout")
        self.set_timeout_btn.get_style_context().add_class("primary-action")
        self.set_timeout_btn.connect("clicked", self._on_apply_timeout_clicked)
        self.disable_timeout_btn = Gtk.Button(label="Disable Timeout (Always On)")
        self.disable_timeout_btn.get_style_context().add_class("choice-btn")
        self.disable_timeout_btn.connect(
            "clicked",
            lambda *_: self._execute_action("rgb.sh", ["--timeout", "off"], refresh_after=True),
        )
        to_row.pack_start(self.timeout_spin, False, False, 0)
        to_row.pack_start(self.set_timeout_btn, False, False, 0)
        to_row.pack_start(self.disable_timeout_btn, False, False, 0)
        fx_body.pack_start(to_row, False, False, 0)

        return scroll

    # ---- Tab 4: Battery & FlexiCharger ----

    def _build_battery_tab(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_border_width(12)
        scroll.add(vbox)

        # Telemetry Cards
        cards_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        cards_grid.set_column_homogeneous(True)
        c1, self.lbl_bat_sysfs, _ = self._make_card("Linux BAT0 Status & Capacity")
        c2, self.lbl_bat_ec, _ = self._make_card("EC AC / BAT0 / Remaining% / Cycles")
        c3, self.lbl_bat_flexi, self.sub_bat_flexi = self._make_card(
            "Active FlexiCharger (Direct EC 0xCB / XRAM 0x06D0..0x06D2)",
            "—",
            "Direct EC 0xCB / XRAM 0x06D0..0x06D2: —",
        )
        cards_grid.attach(c1, 0, 0, 1, 1)
        cards_grid.attach(c2, 1, 0, 1, 1)
        cards_grid.attach(c3, 0, 1, 2, 1)
        vbox.pack_start(cards_grid, False, False, 0)

        # Presets
        pre_sec, pre_body = self._make_section(
            "FlexiCharger Presets (Direct EC 0xCB / XRAM 0x06D0..0x06D2)"
        )
        vbox.pack_start(pre_sec, False, False, 0)

        pre_grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        pre_grid.set_column_homogeneous(True)
        self.battery_preset_btns: dict[str, Gtk.Button] = {}
        for idx, (label, args) in enumerate(BATTERY_PRESETS):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("choice-btn")
            btn.set_tooltip_text(
                f"Run `battery.sh {' '.join(args)}` via Direct EC 0xCB mailbox with live XRAM 0x06D0..0x06D2 readback (ec_sys)"
            )
            btn.connect("clicked", lambda _b, a=args: self._execute_action("battery.sh", list(a), refresh_after=True))
            pre_grid.attach(btn, idx % 2, idx // 2, 1, 1)
            key = f"{args[1]}-{args[2]}" if args[0] == "limit" else args[0]
            self.battery_preset_btns[key] = btn
        pre_body.pack_start(pre_grid, False, False, 0)

        # Custom Thresholds
        cust_sec, cust_body = self._make_section(
            "Custom Charge Thresholds (Direct EC 0xCB / XRAM 0x06D0..0x06D2)"
        )
        vbox.pack_start(cust_sec, False, False, 0)

        thr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        thr_row.pack_start(Gtk.Label(label="Start Charging Below (%):"), False, False, 0)
        self.bat_start_spin = Gtk.SpinButton.new_with_range(20, 95, 1)
        self.bat_start_spin.set_value(60)
        thr_row.pack_start(self.bat_start_spin, False, False, 0)

        thr_row.pack_start(Gtk.Label(label="Stop Charging At (%):"), False, False, 0)
        self.bat_stop_spin = Gtk.SpinButton.new_with_range(30, 100, 1)
        self.bat_stop_spin.set_value(80)
        thr_row.pack_start(self.bat_stop_spin, False, False, 0)

        self.apply_bat_btn = Gtk.Button(label="Apply Custom Limit")
        self.apply_bat_btn.get_style_context().add_class("primary-action")
        self.apply_bat_btn.set_tooltip_text(
            "Apply custom FlexiCharger thresholds via Direct EC 0xCB with live XRAM 0x06D0..0x06D2 readback (ec_sys)"
        )
        self.apply_bat_btn.connect("clicked", self._on_apply_battery_custom_clicked)
        thr_row.pack_end(self.apply_bat_btn, False, False, 0)
        cust_body.pack_start(thr_row, False, False, 0)

        self.bat_warn_label = Gtk.Label(xalign=0.0)
        self.bat_warn_label.get_style_context().add_class("warn-label")
        cust_body.pack_start(self.bat_warn_label, False, False, 0)

        self.bat_start_spin.connect("value-changed", lambda *_: self._validate_battery_spins())
        self.bat_stop_spin.connect("value-changed", lambda *_: self._validate_battery_spins())
        self._validate_battery_spins()

        return scroll

    # ---- Tab 5: Peripherals & Lid ----

    def _build_peripherals_tab(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_border_width(12)
        scroll.add(vbox)

        # Webcam & Trackpad (EC Mailbox 0xB8)
        dev_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        dev_grid.set_column_homogeneous(True)

        cam_sec, cam_body = self._make_section("Webcam (CCD Hardware USB Power Cut • 0xB8 0xA1)")
        cam_card, self.lbl_webcam_state, self.sub_webcam_state = self._make_card(
            "Webcam Status", "—", "EC 0xB8 bit0 / /dev/video0"
        )
        cam_body.pack_start(cam_card, False, False, 0)
        cam_btn_row = Gtk.Grid(column_spacing=8, row_spacing=8)
        cam_btn_row.set_column_homogeneous(True)
        self.webcam_btns: dict[str, Gtk.Button] = {}
        for idx, (arg, label) in enumerate((("on", "Enable Webcam"), ("off", "Cut Webcam Power"))):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("choice-btn")
            btn.connect("clicked", lambda _b, a=arg: self._execute_action("peripherals.sh", ["webcam", a], refresh_after=True))
            cam_btn_row.attach(btn, idx, 0, 1, 1)
            self.webcam_btns[arg] = btn
        cam_body.pack_start(cam_btn_row, False, False, 0)

        tp_sec, tp_body = self._make_section("Trackpad (ELAN0412 + EC 0xB8 0xA5)")
        tp_card, self.lbl_trackpad_state, self.sub_trackpad_state = self._make_card(
            "Trackpad Status", "—", "EC 0xB8 bit4 / input*/inhibited"
        )
        tp_body.pack_start(tp_card, False, False, 0)
        tp_btn_row = Gtk.Grid(column_spacing=8, row_spacing=8)
        tp_btn_row.set_column_homogeneous(True)
        self.trackpad_btns: dict[str, Gtk.Button] = {}
        for idx, (arg, label) in enumerate((("on", "Enable Trackpad"), ("off", "Disable Trackpad"))):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("choice-btn")
            btn.connect("clicked", lambda _b, a=arg: self._execute_action("peripherals.sh", ["touchpad", a], refresh_after=True))
            tp_btn_row.attach(btn, idx, 0, 1, 1)
            self.trackpad_btns[arg] = btn
        tp_body.pack_start(tp_btn_row, False, False, 0)

        dev_grid.attach(cam_sec, 0, 0, 1, 1)
        dev_grid.attach(tp_sec, 1, 0, 1, 1)
        vbox.pack_start(dev_grid, False, False, 0)

        # Lid Controls (EC[0x03] LSTE/LWKE & systemd-inhibit)
        lid_sec, lid_body = self._make_section("Lid Controls (EC[0x03] LSTE/LWKE & systemd-inhibit)")
        vbox.pack_start(lid_sec, False, False, 0)

        lid_cards = Gtk.Grid(column_spacing=10, row_spacing=10)
        lid_cards.set_column_homogeneous(True)
        c_lid_state, self.lbl_lid_state, self.sub_lid_state = self._make_card(
            "Live Lid State", "—", "EC 0x03 LSTE"
        )
        c_lid_wake, self.lbl_lid_wake_state, self.sub_lid_wake_state = self._make_card(
            "Wake on Lid Open", "—", "EC LWKE / PNP0C0D:00"
        )
        c_lid_close, self.lbl_lid_close_state, self.sub_lid_close_state = self._make_card(
            "Lid-Close Action", "—", "systemd-inhibit handle-lid-switch"
        )
        lid_cards.attach(c_lid_state, 0, 0, 1, 1)
        lid_cards.attach(c_lid_wake, 1, 0, 1, 1)
        lid_cards.attach(c_lid_close, 2, 0, 1, 1)
        lid_body.pack_start(lid_cards, False, False, 0)

        lid_ctrl_grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        lid_ctrl_grid.set_column_homogeneous(True)

        wake_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        wake_box.set_homogeneous(True)
        self.lid_wake_btns: dict[str, Gtk.Button] = {}
        for arg, label in (("on", "Wake on Lid: ON"), ("off", "Wake on Lid: OFF")):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("choice-btn")
            btn.connect("clicked", lambda _b, a=arg: self._execute_action("peripherals.sh", ["lid-wake", a], refresh_after=True))
            wake_box.pack_start(btn, True, True, 0)
            self.lid_wake_btns[arg] = btn

        close_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        close_box.set_homogeneous(True)
        self.lid_close_btns: dict[str, Gtk.Button] = {}
        for arg, label in (("suspend", "Suspend on Close"), ("ignore", "Ignore / Clamshell")):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("choice-btn")
            btn.connect("clicked", lambda _b, a=arg: self._execute_action("peripherals.sh", ["lid-close", a], refresh_after=True))
            close_box.pack_start(btn, True, True, 0)
            self.lid_close_btns[arg] = btn

        lid_ctrl_grid.attach(wake_box, 0, 0, 1, 1)
        lid_ctrl_grid.attach(close_box, 1, 0, 1, 1)
        lid_body.pack_start(lid_ctrl_grid, False, False, 0)

        # Keyboard Hardware Locks (EC[0xE2] / EC[0xEC])
        kbd_sec, kbd_body = self._make_section("Keyboard Hardware Locks (EC[0xE2] / EC[0xEC])")
        vbox.pack_start(kbd_sec, False, False, 0)

        kbd_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        kbd_grid.set_column_homogeneous(True)

        self.win_lock_btns: dict[str, Gtk.Button] = {}
        self.fn_swap_btns: dict[str, Gtk.Button] = {}
        self.fn_lock_btns: dict[str, Gtk.Button] = {}

        lock_specs = [
            ("Win-Key Lock (0xE2 bit4)", "win-lock", self.win_lock_btns, "lbl_win_lock_state", "sub_win_lock_state"),
            ("Fn <-> Win Swap (0xE2 bit3)", "fn-swap", self.fn_swap_btns, "lbl_fn_swap_state", "sub_fn_swap_state"),
            ("Fn-Lock (0xEC bit5)", "fn-lock", self.fn_lock_btns, "lbl_fn_lock_state", "sub_fn_lock_state"),
        ]
        for idx, (title, subcmd, btn_map, val_attr, sub_attr) in enumerate(lock_specs):
            card, val_lbl, sub_lbl = self._make_card(title, "—", "EC lock bit")
            setattr(self, val_attr, val_lbl)
            setattr(self, sub_attr, sub_lbl)
            btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            btn_row.set_homogeneous(True)
            for arg, btn_lbl in (("on", "ON"), ("off", "OFF")):
                btn = Gtk.Button(label=btn_lbl)
                btn.get_style_context().add_class("choice-btn")
                btn.connect(
                    "clicked",
                    lambda _b, sc=subcmd, a=arg: self._execute_action("peripherals.sh", [sc, a], refresh_after=True),
                )
                btn_row.pack_start(btn, True, True, 0)
                btn_map[arg] = btn
            card.pack_start(btn_row, False, False, 4)
            kbd_grid.attach(card, idx, 0, 1, 1)

        kbd_body.pack_start(kbd_grid, False, False, 0)
        return scroll

    def _validate_battery_spins(self) -> bool:
        start = self.bat_start_spin.get_value_as_int()
        stop = self.bat_stop_spin.get_value_as_int()
        valid = start < stop
        if valid:
            self.bat_warn_label.set_text("")
        else:
            self.bat_warn_label.set_text(
                f"Invalid thresholds: Start ({start}%) must be strictly less than Stop ({stop}%)."
            )
        self.apply_bat_btn.set_sensitive(valid)
        return valid

    # ---- Collapsible Command Log ----

    def _build_log_expander(self) -> Gtk.Expander:
        exp = Gtk.Expander(label="Command & Telemetry Log")
        exp.set_expanded(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)

        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(135)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.get_style_context().add_class("cmd-log")
        self.log_buf = self.log_view.get_buffer()
        self.log_end_mark = self.log_buf.create_mark("end", self.log_buf.get_end_iter(), False)

        scroll.add(self.log_view)
        box.pack_start(scroll, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        clear_btn = Gtk.Button(label="Clear Log")
        clear_btn.connect("clicked", lambda *_: self.log_buf.set_text(""))
        btn_row.pack_end(clear_btn, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        exp.add(box)
        self.log_expander = exp
        return exp

    def _append_log(self, msg: str) -> None:
        if self._destroyed:
            return
        ts = time.strftime("%H:%M:%S")
        end_iter = self.log_buf.get_end_iter()
        self.log_buf.insert(end_iter, f"[{ts}] {msg}\n")
        self.log_view.scroll_to_mark(self.log_end_mark, 0.0, False, 0.0, 0.0)

    def _log_script_result(self, res: ScriptResult) -> None:
        cmd_str = " ".join(shlex.quote(x) for x in res.effective_cmd)
        lines = [f"$ {cmd_str}  [{res.mode}, rc={res.rc}, {res.duration_ms:.0f}ms]"]
        if res.note:
            lines.append(f"  Note: {res.note}")
        if res.stdout.strip():
            for ln in res.stdout.rstrip().splitlines():
                lines.append(f"  {ln}")
        if res.stderr.strip():
            for ln in res.stderr.rstrip().splitlines():
                lines.append(f"  [stderr] {ln}")
        self._append_log("\n".join(lines))

    # ---- Shared Scale/Spin & Signal Handlers ----

    def _should_sync_input(self, key: str, value: Any, sync_all: bool) -> bool:
        prev = self._last_backend_inputs.get(key)
        self._last_backend_inputs[key] = value
        return sync_all or prev != value

    def _link_scale_spin(self, scale: Gtk.Scale, spin: Gtk.SpinButton) -> None:
        def on_scale(_s: Gtk.Scale) -> None:
            if self._updating_ui:
                return
            with self._ui_update_guard():
                spin.set_value(round(scale.get_value()))

        def on_spin(_s: Gtk.SpinButton) -> None:
            if self._updating_ui:
                return
            with self._ui_update_guard():
                scale.set_value(spin.get_value())

        scale.connect("value-changed", on_scale)
        spin.connect("value-changed", on_spin)

    def _on_color_button_set(self, btn: Gtk.ColorButton) -> None:
        if self._updating_ui:
            return
        rgba = btn.get_rgba()
        with self._ui_update_guard():
            self.r_spin.set_value(max(0, min(255, round(rgba.red * 255))))
            self.g_spin.set_value(max(0, min(255, round(rgba.green * 255))))
            self.b_spin.set_value(max(0, min(255, round(rgba.blue * 255))))

    def _on_rgb_spin_changed(self, _spin: Gtk.SpinButton) -> None:
        if self._updating_ui:
            return
        with self._ui_update_guard():
            r = self.r_spin.get_value_as_int() / 255.0
            g = self.g_spin.get_value_as_int() / 255.0
            b = self.b_spin.get_value_as_int() / 255.0
            self.color_btn.set_rgba(Gdk.RGBA(r, g, b, 1.0))

    def _on_unlock_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._updating_ui:
            return
        if self._unlock_btn_daemon_mode:
            btn.set_sensitive(False)
            btn.set_label("Checking Daemon…")

            def daemon_check() -> None:
                ok, msg = self.runner.ping_daemon(timeout=0.4)

                def finish() -> bool:
                    if self._destroyed:
                        return False
                    self._sync_unlock_button()
                    if ok:
                        self._banner_alert = None
                        self._append_log(f"Daemon status OK: {msg}")
                    else:
                        self._banner_alert = ("warn", msg)
                        self._append_log(f"Daemon connection lost: {msg}")
                    self._update_mode_banner()
                    self.refresh_all(quiet_log=True)
                    return False

                GLib.idle_add(finish)

            threading.Thread(target=daemon_check, daemon=True).start()
            return

        if btn.get_active():
            if not self.runner.explicit_ec_file and self.runner.daemon_available():
                self._sync_unlock_button()
                self._banner_alert = None
                self._update_mode_banner()
                self._append_log("Daemon Connected (Live EC); no pkexec prompt needed.")
                self.refresh_all(quiet_log=True)
                return
            btn.set_sensitive(False)
            btn.set_label("Unlocking…")

            def worker_start() -> None:
                ok, msg = self.runner.start_worker(use_pkexec=True)

                def finish() -> bool:
                    if self._destroyed:
                        return False
                    self._sync_unlock_button()
                    self._banner_alert = None if ok else ("warn", msg)
                    self._update_mode_banner()
                    self._append_log(msg)
                    if ok:
                        self.refresh_all()
                    return False

                GLib.idle_add(finish)

            threading.Thread(target=worker_start, daemon=True).start()
        else:
            btn.set_sensitive(False)
            btn.set_label("Locking…")

            def worker_stop() -> None:
                self.runner.stop_worker()

                def finish() -> bool:
                    if self._destroyed:
                        return False
                    self._sync_unlock_button()
                    self._banner_alert = None
                    self._update_mode_banner()
                    self._append_log("Locked Live EC worker (returned to unprivileged mode).")
                    self.refresh_all()
                    return False

                GLib.idle_add(finish)

            threading.Thread(target=worker_stop, daemon=True).start()

    def _on_dry_switch_toggled(self, sw: Gtk.Switch, _pspec: Any) -> None:
        self.runner.dry_run = sw.get_active()
        self._banner_alert = None
        self._update_mode_banner()
        self._append_log(f"Dry-Run mode {'ENABLED' if self.runner.dry_run else 'DISABLED'}.")
        if not self.runner.dry_run:
            self.refresh_all()

    def _spawn_bg_thread(self, target: Any, name: str) -> threading.Thread:
        def wrapped() -> None:
            try:
                target()
            finally:
                with self._bg_threads_lock:
                    self._bg_threads.discard(threading.current_thread())

        th = threading.Thread(target=wrapped, name=name, daemon=True)
        with self._bg_threads_lock:
            self._bg_threads.add(th)
        th.start()
        return th

    def wait_background_tasks(self, timeout: float = 5.0) -> None:
        """Wait for any tracked background config/startup threads (used cleanly in --self-test)."""
        with self._bg_threads_lock:
            threads = list(self._bg_threads)
        for th in threads:
            if th.is_alive():
                th.join(timeout=timeout)

    def _sync_auto_apply_switch(self, enabled: Optional[bool] = None) -> None:
        if enabled is None:
            enabled = bool(self.runner.load_saved_config(ensure=False).get("auto_apply_on_launch", True))
        with self._ui_update_guard():
            self.auto_apply_switch.set_active(bool(enabled))

    def _log_apply_config_result(self, res: ApplyConfigResult, is_launch: bool = False) -> None:
        prefix = "Startup auto-apply" if is_launch else "Apply Config"
        summary = f"{prefix} [{res.mode}]: {res.message}"
        lines = [summary]
        for item in res.applied:
            act_args = " ".join(shlex.quote(str(a)) for a in (item.get("args") or []))
            act_rc = item.get("rc", "?")
            lines.append(f"  -> {item.get('script')} {act_args} (rc={act_rc})".rstrip())
        for err in res.errors:
            lines.append(f"  [error] {err}")
        self._append_log("\n".join(lines))

    def _finish_apply_config_ui(self, res: ApplyConfigResult, is_launch: bool) -> None:
        if self._destroyed:
            return
        self._last_apply_config_result = res
        self.apply_config_btn.set_sensitive(True)
        self._sync_unlock_button()
        self._sync_auto_apply_switch(bool(res.config.get("auto_apply_on_launch", True)))
        if res.errors and not res.skipped:
            self._banner_alert = ("warn", res.errors[0])
        elif res.mode == "snapshot-dry-run" and res.applied:
            self._banner_alert = (
                "dry-run",
                f"[DRY-RUN FALLBACK - Unlock Live EC to apply to hardware] Previewed {len(res.applied)} saved action(s).",
            )
        else:
            self._banner_alert = None
        self._update_mode_banner()
        self._log_apply_config_result(res, is_launch=is_launch)
        self.refresh_all(quiet_log=False, from_poll=False)

    def _start_launch_auto_apply(self) -> bool:
        if self._destroyed:
            return False

        def worker() -> None:
            res = self.runner.apply_saved_config(force=False)
            GLib.idle_add(lambda: (self._finish_apply_config_ui(res, is_launch=True), False)[1])

        self._spawn_bg_thread(worker, name="GuiStartupAutoApply")
        return False

    def _on_apply_config_clicked(self, _btn: Gtk.Button) -> None:
        self.apply_config_btn.set_sensitive(False)

        def worker() -> None:
            res = self.runner.apply_saved_config(force=True)
            GLib.idle_add(lambda: (self._finish_apply_config_ui(res, is_launch=False), False)[1])

        self._spawn_bg_thread(worker, name="GuiApplyConfig")

    def _on_auto_apply_switch_toggled(self, sw: Gtk.Switch, _pspec: Any) -> None:
        if self._updating_ui:
            return
        enabled = bool(sw.get_active())

        def worker() -> None:
            err_msg = ""
            via = "local"
            try:
                _ok, saved, via = self.runner.set_auto_apply_on_launch(enabled)
                actual = bool(saved.get("auto_apply_on_launch", enabled))
            except Exception as exc:  # noqa: BLE001
                actual = not enabled
                err_msg = str(exc)

            def on_done() -> bool:
                if self._destroyed:
                    return False
                self._sync_auto_apply_switch(actual)
                if err_msg:
                    self._banner_alert = ("warn", f"Failed to save auto_apply_on_launch: {err_msg}")
                    self._update_mode_banner()
                    self._append_log(f"Failed to update auto_apply_on_launch: {err_msg}")
                else:
                    self._append_log(
                        f"Auto-apply on launch {'ENABLED' if actual else 'DISABLED'} "
                        f"(saved via {via} to {self.runner.config_path})."
                    )
                return False

            GLib.idle_add(on_done)

        self._spawn_bg_thread(worker, name="GuiAutoApplySave")

    def _on_poll_switch_toggled(self, sw: Gtk.Switch, _pspec: Any) -> None:
        if self._poll_source_id is not None:
            GLib.source_remove(self._poll_source_id)
            self._poll_source_id = None
        if sw.get_active():
            self._poll_source_id = GLib.timeout_add_seconds(2, self._on_poll_tick)
            self.refresh_all()

    def _on_poll_tick(self) -> bool:
        if self._destroyed or not self.poll_switch.get_active():
            self._poll_source_id = None
            return False
        self.refresh_all(quiet_log=True, from_poll=True)
        return True

    def _on_power_profile_clicked(self, _btn: Gtk.Button, profile_key: str) -> None:
        self._selected_power_profile = profile_key
        args = self._build_power_args(profile_key)
        self._execute_action("power_profile.sh", args, refresh_after=True)

    def _on_apply_custom_power_clicked(self, _btn: Gtk.Button) -> None:
        args = self._build_power_args(self._selected_power_profile)
        self._execute_action("power_profile.sh", args, refresh_after=True)

    def _is_fan_locked(self) -> bool:
        return self._active_power_profile in ("quiet", "powersaving")

    def _sync_fan_lock_state(self) -> None:
        locked = self._is_fan_locked()
        if locked:
            self._dragging_curve_node = None
            self._hovered_curve_node = None
            self._set_graph_cursor(None)
            self._set_active_choice(self.fan_mode_btns, None)
            self.fan_lock_banner_label.set_text(self._FAN_LOCK_BANNER_TEXT)
            self.fan_lock_banner_box.show()
            self.fan_lock_banner_label.show()
        else:
            self.fan_lock_banner_box.hide()
            self._set_active_choice(self.fan_mode_btns, self._fan_mode_key)

        self.fan_curve_sec.set_visible(True)

        for btn in self.fan_mode_btns.values():
            btn.set_sensitive(not locked)

        for ctrl in (
            self.fan_offset_scale,
            self.fan_offset_spin,
            self.apply_fan_offset_btn,
            self.cpu_t2_spin,
            self.cpu_d2_spin,
            self.cpu_t3_spin,
            self.cpu_d3_spin,
            self.gpu_t2_spin,
            self.gpu_d2_spin,
            self.gpu_t3_spin,
            self.gpu_d3_spin,
            self.copy_cpu_to_gpu_btn,
            self.reset_curve_nodes_btn,
            self.apply_custom_curve_btn,
            self.fan_graph_eventbox,
        ):
            ctrl.set_sensitive(not locked)

        self._update_fan_graph()

    def _curve_spin_tuple(
        self, which: str
    ) -> tuple[Gtk.SpinButton, Gtk.SpinButton, Gtk.SpinButton, Gtk.SpinButton]:
        if which == "cpu":
            return (self.cpu_t2_spin, self.cpu_d2_spin, self.cpu_t3_spin, self.cpu_d3_spin)
        return (self.gpu_t2_spin, self.gpu_d2_spin, self.gpu_t3_spin, self.gpu_d3_spin)

    def _sync_curve_spins(self, which: str, curve: Sequence[tuple[int, int]]) -> None:
        pts = _normalize_4pt_curve(curve)
        t1, d1 = pts[0]
        t2, d2 = pts[1]
        t3, d3 = pts[2]
        t4, d4 = pts[3]
        t2_s, d2_s, t3_s, d3_s = self._curve_spin_tuple(which)
        lbl_fixed = self.lbl_cpu_fixed_nodes if which == "cpu" else self.lbl_gpu_fixed_nodes
        with self._ui_update_guard():
            t2_s.set_range(t1 + 1, 98)
            t3_s.set_range(t1 + 2, 99)
            d2_s.set_range(d1 + 1, 99)
            d3_s.set_range(d1 + 2, 100)
            t2_s.set_value(t2)
            d2_s.set_value(d2)
            t3_s.set_value(t3)
            d3_s.set_value(d3)
            t2_s.set_range(t1 + 1, min(98, t3 - 1))
            t3_s.set_range(max(t1 + 2, t2 + 1), 99)
            d2_s.set_range(d1 + 1, min(99, d3 - 1))
            d3_s.set_range(max(d1 + 2, d2 + 1), 100)
            lbl_fixed.set_text(
                f"Fixed: Node 1 = {t1}°C / {d1}%  •  Node 4 = {t4}°C / {d4}%"
            )

    def _apply_drag_to_curve_node(
        self, which: str, node_idx: int, px: float, py: float
    ) -> tuple[int, int]:
        if self._is_fan_locked():
            cur = self._cpu_curve if which == "cpu" else self._gpu_curve
            return cur[node_idx]
        temp, duty = fan_graph_px_to_temp_duty(px, py, width=FAN_GRAPH_WIDTH, height=FAN_GRAPH_HEIGHT)
        cur = self._cpu_curve if which == "cpu" else self._gpu_curve
        updated = clamp_fan_curve_node(cur, node_idx, temp, duty)
        if which == "cpu":
            self._cpu_curve = updated
        else:
            self._gpu_curve = updated
        self._curve_dirty = True
        self._sync_curve_spins(which, updated)
        self._update_fan_graph()
        return updated[node_idx]

    def _set_graph_cursor(self, name: Optional[str]) -> None:
        gdk_win = self.fan_graph_eventbox.get_window()
        if gdk_win is None:
            return
        if name is None:
            gdk_win.set_cursor(None)
        else:
            cursor = Gdk.Cursor.new_from_name(gdk_win.get_display(), name)
            gdk_win.set_cursor(cursor)

    def _on_fan_graph_button_press(self, _widget: Gtk.Widget, event: Any) -> bool:
        if self._is_fan_locked():
            self._dragging_curve_node = None
            self._hovered_curve_node = None
            self._set_graph_cursor(None)
            return False
        if getattr(event, "button", 1) != 1:
            return False
        hit = hit_test_fan_curve_node(
            float(event.x),
            float(event.y),
            self._cpu_curve,
            self._gpu_curve,
            width=FAN_GRAPH_WIDTH,
            height=FAN_GRAPH_HEIGHT,
        )
        if hit is None:
            return False
        self._dragging_curve_node = hit
        self._hovered_curve_node = hit
        self._set_graph_cursor("grabbing")
        self._apply_drag_to_curve_node(hit[0], hit[1], float(event.x), float(event.y))
        return True

    def _on_fan_graph_motion(self, _widget: Gtk.Widget, event: Any) -> bool:
        if self._is_fan_locked():
            self._dragging_curve_node = None
            self._hovered_curve_node = None
            self._set_graph_cursor(None)
            return False
        if self._dragging_curve_node is not None:
            which, idx = self._dragging_curve_node
            self._apply_drag_to_curve_node(which, idx, float(event.x), float(event.y))
            return True
        hit = hit_test_fan_curve_node(
            float(event.x),
            float(event.y),
            self._cpu_curve,
            self._gpu_curve,
            width=FAN_GRAPH_WIDTH,
            height=FAN_GRAPH_HEIGHT,
        )
        if hit != self._hovered_curve_node:
            self._hovered_curve_node = hit
            self._set_graph_cursor("grab" if hit is not None else None)
            self._update_fan_graph()
        return False

    def _on_fan_graph_button_release(self, _widget: Gtk.Widget, event: Any) -> bool:
        if self._is_fan_locked():
            self._dragging_curve_node = None
            self._hovered_curve_node = None
            self._set_graph_cursor(None)
            return False
        if getattr(event, "button", 1) != 1 or self._dragging_curve_node is None:
            return False
        which, idx = self._dragging_curve_node
        self._apply_drag_to_curve_node(which, idx, float(event.x), float(event.y))
        self._dragging_curve_node = None
        hit = hit_test_fan_curve_node(
            float(event.x),
            float(event.y),
            self._cpu_curve,
            self._gpu_curve,
            width=FAN_GRAPH_WIDTH,
            height=FAN_GRAPH_HEIGHT,
        )
        self._hovered_curve_node = hit
        self._set_graph_cursor("grab" if hit is not None else None)
        self._update_fan_graph()
        return True

    def _on_curve_spin_changed(self, _spin: Gtk.SpinButton, which: str, node_idx: int) -> None:
        if self._updating_ui:
            return
        curve = self._cpu_curve if which == "cpu" else self._gpu_curve
        t2_s, d2_s, t3_s, d3_s = self._curve_spin_tuple(which)
        raw_t = t2_s.get_value_as_int() if node_idx == 1 else t3_s.get_value_as_int()
        raw_d = d2_s.get_value_as_int() if node_idx == 1 else d3_s.get_value_as_int()
        updated = clamp_fan_curve_node(curve, node_idx, raw_t, raw_d)
        if which == "cpu":
            self._cpu_curve = updated
        else:
            self._gpu_curve = updated
        self._curve_dirty = True
        self._sync_curve_spins(which, updated)
        self._update_fan_graph()

    def _on_copy_cpu_to_gpu_clicked(self, _btn: Gtk.Button) -> None:
        if self._is_fan_locked():
            return
        self._gpu_curve = _normalize_4pt_curve(self._cpu_curve)
        self._sync_curve_spins("gpu", self._gpu_curve)
        self._curve_dirty = True
        self._update_fan_graph()

    def _on_reset_curve_nodes_clicked(self, _btn: Gtk.Button) -> None:
        if self._is_fan_locked():
            return
        self._cpu_curve = list(CUSTOM_4PT_WAYPOINTS)
        self._gpu_curve = list(CUSTOM_4PT_WAYPOINTS)
        self._sync_curve_spins("cpu", self._cpu_curve)
        self._sync_curve_spins("gpu", self._gpu_curve)
        self._curve_dirty = False
        self._update_fan_graph()

    def _on_apply_custom_curve_clicked(self, _btn: Gtk.Button) -> None:
        if self._is_fan_locked():
            return
        for spin in (
            self.cpu_t2_spin,
            self.cpu_d2_spin,
            self.cpu_t3_spin,
            self.cpu_d3_spin,
            self.gpu_t2_spin,
            self.gpu_d2_spin,
            self.gpu_t3_spin,
            self.gpu_d3_spin,
        ):
            spin.update()
        c_t1, c_d1 = self._cpu_curve[0]
        g_t1, g_d1 = self._gpu_curve[0]
        self._cpu_curve = _normalize_4pt_curve([
            (c_t1, c_d1),
            (self.cpu_t2_spin.get_value_as_int(), self.cpu_d2_spin.get_value_as_int()),
            (self.cpu_t3_spin.get_value_as_int(), self.cpu_d3_spin.get_value_as_int()),
            (100, 100),
        ])
        self._gpu_curve = _normalize_4pt_curve([
            (g_t1, g_d1),
            (self.gpu_t2_spin.get_value_as_int(), self.gpu_d2_spin.get_value_as_int()),
            (self.gpu_t3_spin.get_value_as_int(), self.gpu_d3_spin.get_value_as_int()),
            (100, 100),
        ])
        self._sync_curve_spins("cpu", self._cpu_curve)
        self._sync_curve_spins("gpu", self._gpu_curve)
        self._curve_dirty = False
        self._fan_mode_key = "custom"
        self._fan_mode_text = "custom (0x04)"
        self._set_active_choice(self.fan_mode_btns, "custom")
        self._update_fan_graph()
        args = [
            "custom",
            str(self._cpu_curve[1][0]),
            str(self._cpu_curve[1][1]),
            str(self._cpu_curve[2][0]),
            str(self._cpu_curve[2][1]),
            str(self._gpu_curve[1][0]),
            str(self._gpu_curve[1][1]),
            str(self._gpu_curve[2][0]),
            str(self._gpu_curve[2][1]),
        ]
        self._execute_action("fan.sh", args, refresh_after=True)

    def _on_fan_mode_clicked(self, _btn: Gtk.Button, cmd: str) -> None:
        if self._is_fan_locked():
            return
        effective_key = "auto" if cmd == "reset" else cmd
        if cmd == "reset":
            self._curve_dirty = False
            self._cpu_curve = list(CUSTOM_4PT_WAYPOINTS)
            self._gpu_curve = list(CUSTOM_4PT_WAYPOINTS)
            self._sync_curve_spins("cpu", self._cpu_curve)
            self._sync_curve_spins("gpu", self._gpu_curve)
        self._fan_mode_key = effective_key
        spec = FAN_CURVE_SPECS.get(effective_key)
        self._fan_mode_text = spec[0] if spec else effective_key
        self._set_active_choice(self.fan_mode_btns, effective_key)
        self._update_fan_graph()
        self._execute_action("fan.sh", [cmd], refresh_after=True)

    def _on_fan_offset_preview_changed(self, widget: Gtk.Widget) -> None:
        if self._updating_ui:
            return
        val = (
            widget.get_value_as_int()
            if isinstance(widget, Gtk.SpinButton)
            else int(round(self.fan_offset_scale.get_value()))
        )
        val = max(0, min(100, val))
        if val != self._preview_offset_pct:
            self._preview_offset_pct = val
            self._update_fan_graph()

    def _on_apply_fan_offset_clicked(self, _btn: Gtk.Button) -> None:
        if self._is_fan_locked():
            return
        self.fan_offset_spin.update()
        val = self.fan_offset_spin.get_value_as_int()
        self._preview_offset_pct = max(0, min(100, val))
        self._update_fan_graph()
        self._execute_action("fan.sh", ["offset", str(val)], refresh_after=True)

    def _update_fan_graph(self) -> None:
        cpu_t, cpu_d, cpu_r = self._latest_cpu_telemetry or (None, None, None)
        gpu_t, gpu_d, gpu_r = self._latest_gpu_telemetry or (None, None, None)
        is_preview = (
            self._live_offset_pct is not None
            and self._preview_offset_pct != self._live_offset_pct
        )
        locked = self._is_fan_locked()
        self.lbl_fan_graph_note.set_text(
            self._FAN_GRAPH_NOTE_LOCKED if locked else self._FAN_GRAPH_NOTE_UNLOCKED
        )
        active_node = None if locked else (self._dragging_curve_node or self._hovered_curve_node)
        svg_str = build_fan_curve_svg(
            fan_mode_key=self._fan_mode_key,
            fan_mode_label=self._fan_mode_text,
            offset_pct=self._preview_offset_pct,
            cpu_temp=cpu_t,
            cpu_duty_pct=cpu_d,
            cpu_rpm=cpu_r,
            gpu_temp=gpu_t,
            gpu_duty_pct=gpu_d,
            gpu_rpm=gpu_r,
            cpu_history=tuple(self._cpu_telemetry_history),
            gpu_history=tuple(self._gpu_telemetry_history),
            offset_is_preview=is_preview,
            cpu_curve=self._cpu_curve,
            gpu_curve=self._gpu_curve,
            active_node=active_node,
            width=FAN_GRAPH_WIDTH,
            height=FAN_GRAPH_HEIGHT,
            fan_locked=locked,
        )
        pb = _render_svg_pixbuf(svg_str)
        if pb is not None:
            self.fan_graph_image.set_from_pixbuf(pb)

    def _on_apply_brightness_clicked(self, _btn: Gtk.Button) -> None:
        self.bri_spin.update()
        self._execute_action(
            "brightness.sh", [str(self.bri_spin.get_value_as_int())], refresh_after=True
        )

    def _on_apply_timeout_clicked(self, _btn: Gtk.Button) -> None:
        self.timeout_spin.update()
        self._execute_action(
            "rgb.sh", ["--timeout", str(self.timeout_spin.get_value_as_int())], refresh_after=True
        )

    def _on_brightness_preset_clicked(self, _btn: Gtk.Button, cmd: str, val: int) -> None:
        with self._ui_update_guard():
            self.bri_spin.set_value(val)
            self.bri_scale.set_value(val)
        self._execute_action("brightness.sh", [cmd], refresh_after=True)

    def _on_apply_rgb_clicked(self, _btn: Gtk.Button) -> None:
        self.r_spin.update()
        self.g_spin.update()
        self.b_spin.update()
        r = self.r_spin.get_value_as_int()
        g = self.g_spin.get_value_as_int()
        b = self.b_spin.get_value_as_int()
        args: list[str] = []
        if self.cc_scale_check.get_active():
            args.append("--cc-scale")
        args.extend([str(r), str(g), str(b)])
        self._execute_action("rgb.sh", args, refresh_after=True)

    def _on_rgb_preset_clicked(self, _btn: Gtk.Button, preset: str, pr: int, pg: int, pb: int) -> None:
        with self._ui_update_guard():
            self.r_spin.set_value(pr)
            self.g_spin.set_value(pg)
            self.b_spin.set_value(pb)
            self.color_btn.set_rgba(Gdk.RGBA(pr / 255.0, pg / 255.0, pb / 255.0, 1.0))
        args: list[str] = []
        if self.cc_scale_check.get_active():
            args.append("--cc-scale")
        args.append(preset)
        self._execute_action("rgb.sh", args, refresh_after=True)

    def _on_apply_battery_custom_clicked(self, _btn: Gtk.Button) -> None:
        self.bat_start_spin.update()
        self.bat_stop_spin.update()
        if not self._validate_battery_spins():
            self._append_log("Blocked invalid battery limit: start must be strictly less than stop.")
            return
        start = self.bat_start_spin.get_value_as_int()
        stop = self.bat_stop_spin.get_value_as_int()
        self._execute_action("battery.sh", ["limit", str(start), str(stop)], refresh_after=True)

    # ---- Async Execution & UI Refresh ----

    def _execute_action(self, script: str, args: list[str], refresh_after: bool = False) -> None:
        def worker() -> None:
            res = self.runner.run_script(script, args)
            if res.rc == 0 and (not self.runner.dry_run) and res.mode in ("root", "worker", "daemon"):
                try:
                    self.runner.record_action(script, args)
                except Exception as exc:  # noqa: BLE001
                    res.note = (f"{res.note} " if res.note else "") + f"[Config save warning: {exc}]"

            def on_done() -> bool:
                if self._destroyed:
                    return False
                self._sync_unlock_button()
                if res.mode == "error" or res.rc != 0:
                    err_lines = [ln.strip() for ln in res.stderr.splitlines() if ln.strip()]
                    err_hint = res.note or (err_lines[0] if err_lines else f"{res.script} failed (rc={res.rc}).")
                    self._banner_alert = ("warn", err_hint)
                elif res.mode == "snapshot-dry-run" and res.note:
                    self._banner_alert = ("dry-run", res.note)
                else:
                    self._banner_alert = None
                self._update_mode_banner()
                self._log_script_result(res)
                if refresh_after and res.mode in ("root", "worker", "daemon"):
                    self.refresh_all(quiet_log=True, from_poll=False)
                return False

            GLib.idle_add(on_done)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_all(self, quiet_log: bool = False, from_poll: bool = False) -> bool:
        with self._refresh_lock:
            if self._destroyed:
                return False
            if self._refresh_in_flight:
                self._refresh_pending = True
                self._refresh_pending_quiet = self._refresh_pending_quiet and quiet_log
                self._refresh_pending_poll = self._refresh_pending_poll and from_poll
                return False
            self._refresh_in_flight = True
            self._refresh_pending = False
            self._refresh_pending_quiet = True
            self._refresh_pending_poll = True

        def worker() -> None:
            results: dict[str, ScriptResult] = {}
            scripts = ["power_profile.sh", "fan.sh", "brightness.sh", "rgb.sh", "battery.sh"]
            if not from_poll:
                scripts.append("peripherals.sh")
            try:
                for script in scripts:
                    if self._destroyed:
                        break
                    results[script] = self.runner.run_script(script, ["status"])
            finally:
                def on_done() -> bool:
                    with self._refresh_lock:
                        self._refresh_in_flight = False
                        pending = self._refresh_pending and not self._destroyed
                        next_quiet = self._refresh_pending_quiet
                        next_poll = self._refresh_pending_poll
                        self._refresh_pending = False
                    if self._destroyed:
                        return False
                    self._sync_unlock_button()
                    if not from_poll:
                        self._banner_alert = None
                    self._update_mode_banner()
                    self._apply_all_status_results(
                        results, quiet_log=quiet_log, sync_inputs=not from_poll
                    )
                    if pending:
                        self.refresh_all(quiet_log=next_quiet, from_poll=next_poll)
                    return False

                GLib.idle_add(on_done)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def _apply_all_status_results(
        self,
        results: dict[str, ScriptResult],
        quiet_log: bool = False,
        sync_inputs: bool = True,
    ) -> None:
        for _script, res in results.items():
            if not quiet_log or res.rc != 0:
                self._log_script_result(res)
        if "power_profile.sh" in results:
            self._apply_power_status(parse_power_status(results["power_profile.sh"].stdout))
        if "fan.sh" in results:
            self._apply_fan_status(parse_fan_status(results["fan.sh"].stdout), sync_inputs=sync_inputs)
        if "brightness.sh" in results:
            self._apply_brightness_status(
                parse_brightness_status(results["brightness.sh"].stdout), sync_inputs=sync_inputs
            )
        if "rgb.sh" in results:
            self._apply_rgb_status(parse_rgb_status(results["rgb.sh"].stdout), sync_inputs=sync_inputs)
        if "battery.sh" in results:
            self._apply_battery_status(
                parse_battery_status(results["battery.sh"].stdout), sync_inputs=sync_inputs
            )
        if "peripherals.sh" in results:
            self._apply_peripherals_status(
                parse_peripherals_status(results["peripherals.sh"].stdout)
            )

    def _apply_power_status(self, parsed: dict[str, Any]) -> None:
        with self._ui_update_guard():
            self._active_power_profile = parsed.get("profile_key")
            if parsed["profile_key"]:
                self._selected_power_profile = parsed["profile_key"]
            self._set_active_choice(self.power_profile_btns, parsed["profile_key"])
            self.card_ec_profile.set_text(parsed["profile_text"])
            self.card_ec_peci.set_text(parsed["ec_peci_text"])
            self.card_rapl_msr.set_text(parsed["rapl_msr_text"])
            self.card_rapl_mmio.set_text(parsed["rapl_mmio_text"])

            oem_text = parsed.get("oem_table_text", "N/A")
            oem_profiles = parsed.get("oem_table_profiles") or {}
            ec_pl1 = parsed.get("ec_pl1_w")
            ec_pl2 = parsed.get("ec_pl2_w")
            if oem_text == "N/A":
                self.card_oem_table.set_text("N/A")
                self.sub_oem_table.set_text("Direct EC + RAPL OEM table unavailable (dry-run or locked)")
            elif oem_profiles:
                ordered = [
                    f"{lbl}={oem_profiles[k][0]:g}/{oem_profiles[k][1]:g}W"
                    for k, lbl in (
                        ("quiet", "Quiet"),
                        ("powersaving", "PowerSaving"),
                        ("entertainment", "Entertainment"),
                        ("performance", "Performance"),
                    )
                    if k in oem_profiles
                ]
                self.card_oem_table.set_text(" • ".join(ordered) if ordered else str(oem_text))
                if ec_pl1 is not None and ec_pl2 is not None:
                    self.sub_oem_table.set_text(
                        f"EC PL1T/PL2T (0xF0..0xF3) = {ec_pl1:.3f} W / {ec_pl2:.3f} W • Direct EC + RAPL"
                    )
                else:
                    self.sub_oem_table.set_text("Direct EC + RAPL")
            else:
                self.card_oem_table.set_text(str(oem_text))
                if ec_pl1 is not None and ec_pl2 is not None:
                    self.sub_oem_table.set_text(
                        f"EC PL1T/PL2T (0xF0..0xF3) = {ec_pl1:.3f} W / {ec_pl2:.3f} W • Direct EC + RAPL"
                    )
                else:
                    self.sub_oem_table.set_text("Direct EC + RAPL")

            pm_state = parsed.get("gpu_pm_state")
            if parsed["gpu_power_text"] == "N/A":
                self.card_gpu_power.set_text("N/A")
                self.sub_gpu_power.set_text("nvidia-smi / PCI Runtime PM telemetry unavailable")
            elif pm_state == "d3cold":
                self.card_gpu_power.set_text("Disabled / Suspended (D3cold / Power Saving mode)")
                self.sub_gpu_power.set_text(
                    "PCI power/control=auto • runtime=suspended (dGPU powered off)"
                )
            elif pm_state == "d3cold_pending":
                self.card_gpu_power.set_text(
                    "RTD3 Auto / D3cold Pending (Power Saving mode; display active)"
                )
                self.sub_gpu_power.set_text(
                    "PCI power/control=auto • runtime=active (suspends when display consumers release)"
                )
            else:
                if (
                    parsed["gpu_power_default_text"] != "N/A"
                    and parsed["gpu_power_max_text"] != "N/A"
                ):
                    self.card_gpu_power.set_text(
                        f"{parsed['gpu_power_cur_text']}  "
                        f"(default {parsed['gpu_power_default_text']} / max {parsed['gpu_power_max_text']})"
                    )
                else:
                    self.card_gpu_power.set_text(parsed["gpu_power_text"])

                sub_parts: list[str] = []
                if parsed.get("gpu_tier_label"):
                    sub_parts.append(str(parsed["gpu_tier_label"]))
                if parsed.get("nvidia_powerd_state"):
                    sub_parts.append(f"nvidia-powerd={parsed['nvidia_powerd_state']}")
                if parsed.get("gpu_pci_control"):
                    sub_parts.append(f"power/control={parsed['gpu_pci_control']}")
                if parsed.get("gpu_runtime_status"):
                    sub_parts.append(f"runtime={parsed['gpu_runtime_status']}")
                self.sub_gpu_power.set_text(
                    " • ".join(sub_parts) if sub_parts else "nvidia-smi / PCI Runtime PM active"
                )

            self._sync_fan_lock_state()

    def _apply_fan_status(
        self, parsed: Union[FanStatus, Mapping[str, Any]], sync_inputs: bool = True
    ) -> None:
        with self._ui_update_guard():
            temp_map = {
                "cpu": parsed["cpu_temp"],
                "ac1": parsed["ac1_temp"],
                "vr": parsed["cpu_vr_temp"],
                "dgpu": parsed["dgpu_temp"],
            }
            for key, temp_c in temp_map.items():
                self.temp_vals[key].set_text(f"{temp_c} °C" if temp_c is not None else "—")
                self._update_temp_bar(self.temp_bars[key], temp_c)

            if parsed["cpu_fan_rpm"] is not None:
                self.lbl_cpu_fan.set_text(f"{parsed['cpu_fan_rpm']} RPM")
                self.sub_cpu_fan.set_text(
                    f"Duty {parsed['cpu_fan_duty']}/255 ({parsed['cpu_fan_pct']}%) • raw={parsed['cpu_fan_raw']}"
                )
                self.bar_cpu_fan.set_fraction(max(0.0, min(1.0, parsed["cpu_fan_pct"] / 100.0)))
                self.bar_cpu_fan.set_text(f"{parsed['cpu_fan_pct']}%")
            else:
                self.lbl_cpu_fan.set_text("—")
                self.sub_cpu_fan.set_text("Duty / Tach")
                self.bar_cpu_fan.set_fraction(0.0)
                self.bar_cpu_fan.set_text("—")

            if parsed["gpu_fan_rpm"] is not None:
                self.lbl_gpu_fan.set_text(f"{parsed['gpu_fan_rpm']} RPM")
                self.sub_gpu_fan.set_text(
                    f"Duty {parsed['gpu_fan_duty']}/255 ({parsed['gpu_fan_pct']}%) • raw={parsed['gpu_fan_raw']}"
                )
                self.bar_gpu_fan.set_fraction(max(0.0, min(1.0, parsed["gpu_fan_pct"] / 100.0)))
                self.bar_gpu_fan.set_text(f"{parsed['gpu_fan_pct']}%")
            else:
                self.lbl_gpu_fan.set_text("—")
                self.sub_gpu_fan.set_text("Duty / Tach")
                self.bar_gpu_fan.set_fraction(0.0)
                self.bar_gpu_fan.set_text("—")

            self.lbl_nvidia_gpu.set_text(parsed["nvidia_text"])
            self.lbl_fan_mode.set_text(parsed["fan_mode_text"])
            if parsed["fan_mode_key"] is not None:
                self._fan_mode_key = parsed["fan_mode_key"]
                self._fan_mode_text = parsed["fan_mode_text"]
            elif sync_inputs or self._fan_mode_key is None:
                self._fan_mode_key = None
                self._fan_mode_text = parsed["fan_mode_text"]
            self._set_active_choice(
                self.fan_mode_btns,
                None if self._is_fan_locked() else self._fan_mode_key,
            )

            self._live_offset_pct = parsed["offset_pct"]
            if parsed["offset_pct"] is not None:
                self.sub_fan_offset.set_text(f"Offset (0xE7): {parsed['offset_raw']}/255 ({parsed['offset_pct']}%)")
                if self._should_sync_input("fan_offset_pct", parsed["offset_pct"], sync_inputs):
                    self.fan_offset_spin.set_value(parsed["offset_pct"])
                    self.fan_offset_scale.set_value(parsed["offset_pct"])
                    self._preview_offset_pct = parsed["offset_pct"]
            else:
                self.sub_fan_offset.set_text("Offset (0xE7): —")

            if sync_inputs:
                self._curve_dirty = False
            raw_cpu_curve = _normalize_4pt_curve(parsed.get("cpu_curve") or CUSTOM_4PT_WAYPOINTS)
            raw_gpu_curve = _normalize_4pt_curve(parsed.get("gpu_curve") or CUSTOM_4PT_WAYPOINTS)
            if not self._curve_dirty and self._should_sync_input(
                "fan_curves", (tuple(raw_cpu_curve), tuple(raw_gpu_curve)), sync_inputs
            ):
                self._cpu_curve = raw_cpu_curve
                self._gpu_curve = raw_gpu_curve
                self._sync_curve_spins("cpu", self._cpu_curve)
                self._sync_curve_spins("gpu", self._gpu_curve)

            cpu_t = parsed["cpu_temp"]
            cpu_d = parsed["cpu_fan_pct"]
            cpu_r = parsed["cpu_fan_rpm"]
            gpu_t = parsed["dgpu_temp"]
            gpu_d = parsed["gpu_fan_pct"]
            gpu_r = parsed["gpu_fan_rpm"]
            self._latest_cpu_telemetry = (cpu_t, cpu_d, cpu_r)
            self._latest_gpu_telemetry = (gpu_t, gpu_d, gpu_r)
            if cpu_t is not None and cpu_d is not None:
                self._cpu_telemetry_history.append((float(cpu_t), float(cpu_d)))
            if gpu_t is not None and gpu_d is not None:
                self._gpu_telemetry_history.append((float(gpu_t), float(gpu_d)))
            self._update_fan_graph()

    def _apply_brightness_status(self, parsed: dict[str, Any], sync_inputs: bool = True) -> None:
        with self._ui_update_guard():
            self.lbl_bri_state.set_text(parsed["cached_text"])
            if parsed.get("mode_text"):
                self.sub_bri_mode.set_text(f"Mode (KBType=6): {parsed['mode_text']}")
            else:
                self.sub_bri_mode.set_text("Mode (KBType=6): 0xCA sub 0x06 (write-only; cached)")
            if parsed["level"] is not None and self._should_sync_input(
                "bri_level", parsed["level"], sync_inputs
            ):
                self.bri_spin.set_value(parsed["level"])
                self.bri_scale.set_value(parsed["level"])

    def _apply_rgb_status(self, parsed: dict[str, Any], sync_inputs: bool = True) -> None:
        with self._ui_update_guard():
            self.lbl_rgb_state.set_text(parsed["cached_text"])
            target_mode = parsed.get("target_mode") or "KBType=6 1-zone (0xF9=0x03)"
            mailbox_info = (
                f"Mailbox 0xF8..0xFD: {parsed['mailbox_text']}"
                if parsed["mailbox_text"]
                else "Mailbox 0xF8..0xFD: (dry-run / unavailable)"
            )
            self.sub_rgb_mailbox.set_text(f"{target_mode} • {mailbox_info}")
            if parsed["rgb"] is not None and self._should_sync_input("rgb_color", parsed["rgb"], sync_inputs):
                r, g, b = parsed["rgb"]
                self.r_spin.set_value(r)
                self.g_spin.set_value(g)
                self.b_spin.set_value(b)
                self.color_btn.set_rgba(Gdk.RGBA(r / 255.0, g / 255.0, b / 255.0, 1.0))
            if parsed["cc_scale"] is not None and self._should_sync_input(
                "rgb_cc_scale", bool(parsed["cc_scale"]), sync_inputs
            ):
                self.cc_scale_check.set_active(bool(parsed["cc_scale"]))
            if parsed["effect"]:
                self._set_active_choice(self.rgb_effect_btns, parsed["effect"])
            elif parsed["rgb"] is not None:
                self._set_active_choice(self.rgb_effect_btns, None)
            if parsed["timeout"] and parsed["timeout"].isdigit():
                tval = int(parsed["timeout"])
                if 5 <= tval <= 3600 and self._should_sync_input("rgb_timeout", tval, sync_inputs):
                    self.timeout_spin.set_value(tval)

    def _apply_battery_status(self, parsed: dict[str, Any], sync_inputs: bool = True) -> None:
        with self._ui_update_guard():
            self.lbl_bat_sysfs.set_text(parsed["sysfs_text"])
            self.lbl_bat_ec.set_text(parsed["ec_power_text"])

            f1_enabled = parsed.get("f1_enabled")
            start = parsed.get("f1_start")
            stop = parsed.get("f1_stop")
            raw_info = parsed.get("f1_raw")

            active_presets: set[str] = set()
            f1_text = str(parsed.get("f1_text") or "N/A")
            sub_detail = (
                f1_text
                if "Direct EC 0xCB" in f1_text
                else f"Direct EC 0xCB / XRAM 0x06D0..0x06D2: {f1_text}"
            )
            if raw_info and raw_info not in sub_detail:
                sub_detail += f" [{raw_info}]"

            if f1_enabled is True:
                if start is not None and stop is not None:
                    self.lbl_bat_flexi.set_text(f"Enabled ({start}%–{stop}%)")
                    if (start, stop) == (60, 80):
                        active_presets.add("60-80")
                    elif (start, stop) == (70, 85):
                        active_presets.add("70-85")
                else:
                    self.lbl_bat_flexi.set_text("Enabled")
                self.sub_bat_flexi.set_text(sub_detail)
            elif f1_enabled is False:
                self.lbl_bat_flexi.set_text("Disabled (100% Full Charge)")
                active_presets.update(("full", "disable"))
                self.sub_bat_flexi.set_text(sub_detail)
            else:
                if f1_text.lower().startswith("ready"):
                    self.lbl_bat_flexi.set_text("Ready (Select 80% Limit or 100% Full)")
                    self.sub_bat_flexi.set_text(f1_text)
                elif f1_text.lower().startswith("unknown"):
                    self.lbl_bat_flexi.set_text(f1_text)
                    self.sub_bat_flexi.set_text(
                        "Direct EC 0xCB / XRAM 0x06D0..0x06D2: unrecognized FlexiCharger threshold readback"
                    )
                else:
                    self.lbl_bat_flexi.set_text("Unavailable (Unlock Live EC)")
                    self.sub_bat_flexi.set_text(
                        sub_detail
                        if f1_text not in ("N/A", "")
                        else "Direct EC 0xCB / XRAM 0x06D0..0x06D2: unlock live EC (ec_sys)"
                    )

            for pkey, btn in self.battery_preset_btns.items():
                btn.set_sensitive(True)
                ctx = btn.get_style_context()
                if pkey in active_presets:
                    ctx.add_class("active-choice")
                else:
                    ctx.remove_class("active-choice")

            if (
                f1_enabled is True
                and start is not None
                and stop is not None
                and 20 <= start <= 95
                and 30 <= stop <= 100
                and start < stop
                and self._should_sync_input("bat_limits", (start, stop), sync_inputs)
            ):
                self.bat_start_spin.set_value(start)
                self.bat_stop_spin.set_value(stop)
                self._validate_battery_spins()

    @staticmethod
    def _on_off_key(state_str: str) -> Optional[str]:
        up = state_str.strip().upper()
        if up == "ON":
            return "on"
        if up == "OFF":
            return "off"
        return None

    def _apply_peripherals_status(self, parsed: PeripheralsStatus) -> None:
        with self._ui_update_guard():
            self.lbl_webcam_state.set_text(parsed.webcam)
            self.sub_webcam_state.set_text(parsed.webcam_detail)
            self._set_active_choice(self.webcam_btns, self._on_off_key(parsed.webcam))

            self.lbl_trackpad_state.set_text(parsed.trackpad)
            self.sub_trackpad_state.set_text(parsed.trackpad_detail)
            self._set_active_choice(self.trackpad_btns, self._on_off_key(parsed.trackpad))

            self.lbl_lid_state.set_text(parsed.lid_state)
            self.sub_lid_state.set_text(parsed.lid_state_detail)
            lid_ctx = self.lbl_lid_state.get_style_context()
            lid_ctx.remove_class("lid-badge-open")
            lid_ctx.remove_class("lid-badge-closed")
            if parsed.lid_state.lower() == "open":
                lid_ctx.add_class("lid-badge-open")
            elif parsed.lid_state.lower() == "closed":
                lid_ctx.add_class("lid-badge-closed")

            self.lbl_lid_wake_state.set_text(parsed.wake_on_lid)
            self.sub_lid_wake_state.set_text(parsed.wake_on_lid_detail)
            self._set_active_choice(self.lid_wake_btns, self._on_off_key(parsed.wake_on_lid))

            self.lbl_lid_close_state.set_text(parsed.lid_close_mode)
            self.sub_lid_close_state.set_text(parsed.lid_close_detail)
            self._set_active_choice(self.lid_close_btns, parsed.lid_close_key)

            self.lbl_win_lock_state.set_text(parsed.win_lock)
            self.sub_win_lock_state.set_text(parsed.win_lock_detail)
            self._set_active_choice(self.win_lock_btns, self._on_off_key(parsed.win_lock))

            self.lbl_fn_swap_state.set_text(parsed.fn_swap)
            self.sub_fn_swap_state.set_text(parsed.fn_swap_detail)
            self._set_active_choice(self.fn_swap_btns, self._on_off_key(parsed.fn_swap))

            self.lbl_fn_lock_state.set_text(parsed.fn_lock)
            self.sub_fn_lock_state.set_text(parsed.fn_lock_detail)
            self._set_active_choice(self.fn_lock_btns, self._on_off_key(parsed.fn_lock))

    def _on_delete_event(self, *_args: Any) -> bool:
        self._destroyed = True
        if self._poll_source_id is not None:
            GLib.source_remove(self._poll_source_id)
            self._poll_source_id = None
        self.runner.stop_worker()
        Gtk.main_quit()
        return False


# ---------------------------------------------------------------------------
# Comprehensive Self-Test Mode (--self-test)
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


def run_self_test() -> int:
    """Headless-safe verification of parsers, dry-run dispatch, worker IPC, and GTK window."""
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        status = "PASS" if cond else "FAIL"
        suffix = f" ({detail})" if detail else ""
        print(f"[{status}] {name}{suffix}")
        if not cond:
            failures.append(f"{name}: {detail}")

    with tempfile.TemporaryDirectory(prefix="acer_cc_selftest_") as tmp_cache, _temp_env(
        "ACER_CONTROL_SOCK", ""
    ), _temp_env("ACER_CONTROL_CONFIG", os.path.join(tmp_cache, "isolated_config.json")):
        # 1. Test snapshot status & parsers on ec0_io.bin (or synthetic 256B fixture in standalone repo)
        ec_fixture = DEFAULT_EC_SNAPSHOT
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
            ec_fixture = os.path.join(tmp_cache, "ec0_io.bin")
            with open(ec_fixture, "wb") as fh:
                fh.write(ec_buf)
        snap_runner = ScriptRunner(dry_run=False, explicit_ec_file=ec_fixture, cache_dir=tmp_cache)
        for script in ("power_profile.sh", "fan.sh", "battery.sh", "brightness.sh", "rgb.sh", "peripherals.sh"):
            res = snap_runner.run_script(script, ["status"])
            check(
                f"Snapshot/fallback status {script}",
                res.rc == 0,
                f"mode={res.mode}, rc={res.rc}, err={res.stderr.strip()!r}",
            )
            if script in EC_FILE_SCRIPTS and os.path.isfile(ec_fixture):
                check(f"{script} did use --ec-file", "--ec-file" in res.effective_cmd, str(res.effective_cmd))
            else:
                check(f"{script} NEVER passed --ec-file", "--ec-file" not in res.effective_cmd, str(res.effective_cmd))

            if script == "power_profile.sh":
                p = parse_power_status(res.stdout)
                check("parse_power_status ec_peci", p["ec_pl1_w"] is not None or p["dry_run"], p["ec_peci_text"])
                if not p["dry_run"]:
                    check(
                        "parse_power_status snapshot EC hardware PECI & OS RAPL ceiling",
                        "(EC hardware PECI)" in res.stdout
                        and "EC hardware PECI" in p["ec_peci_text"]
                        and (
                            "(OS ceiling" not in res.stdout
                            or (
                                p["rapl_msr_pl1"] is not None
                                and p["rapl_msr_pl2"] is not None
                                and "(OS ceiling" in p["rapl_msr_text"]
                            )
                        )
                        and ("nominal TDP=" not in res.stdout or "nominal TDP=" in p["rapl_msr_text"]),
                        f"peci={p['ec_peci_text']!r}, msr={p['rapl_msr_text']!r}",
                    )
                    check(
                        "parse_power_status snapshot OEM Table (Direct EC + RAPL)",
                        "OEM Table" not in res.stdout
                        or (
                            p["oem_table_profiles"].get("quiet") == (15.0, 30.0)
                            and p["oem_table_profiles"].get("powersaving") == (15.0, 30.0)
                            and p["oem_table_profiles"].get("entertainment") == (45.0, 115.0)
                            and p["oem_table_profiles"].get("performance") == (90.0, 115.0)
                        ),
                        p["oem_table_text"],
                    )
                    check(
                        "parse_power_status snapshot GPU Power",
                        p["gpu_power_text"] != "N/A"
                        and (
                            p["gpu_pm_state"] in ("d3cold", "d3cold_pending")
                            or (
                                p["nvidia_powerd_state"] is not None
                                and (
                                    "[N/A]" not in res.stdout
                                    or (p["gpu_power_cur_w"] is None and p["gpu_power_cur_text"] == "[N/A]")
                                )
                                and (
                                    "default 60.00 W" not in res.stdout
                                    or (p["gpu_power_default_w"] == 60.0 and p["gpu_power_max_w"] == 75.0)
                                )
                            )
                        ),
                        p["gpu_power_text"],
                    )
            elif script == "fan.sh":
                f = parse_fan_status(res.stdout)
                check(
                    "parse_fan_status temps/fans & dual curves",
                    isinstance(f, FanStatus)
                    and f["cpu_temp"] is not None
                    and f["cpu_fan_rpm"] is not None
                    and len(f.cpu_curve) == 4
                    and len(f.gpu_curve) == 4,
                    str(f),
                )
            elif script == "battery.sh":
                b = parse_battery_status(res.stdout)
                check(
                    "parse_battery_status ec_power & Ready FlexiCharger in snapshot mode",
                    b["ec_remaining"] is not None
                    and (
                        "ready" not in res.stdout.lower()
                        or (
                            b["f1_enabled"] is None
                            and "ready" in b["f1_text"].lower()
                            and "XRAM 0x06D0..0x06D2" in b["f1_text"]
                        )
                    ),
                    f"{b['ec_power_text']} | {b['f1_text']}",
                )
            elif script == "brightness.sh":
                br = parse_brightness_status(res.stdout)
                check(
                    "parse_brightness_status snapshot/dry-run",
                    br["cached_text"] == "(none recorded yet)"
                    and br["mode_text"] is not None
                    and "0xCA sub 0x06" in br["mode_text"],
                    str(br),
                )
            elif script == "rgb.sh":
                rg = parse_rgb_status(res.stdout)
                check(
                    "parse_rgb_status snapshot/dry-run",
                    rg["target_mode"] == "KBType=6 1-zone (0xF9=0x03)"
                    and rg["cached_text"] == "(none recorded yet)"
                    and "zone" not in rg,
                    str(rg),
                )
            elif script == "peripherals.sh":
                ps = parse_peripherals_status(res.stdout)
                check(
                    "parse_peripherals_status snapshot (ec0_io.bin)",
                    isinstance(ps, PeripheralsStatus)
                    and ps.webcam in ("ON", "OFF")
                    and ps.trackpad in ("ON", "OFF")
                    and ps.lid_state in ("Open", "Closed")
                    and ps.wake_on_lid in ("ON", "OFF")
                    and ps.lid_close_key in ("suspend", "ignore")
                    and ps.win_lock in ("ON", "OFF")
                    and ps.fn_swap in ("ON", "OFF")
                    and ps.fn_lock in ("ON", "OFF"),
                    str(ps),
                )

        # Verify locked setter action falls back transparently to --dry-run (without modifying hardware/cache)
        if os.geteuid() != 0:
            locked_set = snap_runner.run_script("power_profile.sh", ["quiet"])
            check(
                "Locked setter falls back transparently",
                locked_set.mode == "snapshot-dry-run"
                and locked_set.rc == 0
                and "[dry-run]" in locked_set.stdout
                and "[DRY-RUN FALLBACK" in locked_set.note,
                f"mode={locked_set.mode}, note={locked_set.note!r}",
            )
            locked_periph = snap_runner.run_script("peripherals.sh", ["win-lock", "on"])
            check(
                "Locked peripherals setter falls back transparently",
                locked_periph.mode == "snapshot-dry-run"
                and locked_periph.rc == 0
                and "[dry-run]" in locked_periph.stdout
                and "[DRY-RUN FALLBACK" in locked_periph.note,
                f"mode={locked_periph.mode}, note={locked_periph.note!r}",
            )

        # Synthetic live & optional-line parser checks (Direct EC + RAPL + PCI Runtime PM)
        na_gpu_power = parse_power_status(
            "Power Profile : (snapshot mode; mailbox read 0xD8 skipped on static file ec0_io.bin)\n"
            "EC PL1T/PL2T  : 35.000 W (en=1) / 115.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
            "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
            "RAPL MSR      : 90.000 W / 115.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "RAPL MMIO     : 90.000 W / 115.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "GPU Power     : [N/A] / default 60.00 W / max 75.00 W (nvidia-powerd=active, power/control=on, runtime=active)\n"
        )
        check(
            "parse_power_status GPU [N/A] + Direct EC OEM Table / PECI / RAPL strings",
            na_gpu_power["ec_pl1_w"] == 35.0
            and na_gpu_power["ec_pl2_w"] == 115.0
            and na_gpu_power["oem_table_profiles"] == {
                "quiet": (15.0, 30.0),
                "powersaving": (15.0, 30.0),
                "entertainment": (45.0, 115.0),
                "performance": (90.0, 115.0),
            }
            and "EC hardware PECI" in na_gpu_power["ec_peci_text"]
            and na_gpu_power["rapl_msr_pl1"] == 90.0
            and na_gpu_power["rapl_msr_pl2"] == 115.0
            and "OS ceiling; nominal TDP=45.000 W" in na_gpu_power["rapl_msr_text"]
            and na_gpu_power["gpu_pm_state"] == "active"
            and na_gpu_power["gpu_power_cur_w"] is None
            and na_gpu_power["gpu_power_cur_text"] == "[N/A]"
            and na_gpu_power["gpu_power_default_w"] == 60.0
            and na_gpu_power["gpu_power_max_w"] == 75.0
            and na_gpu_power["nvidia_powerd_state"] == "active"
            and na_gpu_power["gpu_pci_control"] == "on"
            and na_gpu_power["gpu_runtime_status"] == "active",
            str(na_gpu_power),
        )

        alt_headers_power = parse_power_status(
            "Power Profile : performance (0x01)\n"
            "EC PECI Limits: 90.000 W (en=1) / 115.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
            "OS RAPL MSR   : 90.000 W / 115.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "OS RAPL MMIO  : 90.000 W / 115.000 W (OS ceiling)\n"
        )
        check(
            "parse_power_status EC PECI Limits & OS RAPL MSR/MMIO variants",
            alt_headers_power["profile_key"] == "performance"
            and alt_headers_power["ec_pl1_w"] == 90.0
            and alt_headers_power["ec_pl2_w"] == 115.0
            and "EC hardware PECI" in alt_headers_power["ec_peci_text"]
            and alt_headers_power["rapl_msr_pl1"] == 90.0
            and alt_headers_power["rapl_msr_pl2"] == 115.0
            and alt_headers_power["rapl_mmio_pl1"] == 90.0
            and alt_headers_power["rapl_mmio_pl2"] == 115.0,
            str(alt_headers_power),
        )

        live_power = parse_power_status(
            "Power Profile : performance (0x01)\n"
            "EC PL1T/PL2T  : 90.000 W (en=1) / 115.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
            "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
            "RAPL MSR      : 90.000 W / 115.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "RAPL MMIO     : 90.000 W / 115.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "GPU Power     : 75.00 W / default 60.00 W / max 75.00 W "
            "(nvidia-powerd=active, power/control=on, runtime=active) [Performance 75W, cur=75.00 W]\n"
        )
        check(
            "parse_power_status live 75W + Direct EC OEM Table + PCI PM",
            live_power["profile_key"] == "performance"
            and live_power["oem_table_profiles"].get("performance") == (90.0, 115.0)
            and live_power["rapl_msr_pl1"] == 90.0
            and live_power["rapl_msr_pl2"] == 115.0
            and live_power["gpu_pm_state"] == "active"
            and live_power["gpu_power_cur_w"] == 75.0
            and live_power["gpu_power_default_w"] == 60.0
            and live_power["gpu_power_max_w"] == 75.0
            and live_power["nvidia_powerd_state"] == "active"
            and live_power["gpu_pci_control"] == "on"
            and live_power["gpu_runtime_status"] == "active"
            and live_power["gpu_tier_label"] == "Performance 75W",
            str(live_power),
        )

        unknown_04_power = parse_power_status("Power Profile : unknown (0x04)\n")
        check(
            "parse_power_status leaves unexpected 0x04 as unknown (no turbo mapping)",
            unknown_04_power["profile_key"] is None and unknown_04_power["profile_text"] == "unknown (0x04)",
            str(unknown_04_power),
        )

        quiet_25w_power = parse_power_status(
            "Power Profile : quiet (0x02)\n"
            "EC PL1T/PL2T  : 15.000 W (en=1) / 30.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
            "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
            "RAPL MSR      : 15.000 W / 30.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "RAPL MMIO     : 15.000 W / 30.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "GPU Power     : 25.00 W / default 60.00 W / max 75.00 W "
            "(nvidia-powerd=active, power/control=on, runtime=active) [Quiet 25W, cur=25.00 W]\n"
        )
        bal_60w_power = parse_power_status(
            "Power Profile : entertainment / balanced (0x00)\n"
            "EC PL1T/PL2T  : 45.000 W (en=1) / 115.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
            "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
            "RAPL MSR      : 45.000 W / 115.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "RAPL MMIO     : 45.000 W / 115.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "GPU Power     : 60.00 W / default 60.00 W / max 75.00 W "
            "(nvidia-powerd=active, power/control=on, runtime=active) [Entertainment 60W, cur=60.00 W]\n"
        )
        powersaving_d3cold_power = parse_power_status(
            "Power Profile : powersaving (0x03)\n"
            "EC PL1T/PL2T  : 15.000 W (en=1) / 30.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
            "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
            "RAPL MSR      : 15.000 W / 30.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "RAPL MMIO     : 15.000 W / 30.000 W (OS ceiling; nominal TDP=45.000 W)\n"
            "GPU Power     : Disabled / Suspended (D3cold / Power Saving mode)\n"
        )
        powersaving_pending_power = parse_power_status(
            "Power Profile : powersaving (0x03)\n"
            "EC PL1T/PL2T  : 15.000 W (en=1) / 30.000 W (en=1) (TAUT=0xDF) (EC hardware PECI)\n"
            "OEM Table     : Quiet=15/30W PowerSaving=15/30W Entertainment=45/115W Performance=90/115W (Direct EC + RAPL)\n"
            "GPU Power     : RTD3 Auto / D3cold Pending (Power Saving mode; display active)\n"
        )
        check(
            "parse_power_status Quiet 25W, Entertainment 60W, and Power Saving D3cold / Pending",
            quiet_25w_power["profile_key"] == "quiet"
            and quiet_25w_power["gpu_power_cur_w"] == 25.0
            and quiet_25w_power["gpu_tier_label"] == "Quiet 25W"
            and bal_60w_power["profile_key"] == "entertainment"
            and bal_60w_power["gpu_power_cur_w"] == 60.0
            and bal_60w_power["gpu_tier_label"] == "Entertainment 60W"
            and powersaving_d3cold_power["profile_key"] == "powersaving"
            and powersaving_d3cold_power["gpu_pm_state"] == "d3cold"
            and powersaving_d3cold_power["gpu_runtime_status"] == "suspended"
            and powersaving_pending_power["profile_key"] == "powersaving"
            and powersaving_pending_power["gpu_pm_state"] == "d3cold_pending"
            and powersaving_pending_power["gpu_pci_control"] == "auto",
            f"q={quiet_25w_power['gpu_tier_label']!r}, ps={powersaving_d3cold_power['gpu_pm_state']!r}, pend={powersaving_pending_power['gpu_pm_state']!r}",
        )

        live_fan = parse_fan_status(
            "=== EC Thermal & Fan Telemetry (/sys/kernel/debug/ec/ec0/io) ===\n"
            "  CPU Temp  (0x07) : 61 C  (Sensor 0x08: 48 C)\n"
            "  CPU VR    (0x0A) : 52 C  (dGPU 0x0B  : 49 C)\n"
            "  CPU Fan   (0xCE) : 3100 RPM  (duty=128/255,  50%, raw_tach=696)\n"
            "  GPU Fan   (0xCF) : 2950 RPM  (duty=120/255,  47%, raw_tach=731)\n"
            "  Fan Offset(0xE7) :  51/255 (20%)\n"
            "  Fan Mode  (0xD7) : Auto (0x08, Performance Profile)\n"
            "  CPU Curve (0x038C): (40C, 50%) -> (55C, 54%) -> (75C, 76%) -> (100C, 100%)\n"
            "  GPU Curve (0x039A): (40C, 50%) -> (58C, 58%) -> (78C, 80%) -> (100C, 100%)\n"
            "=== NVIDIA GPU ===\n"
            "  NVIDIA GeForce RTX 3050 6GB Laptop GPU, 44, 7.10 W\n"
        )
        fan_d3cold_susp = parse_fan_status(
            "=== EC Thermal & Fan Telemetry (/sys/kernel/debug/ec/ec0/io) ===\n"
            "  CPU Temp  (0x07) : 45 C  (Sensor 0x08: 38 C)\n"
            "  CPU VR    (0x0A) : 41 C  (dGPU 0x0B  : N/A (D3cold))\n"
            "  CPU Fan   (0xCE) : 2048 RPM  (duty= 71/255,  28%, raw_tach=1053)\n"
            "  GPU Fan   (0xCF) :    0 RPM  (duty=  0/255,   0%, raw_tach=0)\n"
            "  Fan Offset(0xE7) :   0/255 (0%)\n"
            "  Fan Mode  (0xD7) : Auto (0x02)\n"
            "=== NVIDIA GPU ===\n"
            "  NVIDIA dGPU, Suspended (D3cold), 0 W\n"
        )
        fan_d3cold_na = parse_fan_status(
            "=== NVIDIA GPU ===\n"
            "  NVIDIA dGPU, N/A (D3cold), 0 W\n"
        )
        fan_perf_alias = parse_fan_status("  Fan Mode  (0xD7) : Perf-Auto (0x08)\n")
        fan_silent_auto = parse_fan_status("  Fan Mode  (0xD7) : Silent-Auto (0x80, Quiet Profile)\n")
        fan_eco_auto = parse_fan_status("  Fan Mode  (0xD7) : Auto (0x02, Power Saving Eco Policy)\n")
        fan_slow_40 = parse_fan_status("  Fan Mode  (0xD7) : silent / noiseless (0x40)\n")
        check(
            "parse_fan_status live + 0x02/0x08/0x80 -> auto + 0x40 non-collision + D3cold GPU temp + dual curves",
            live_fan["fan_mode_key"] == "auto"
            and live_fan["offset_pct"] == 20
            and live_fan["nvidia_temp"] == 44
            and live_fan.cpu_curve == [(40, 50), (55, 54), (75, 76), (100, 100)]
            and live_fan.gpu_curve == [(40, 50), (58, 58), (78, 80), (100, 100)]
            and fan_perf_alias["fan_mode_key"] == "auto"
            and fan_silent_auto["fan_mode_key"] == "auto"
            and "0x80" in fan_silent_auto["fan_mode_raw"]
            and fan_silent_auto["fan_mode_raw"] == "Silent-Auto (0x80, Quiet Profile)"
            and fan_eco_auto["fan_mode_key"] == "auto"
            and "0x02" in fan_eco_auto["fan_mode_raw"]
            and fan_slow_40["fan_mode_key"] is None
            and fan_d3cold_susp["fan_mode_key"] == "auto"
            and fan_d3cold_susp["cpu_vr_temp"] == 41
            and fan_d3cold_susp["dgpu_temp"] is None
            and fan_d3cold_susp["nvidia_temp"] is None
            and "Suspended (D3cold)" in fan_d3cold_susp["nvidia_text"]
            and fan_d3cold_na["nvidia_temp"] is None
            and "N/A (D3cold)" in fan_d3cold_na["nvidia_text"],
            f"live={live_fan}, silent80={fan_silent_auto}, d3cold={fan_d3cold_susp}",
        )

        fan_no_opt = parse_fan_status(
            "=== EC Thermal & Fan Telemetry (/tmp/ec0_io.bin) ===\n"
            "  CPU Temp  (0x07) : 54 C  (Sensor 0x08: 45 C)\n"
            "  CPU VR    (0x0A) : 47 C  (dGPU 0x0B  : 47 C)\n"
            "  CPU Fan   (0xCE) : 2048 RPM  (duty= 71/255,  28%, raw_tach=1053)\n"
            "  GPU Fan   (0xCF) : 2025 RPM  (duty= 71/255,  28%, raw_tach=1065)\n"
            "  Fan Offset(0xE7) :   0/255 (0%)\n"
        )
        check(
            "parse_fan_status without optional Fan Mode / NVIDIA / Curve defaults",
            fan_no_opt["fan_mode_text"] == "Snapshot (0xD7 skipped)"
            and fan_no_opt["nvidia_text"] == "N/A"
            and fan_no_opt.cpu_curve == [(40, 50), (60, 60), (80, 70), (100, 100)]
            and fan_no_opt.gpu_curve == [(40, 50), (60, 60), (80, 70), (100, 100)],
            str(fan_no_opt),
        )

        periph_live_open = parse_peripherals_status(
            "Webcam (0xB8) : ON (bit0=1, video0=present)\n"
            "Trackpad      : ON (EC bit4=1, input_inhibited=0)\n"
            "Lid State     : Open (EC 0x03=0xC5, LSTE=1)\n"
            "Wake on Lid   : ON (EC LWKE=1, kernel_wakeup=enabled)\n"
            "Lid Close Mode: Suspend (inhibit_pid=none)\n"
            "Win-Key Lock  : OFF (EC 0xE2=0x80, bit4=0)\n"
            "Fn/Win Swap   : OFF (EC 0xE2=0x80, bit3=0)\n"
            "Fn-Lock       : ON (EC 0xEC=0x60, bit5=1)\n"
        )
        periph_live_closed = parse_peripherals_status(
            "Webcam (0xB8) : OFF (bit0=0, video0=absent)\n"
            "Trackpad      : OFF (EC bit4=0, input_inhibited=1)\n"
            "Lid State     : Closed (EC 0x03=0xC0, LSTE=0)\n"
            "Wake on Lid   : OFF (EC LWKE=0, kernel_wakeup=disabled)\n"
            "Lid Close Mode: Ignore (Clamshell) (inhibit_pid=4242)\n"
            "Win-Key Lock  : ON (EC 0xE2=0x98, bit4=1)\n"
            "Fn/Win Swap   : ON (EC 0xE2=0x98, bit3=1)\n"
            "Fn-Lock       : OFF (EC 0xEC=0x40, bit5=0)\n"
        )
        check(
            "parse_peripherals_status synthetic open/suspend & closed/clamshell",
            periph_live_open.webcam == "ON"
            and periph_live_open.lid_state == "Open"
            and periph_live_open.lid_close_key == "suspend"
            and periph_live_open.fn_lock == "ON"
            and periph_live_closed.webcam == "OFF"
            and periph_live_closed.trackpad == "OFF"
            and periph_live_closed.lid_state == "Closed"
            and periph_live_closed.wake_on_lid == "OFF"
            and periph_live_closed.lid_close_key == "ignore"
            and periph_live_closed.inhibit_pid == "4242"
            and periph_live_closed.win_lock == "ON"
            and periph_live_closed.fn_swap == "ON"
            and periph_live_closed.fn_lock == "OFF",
            f"open={periph_live_open}, closed={periph_live_closed}",
        )

        live_rgb = parse_rgb_status(
            "Target mode   : KBType=6 1-zone (0xF9=0x03)\n"
            "Cached state  : color=(0,255,200) ec=(0,127,100) cc_scale=1\n"
            "EC Mailbox    : 00 03 64 00 7f 00\n"
        )
        check(
            "parse_rgb_status live color",
            live_rgb["target_mode"] == "KBType=6 1-zone (0xF9=0x03)"
            and live_rgb["rgb"] == (0, 255, 200)
            and live_rgb["cc_scale"] is True
            and live_rgb["mailbox_text"] == "00 03 64 00 7f 00"
            and "zone" not in live_rgb,
            str(live_rgb),
        )

        fx_rgb = parse_rgb_status(
            "Target mode   : KBType=6 1-zone (0xF9=0x03)\n"
            "Cached state  : effect=breath\n"
            "EC Mailbox    : 00 0f 07 10 00 00\n"
        )
        to_rgb = parse_rgb_status(
            "Target mode   : KBType=6 1-zone (0xF9=0x03)\n"
            "Cached state  : timeout=45\n"
            "EC Mailbox    : 00 ff 00 2d 00 00\n"
        )
        check(
            "parse_rgb_status effect & timeout",
            fx_rgb["target_mode"] == "KBType=6 1-zone (0xF9=0x03)"
            and fx_rgb["effect"] == "breath"
            and fx_rgb["rgb"] is None
            and to_rgb["timeout"] == "45",
            f"fx={fx_rgb}, to={to_rgb}",
        )

        live_bri = parse_brightness_status(
            "Cached state (KBType=6 RGB): brightness=143 (0x8F)\n"
            "Mode (KBType=6)            : 0xCA sub 0x06 (0xF9=0x06, 0xFA=level, 0xF8=0xCA; write-only)\n"
        )
        check(
            "parse_brightness_status live",
            live_bri["level"] == 143
            and live_bri["mode_text"] == "0xCA sub 0x06 (0xF9=0x06, 0xFA=level, 0xF8=0xCA; write-only)"
            and "white_kb_text" not in live_bri,
            str(live_bri),
        )

        live_bat = parse_battery_status(
            "Linux sysfs   : status=Not charging, capacity=78%\n"
            "EC Power      : AC=1, BAT0=1, Remaining=78%, Cycles=16\n"
            "FlexiCharger  : Enabled (80% limit, Stop=80% Start=60% | Direct EC 0xCB / XRAM 0x06D0=0x1B)\n"
        )
        bal_bat = parse_battery_status(
            "Linux sysfs   : status=Charging, capacity=72%\n"
            "EC Power      : AC=1, BAT0=1, Remaining=72%, Cycles=16\n"
            "FlexiCharger  : Enabled (85% limit, Stop=85% Start=70% | Direct EC 0xCB / XRAM 0x06D0=0x1A)\n"
        )
        dis_bat = parse_battery_status(
            "Linux sysfs   : status=Full, capacity=100%\n"
            "EC Power      : AC=1, BAT0=1, Remaining=100%, Cycles=15\n"
            "FlexiCharger  : Disabled (100% full charge, Stop=100% Start=95% | Direct EC 0xCB / XRAM 0x06D0=0x18)\n"
        )
        dis_zero_bat = parse_battery_status(
            "FlexiCharger  : Disabled (100% full charge, Stop=100% Start=0% | Direct EC 0xCB / XRAM 0x06D0=0x18)\n"
        )
        ready_bat = parse_battery_status(
            "Linux sysfs   : status=Charging, capacity=52%\n"
            "EC Power      : AC=1, BAT0=1, Remaining=52%, Cycles=15\n"
            "FlexiCharger  : Ready (Direct EC 0xCB / XRAM 0x06D0..0x06D2; run via root/daemon to read live XRAM)\n"
        )
        unavail_bat = parse_battery_status(
            "Linux sysfs   : status=Discharging, capacity=52%\n"
            "EC Power      : AC=0, BAT0=1, Remaining=52%, Cycles=15\n"
            "FlexiCharger  : (unavailable: unlock live EC)\n"
        )
        legacy_f1_bat = parse_battery_status(
            "EC FCMD 0xF1  : Enabled (start=60%, stop=80%) [raw: 0xFA=0x3C, 0xFB=0x50]\n"
        )
        check(
            "parse_battery_status Direct EC 0xCB / XRAM 0x06D0..0x06D2 FlexiCharger (0x1B enabled, 0x1A, 0x18 disabled, ready, unavailable)",
            live_bat["f1_enabled"] is True
            and live_bat["f1_start"] == 60
            and live_bat["f1_stop"] == 80
            and live_bat["f1_raw"] == "Direct EC 0xCB / XRAM 0x06D0=0x1B"
            and bal_bat["f1_enabled"] is True
            and bal_bat["f1_start"] == 70
            and bal_bat["f1_stop"] == 85
            and bal_bat["f1_raw"] == "Direct EC 0xCB / XRAM 0x06D0=0x1A"
            and dis_bat["f1_enabled"] is False
            and dis_bat["f1_start"] == 95
            and dis_bat["f1_stop"] == 100
            and dis_bat["f1_raw"] == "Direct EC 0xCB / XRAM 0x06D0=0x18"
            and dis_zero_bat["f1_enabled"] is False
            and dis_zero_bat["f1_start"] is None
            and dis_zero_bat["f1_stop"] == 100
            and ready_bat["f1_enabled"] is None
            and ready_bat["f1_start"] is None
            and ready_bat["f1_stop"] is None
            and "Ready" in ready_bat["f1_text"]
            and "XRAM 0x06D0..0x06D2" in ready_bat["f1_text"]
            and unavail_bat["f1_enabled"] is None
            and unavail_bat["f1_start"] is None
            and unavail_bat["f1_stop"] is None
            and legacy_f1_bat["f1_enabled"] is True
            and legacy_f1_bat["f1_start"] == 60
            and legacy_f1_bat["f1_stop"] == 80
            and legacy_f1_bat["f1_raw"] == "0xFA=0x3C, 0xFB=0x50"
            and set(live_bat.keys()) == {
                "dry_run",
                "sysfs_status",
                "sysfs_capacity",
                "sysfs_text",
                "ac_online",
                "bat_present",
                "ec_remaining",
                "ec_cycles",
                "ec_power_text",
                "f1_text",
                "f1_enabled",
                "f1_start",
                "f1_stop",
                "f1_raw",
            },
            f"en={live_bat}, dis={dis_bat}, ready={ready_bat}, unavail={unavail_bat}",
        )

        # 2. Test dry-run command dispatch across all 6 scripts (with isolated XDG_CACHE_HOME)
        dry_runner = ScriptRunner(dry_run=True, cache_dir=tmp_cache)

        dry_bri_status = dry_runner.run_script("brightness.sh", ["status"])
        parsed_dry_bri = parse_brightness_status(dry_bri_status.stdout)
        check(
            "parse_brightness_status real --dry-run status",
            dry_bri_status.rc == 0
            and parsed_dry_bri["dry_run"] is True
            and parsed_dry_bri["cached_text"] == "(none recorded yet)"
            and parsed_dry_bri["mode_text"] == "0xCA sub 0x06 (0xF9=0x06, 0xFA=level, 0xF8=0xCA; write-only)",
            f"rc={dry_bri_status.rc}, parsed={parsed_dry_bri}",
        )
        dry_rgb_status = dry_runner.run_script("rgb.sh", ["status"])
        parsed_dry_rgb = parse_rgb_status(dry_rgb_status.stdout)
        check(
            "parse_rgb_status real --dry-run status",
            dry_rgb_status.rc == 0
            and parsed_dry_rgb["dry_run"] is True
            and parsed_dry_rgb["target_mode"] == "KBType=6 1-zone (0xF9=0x03)"
            and parsed_dry_rgb["cached_text"] == "(none recorded yet)",
            f"rc={dry_rgb_status.rc}, parsed={parsed_dry_rgb}",
        )

        dry_cases = [
            ("power_profile.sh", ["quiet"]),
            ("power_profile.sh", ["powersaving"]),
            ("power_profile.sh", ["entertainment"]),
            ("power_profile.sh", ["performance"]),
            ("power_profile.sh", ["--pl1", "40", "--pl2", "95", "performance"]),
            ("power_profile.sh", ["--no-rapl", "--no-os", "quiet"]),
            ("fan.sh", ["auto"]),
            ("fan.sh", ["max"]),
            ("fan.sh", ["custom", "55", "52", "75", "72", "58", "55", "78", "75"]),
            ("fan.sh", ["offset", "25"]),
            ("fan.sh", ["reset"]),
            ("brightness.sh", ["high"]),
            ("brightness.sh", ["120"]),
            ("rgb.sh", ["255", "0", "128"]),
            ("rgb.sh", ["--cc-scale", "255", "0", "128"]),
            ("rgb.sh", ["cyan"]),
            ("rgb.sh", ["--effect", "breath"]),
            ("rgb.sh", ["--timeout", "30"]),
            ("rgb.sh", ["--timeout", "off"]),
            ("battery.sh", ["limit", "60", "80"]),
            ("battery.sh", ["full"]),
            ("peripherals.sh", ["status"]),
            ("peripherals.sh", ["webcam", "on"]),
            ("peripherals.sh", ["webcam", "off"]),
            ("peripherals.sh", ["touchpad", "on"]),
            ("peripherals.sh", ["touchpad", "off"]),
            ("peripherals.sh", ["lid-wake", "on"]),
            ("peripherals.sh", ["lid-wake", "off"]),
            ("peripherals.sh", ["lid-close", "suspend"]),
            ("peripherals.sh", ["lid-close", "ignore"]),
            ("peripherals.sh", ["win-lock", "on"]),
            ("peripherals.sh", ["win-lock", "off"]),
            ("peripherals.sh", ["fn-swap", "on"]),
            ("peripherals.sh", ["fn-swap", "off"]),
            ("peripherals.sh", ["fn-lock", "on"]),
            ("peripherals.sh", ["fn-lock", "off"]),
        ]
        for script, args in dry_cases:
            r = dry_runner.run_script(script, args)
            extra_ok = True
            if script == "power_profile.sh" and args == ["quiet"]:
                extra_ok = (
                    "OEM PL1/PL2 (quiet): PL1=15.000 W (15000000 uW), PL2=30.000 W (30000000 uW)" in r.stdout
                    and "RAPL PL1=15000000 uW (15.000 W), PL2=30000000 uW (30.000 W)" in r.stdout
                    and "ec[0xFA]=0x02" in r.stdout
                )
            elif script == "power_profile.sh" and args == ["powersaving"]:
                extra_ok = (
                    "OEM PL1/PL2 (powersaving): PL1=15.000 W (15000000 uW), PL2=30.000 W (30000000 uW)" in r.stdout
                    and "RAPL PL1=15000000 uW (15.000 W), PL2=30000000 uW (30.000 W)" in r.stdout
                    and "ec[0xFA]=0x03" in r.stdout
                )
            elif script == "power_profile.sh" and args == ["entertainment"]:
                extra_ok = (
                    "OEM PL1/PL2 (entertainment): PL1=45.000 W (45000000 uW), PL2=115.000 W (115000000 uW)" in r.stdout
                    and "RAPL PL1=45000000 uW (45.000 W), PL2=115000000 uW (115.000 W)" in r.stdout
                    and "ec[0xFA]=0x00" in r.stdout
                )
            elif script == "power_profile.sh" and args == ["performance"]:
                extra_ok = (
                    "90W/115W" in r.stdout
                    and "OEM PL1/PL2 (performance): PL1=90.000 W (90000000 uW), PL2=115.000 W (115000000 uW)" in r.stdout
                    and "RAPL PL1=90000000 uW (90.000 W), PL2=115000000 uW (115.000 W)" in r.stdout
                    and "ec[0xFA]=0x01" in r.stdout
                )
            elif script == "rgb.sh" and args == ["255", "0", "128"]:
                extra_ok = (
                    "ec[0xF9]=0x03 (KBType=6 1-zone)" in r.stdout
                    and "ec[0xFA]=0x80 (B)" in r.stdout
                    and "ec[0xFB]=0xFF (R)" in r.stdout
                    and "ec[0xFC]=0x00 (G)" in r.stdout
                    and "ec[0xF8]=0xCA" in r.stdout
                )
            elif script == "rgb.sh" and args == ["--cc-scale", "255", "0", "128"]:
                extra_ok = (
                    "ec[0xF9]=0x03 (KBType=6 1-zone)" in r.stdout
                    and "ec[0xFA]=0x40 (B)" in r.stdout
                    and "ec[0xFB]=0x7F (R)" in r.stdout
                    and "[cc-scale from (255,0,128)]" in r.stdout
                )
            elif script == "rgb.sh" and args == ["cyan"]:
                extra_ok = (
                    "ec[0xF9]=0x03 (KBType=6 1-zone)" in r.stdout
                    and "ec[0xFA]=0xC8 (B)" in r.stdout
                    and "ec[0xFB]=0x00 (R)" in r.stdout
                    and "ec[0xFC]=0xFF (G)" in r.stdout
                )
            elif script == "rgb.sh" and args == ["--effect", "breath"]:
                extra_ok = (
                    "ec[0xF9]=0x0F" in r.stdout
                    and "ec[0xFA]=0x07" in r.stdout
                    and "ec[0xFB]=0x10" in r.stdout
                    and "ec[0xF8]=0xCA" in r.stdout
                )
            elif script == "rgb.sh" and args == ["--timeout", "30"]:
                extra_ok = (
                    "ec[0xF9]=0xFF" in r.stdout
                    and "ec[0xFB]=0x1E (sec_lo)" in r.stdout
                    and "ec[0xF8]=0xD4" in r.stdout
                )
            elif script == "rgb.sh" and args == ["--timeout", "off"]:
                extra_ok = (
                    "ec[0xF9]=0x00" in r.stdout
                    and "ec[0xFB]=0x00 (sec_lo)" in r.stdout
                    and "ec[0xF8]=0xD4" in r.stdout
                )
            check(
                f"Dry-run {script} {' '.join(args)}",
                r.rc == 0 and "[dry-run]" in r.stdout and extra_ok,
                f"rc={r.rc}, out={r.stdout.strip()!r}, err={r.stderr.strip()!r}",
            )

        bad_pp_res = dry_runner.run_script("power_profile.sh", ["turbo"])
        check(
            "Dry-run power_profile.sh rejects removed turbo profile",
            bad_pp_res.rc != 0,
            f"rc={bad_pp_res.rc}, err={bad_pp_res.stderr.strip()!r}",
        )

        rgb_reject_cases = [
            (["255", "0", "128", "3"], "no zone argument"),
            (["cyan", "3"], "no zone argument"),
            (["--effect", "breath-3zone"], "unknown effect"),
            (["--effect", "cycle"], "unknown effect"),
        ]
        for bad_args, err_substr in rgb_reject_cases:
            bad_res = dry_runner.run_script("rgb.sh", bad_args)
            check(
                f"Dry-run rgb.sh rejects {' '.join(bad_args)}",
                bad_res.rc != 0 and err_substr in bad_res.stderr,
                f"rc={bad_res.rc}, err={bad_res.stderr.strip()!r}",
            )

        check(
            "RGB_EFFECTS only contains KBType=6 breath",
            len(RGB_EFFECTS) == 1 and RGB_EFFECTS[0][0] == "breath" and "0xCA Sub 0x0F" in RGB_EFFECTS[0][1],
            str(RGB_EFFECTS),
        )

        # Verify self-test dry-run & status calls wrote nothing inside tmp_cache/acer-a715-59g
        check(
            "Self-test cache hygiene (no state written during dry-run/status)",
            not os.path.exists(os.path.join(tmp_cache, "acer-a715-59g")),
            f"tmp_cache={tmp_cache}",
        )

        # 3. Test --worker JSON IPC round-trip (local subprocess without pkexec)
        local_runner = ScriptRunner(dry_run=False, cache_dir=tmp_cache)
        ok, msg = local_runner.start_worker(use_pkexec=False)
        check("Local --worker handshake", ok, msg)
        if ok:
            w_res1 = local_runner.run_script("fan.sh", ["--dry-run", "status"])
            check(
                "Worker IPC fan.sh --dry-run status",
                w_res1.mode == "worker" and w_res1.rc == 0 and "[dry-run]" in w_res1.stdout,
                f"rc={w_res1.rc}, out={w_res1.stdout.strip()!r}",
            )
            w_res2 = local_runner.run_script("rgb.sh", ["--dry-run", "blue"])
            check(
                "Worker IPC rgb.sh --dry-run blue",
                w_res2.mode == "worker" and w_res2.rc == 0 and "[dry-run]" in w_res2.stdout,
                f"rc={w_res2.rc}, out={w_res2.stdout.strip()!r}",
            )
            # Also verify worker rejects disallowed script via direct pipe check
            try:
                rc_bad, _out_bad, err_bad = local_runner._call_worker("evil.sh", ["status"])
                check("Worker rejects disallowed script", rc_bad != 0 and "Disallowed" in err_bad, err_bad)
            except Exception as exc:  # noqa: BLE001
                check("Worker rejects disallowed script", False, str(exc))
            local_runner.stop_worker()
            check("Worker clean shutdown", not local_runner.worker_alive())

        # Test pkexec cancellation (rc=126) and immediate exit (rc=127) via stub pkexec
        if os.geteuid() != 0:
            stub_dir = os.path.join(tmp_cache, "bin")
            os.makedirs(stub_dir, exist_ok=True)
            stub_pkexec = os.path.join(stub_dir, "pkexec")
            old_path = os.environ.get("PATH", "")
            try:
                os.environ["PATH"] = f"{stub_dir}:{old_path}"
                for code, expected_tag in ((126, "rc=126"), (127, "rc=127")):
                    with open(stub_pkexec, "w", encoding="utf-8") as fp:
                        fp.write(
                            "#!/bin/sh\n"
                            f"echo 'Error executing command as another user: test_{code}' >&2\n"
                            f"exit {code}\n"
                        )
                    os.chmod(stub_pkexec, 0o755)
                    pk_runner = ScriptRunner(dry_run=False, cache_dir=tmp_cache)
                    pk_ok, pk_msg = pk_runner.start_worker(use_pkexec=True)
                    check(
                        f"pkexec failure handling ({expected_tag})",
                        (not pk_ok) and (not pk_runner.worker_alive()) and (expected_tag in pk_msg),
                        pk_msg,
                    )
            finally:
                os.environ["PATH"] = old_path

        # 3c. Daemon UNIX domain socket integration (headless)
        daemon_spec = importlib.util.spec_from_file_location(
            "acer_control_daemon", os.path.join(SCRIPT_DIR, "daemon.py")
        )
        assert daemon_spec is not None and daemon_spec.loader is not None
        daemon_mod = importlib.util.module_from_spec(daemon_spec)
        daemon_spec.loader.exec_module(daemon_mod)

        test_sock = os.path.join(tmp_cache, "gui_daemon_test.sock")
        daemon_srv = daemon_mod.DaemonServer(
            socket_path=test_sock,
            uid=os.getuid(),
            gid=os.getgid(),
            cache_dir=tmp_cache,
        )
        daemon_srv.start_in_thread()
        try:
            os.environ["ACER_CONTROL_SOCK"] = test_sock
            d_runner = ScriptRunner(dry_run=False, cache_dir=tmp_cache)
            check(
                "ScriptRunner detects daemon via ACER_CONTROL_SOCK",
                d_runner.daemon_available()
                and d_runner.is_live_root()
                and d_runner.is_live_ec()
                and d_runner.mode_label() == "daemon"
                and d_runner.current_mode_label()[0] == "live"
                and "Daemon Connected (Live EC" in d_runner.current_mode_label()[1],
                f"mode={d_runner.mode_label()}, label={d_runner.current_mode_label()}",
            )
            if os.path.isfile(DEFAULT_EC_SNAPSHOT):
                d_pp = d_runner.run_script("power_profile.sh", ["--ec-file", DEFAULT_EC_SNAPSHOT, "status"])
                check(
                    "Daemon socket power_profile.sh --ec-file status",
                    d_pp.mode == "daemon" and d_pp.rc == 0 and "EC hardware PECI" in d_pp.stdout,
                    f"mode={d_pp.mode}, rc={d_pp.rc}, err={d_pp.stderr.strip()!r}",
                )
                d_explicit = ScriptRunner(
                    dry_run=False, explicit_ec_file=DEFAULT_EC_SNAPSHOT, cache_dir=tmp_cache
                )
                d_exp_res = d_explicit.run_script("fan.sh", ["status"])
                check(
                    "Explicit --ec-file takes precedence over daemon socket",
                    (not d_explicit.is_live_root())
                    and d_explicit.mode_label() == "ec-file"
                    and d_exp_res.mode == "ec-file"
                    and d_exp_res.rc == 0,
                    f"mode={d_explicit.mode_label()}, res_mode={d_exp_res.mode}",
                )
            d_fan = d_runner.run_script("fan.sh", ["--dry-run", "auto"])
            check(
                "Daemon socket fan.sh --dry-run auto",
                d_fan.mode == "daemon" and d_fan.rc == 0 and "[dry-run]" in d_fan.stdout,
                f"mode={d_fan.mode}, rc={d_fan.rc}, out={d_fan.stdout.strip()!r}",
            )
        finally:
            daemon_srv.shutdown()

        # Verify graceful fallback to snapshot/locked mode after daemon socket stops/drops
        d_post_drop = d_runner.run_script("fan.sh", ["auto"])
        check(
            "Daemon offline falls back cleanly to snapshot/locked mode",
            (not d_runner.daemon_available())
            and (not d_runner.is_live_root())
            and d_post_drop.mode == "snapshot-dry-run"
            and d_post_drop.rc == 0,
            f"mode={d_post_drop.mode}, rc={d_post_drop.rc}",
        )
        orig_daemon_avail = d_runner.daemon_available
        d_runner.daemon_available = lambda: True  # type: ignore[method-assign]
        try:
            d_mid_drop = d_runner.run_script("fan.sh", ["auto"])
        finally:
            d_runner.daemon_available = orig_daemon_avail  # type: ignore[method-assign]
        check(
            "Mid-call daemon socket drop falls back to snapshot/locked mode",
            d_mid_drop.mode == "snapshot-dry-run"
            and d_mid_drop.rc == 0
            and "connection dropped" in d_mid_drop.note.lower(),
            f"mode={d_mid_drop.mode}, note={d_mid_drop.note!r}",
        )
        os.environ["ACER_CONTROL_SOCK"] = ""

        # 3d. Persistent config_store integration, CLI options & legacy daemon fallback replay (headless)
        cli_cfg_args = parse_cli(["--config", "/tmp/acer_test_cfg.json", "--no-auto-apply"])
        check(
            "parse_cli parses --config and --no-auto-apply",
            cli_cfg_args.config == "/tmp/acer_test_cfg.json" and cli_cfg_args.no_auto_apply is True,
            str(cli_cfg_args),
        )

        cfg_test_path = os.path.join(tmp_cache, "runner_cfg_test.json")
        sample_cfg = config_store.default_config()
        sample_cfg["power"]["profile"] = "entertainment"
        sample_cfg["keyboard"]["brightness"] = 191
        sample_cfg["keyboard"]["mode"] = "effect"
        sample_cfg["keyboard"]["effect"] = "breath"
        sample_cfg["battery"] = {"mode": "limit", "start_pct": 70, "stop_pct": 85}
        sample_cfg["peripherals"]["win_lock"] = "on"
        config_store.save_config(cfg_test_path, sample_cfg, uid=os.getuid(), gid=os.getgid())

        # Unprivileged without daemon skips force=False (no pkexec prompts) and previews force=True
        locked_cfg_runner = ScriptRunner(dry_run=False, cache_dir=tmp_cache, config_path=cfg_test_path)
        locked_auto = locked_cfg_runner.apply_saved_config(force=False)
        locked_manual = locked_cfg_runner.apply_saved_config(force=True)
        check(
            "apply_saved_config skips force=False when unprivileged without daemon, allows force=True preview",
            locked_auto.skipped is True
            and locked_auto.mode == "skipped"
            and len(locked_auto.applied) == 0
            and locked_manual.skipped is False
            and locked_manual.mode == "snapshot-dry-run"
            and locked_manual.rc == 0
            and len(locked_manual.applied) == 5,
            f"auto={locked_auto.mode}/{locked_auto.skipped}, manual={locked_manual.mode}/{len(locked_manual.applied)}",
        )

        # auto_apply_on_launch=False skips even when daemon is available unless force=True
        cfg_sock = os.path.join(tmp_cache, "gui_cfg_daemon.sock")
        cfg_daemon_srv = daemon_mod.DaemonServer(
            socket_path=cfg_sock,
            uid=os.getuid(),
            gid=os.getgid(),
            cache_dir=tmp_cache,
            config_path=cfg_test_path,
        )
        orig_cfg_exec = cfg_daemon_srv._execute_script
        daemon_exec_calls: list[tuple[str, list[str]]] = []

        def _mock_cfg_exec(script: str, act_args: list[str]) -> tuple[int, str, str]:
            daemon_exec_calls.append((script, list(act_args)))
            return (0, f"[mock] {script} {' '.join(act_args)}", "")

        cfg_daemon_srv._execute_script = _mock_cfg_exec  # type: ignore[method-assign]
        cfg_daemon_srv.start_in_thread()
        try:
            with _temp_env("ACER_CONTROL_SOCK", cfg_sock):
                cd_runner = ScriptRunner(dry_run=False, cache_dir=tmp_cache, config_path=cfg_test_path)
                _ok, saved_off, via_off = cd_runner.set_auto_apply_on_launch(False)
                skip_res = cd_runner.apply_saved_config(force=False)
                calls_when_skipped = len(daemon_exec_calls)
                force_res = cd_runner.apply_saved_config(force=True)
                _ok, saved_on, via_on = cd_runner.set_auto_apply_on_launch(True)
                check(
                    "Daemon apply_config respects auto_apply_on_launch=False vs force=True & set_config persists",
                    via_off == "daemon"
                    and saved_off["auto_apply_on_launch"] is False
                    and skip_res.skipped is True
                    and skip_res.mode == "skipped"
                    and calls_when_skipped == 0
                    and force_res.mode == "daemon-apply"
                    and force_res.rc == 0
                    and len(daemon_exec_calls) == 5
                    and via_on == "daemon"
                    and saved_on["auto_apply_on_launch"] is True,
                    f"calls={daemon_exec_calls}, skip={skip_res.mode}, force={force_res.mode}",
                )
        finally:
            cfg_daemon_srv._execute_script = orig_cfg_exec  # type: ignore[method-assign]
            cfg_daemon_srv.shutdown()

        # Fallback replay when older daemon rejects non-"script" requests ("cmd" / "apply_config")
        legacy_sock = os.path.join(tmp_cache, "gui_legacy_daemon.sock")
        legacy_reqs: list[dict[str, Any]] = []
        legacy_stop = threading.Event()
        legacy_srv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        legacy_srv_sock.bind(legacy_sock)
        legacy_srv_sock.listen(8)
        legacy_srv_sock.settimeout(0.1)

        def _serve_legacy() -> None:
            while not legacy_stop.is_set():
                try:
                    conn, _ = legacy_srv_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    raw = b""
                    while b"\n" not in raw:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        raw += chunk
                    if not raw:
                        continue
                    req = json.loads(raw.decode("utf-8").strip())
                    legacy_reqs.append(req)
                    rid = req.get("id")
                    if req.get("ping"):
                        resp = {"id": rid, "rc": 0, "stdout": "pong", "stderr": "", "euid": 0, "version": "1.0"}
                    elif not isinstance(req.get("script"), str):
                        resp = {
                            "id": rid,
                            "rc": 1,
                            "stdout": "",
                            "stderr": "Daemon error: Disallowed or invalid script: None",
                        }
                    else:
                        resp = {
                            "id": rid,
                            "rc": 0,
                            "stdout": f"[legacy] {req['script']} {' '.join(req.get('args', []))}",
                            "stderr": "",
                        }
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

        legacy_th = threading.Thread(target=_serve_legacy, daemon=True)
        legacy_th.start()
        try:
            with _temp_env("ACER_CONTROL_SOCK", legacy_sock):
                leg_runner = ScriptRunner(dry_run=False, cache_dir=tmp_cache, config_path=cfg_test_path)
                leg_res = leg_runner.apply_saved_config(force=False)
                script_reqs = [
                    (r.get("script"), r.get("args"))
                    for r in legacy_reqs
                    if isinstance(r.get("script"), str)
                ]
                saw_cmd_reject = any(
                    (not r.get("ping")) and (r.get("cmd") == "apply_config" or r.get("apply_config"))
                    for r in legacy_reqs
                )
                check(
                    "Legacy daemon rejecting cmd/apply_config seamlessly falls back to sequential script IPC",
                    saw_cmd_reject
                    and leg_res.mode == "daemon-replay"
                    and leg_res.rc == 0
                    and script_reqs
                    == [
                        ("power_profile.sh", ["entertainment"]),
                        ("brightness.sh", ["191"]),
                        ("rgb.sh", ["--effect", "breath"]),
                        ("battery.sh", ["limit", "70", "85"]),
                        ("peripherals.sh", ["win-lock", "on"]),
                    ],
                    f"mode={leg_res.mode}, saw_cmd={saw_cmd_reject}, scripts={script_reqs}",
                )
        finally:
            legacy_stop.set()
            try:
                legacy_srv_sock.close()
            except OSError:
                pass
            legacy_th.join(timeout=2.0)
            if os.path.exists(legacy_sock):
                os.unlink(legacy_sock)

        # 3b. Headless SVG rendering & source hygiene checks (no DISPLAY required)
        swatch_ok = True
        for _preset_name, (pr, pg, pb_val) in RGB_PRESETS:
            sw_pb = _render_svg_pixbuf(_build_swatch_svg(pr, pg, pb_val, size=14))
            if sw_pb is None or sw_pb.get_width() != 14 or sw_pb.get_height() != 14:
                swatch_ok = False
                break
        check(
            f"SVG swatch rendering ({len(RGB_PRESETS)}/11 RGB presets)",
            swatch_ok and _render_svg_pixbuf("<svg><broken") is None,
            f"presets={len(RGB_PRESETS)}",
        )

        shifted_4pt = _apply_curve_offset([(40, 50), (60, 60), (80, 70), (100, 100)], 25)
        check(
            "Fan curve offset clamp math (Custom 4-pt +25%)",
            len(shifted_4pt) == 5
            and shifted_4pt[0] == (40.0, 75.0)
            and shifted_4pt[2] == (80.0, 95.0)
            and shifted_4pt[3][1] == 100.0
            and abs(shifted_4pt[3][0] - (80.0 + 20.0 / 6.0)) < 1e-6,
            str(shifted_4pt),
        )

        # Dual-curve node clamping & pixel <-> curve coordinate helpers
        base_c = [(40, 50), (60, 60), (80, 70), (100, 100)]
        clamped_n2_hi = clamp_fan_curve_node(base_c, 1, 95, 95)
        clamped_n2_lo = clamp_fan_curve_node(base_c, 1, 30, 40)
        clamped_n3_lo = clamp_fan_curve_node(base_c, 2, 50, 55)
        clamped_n3_hi = clamp_fan_curve_node([(35, 40), (60, 60), (80, 70), (100, 100)], 2, 100, 100)
        px_n2, py_n2 = fan_curve_point_to_px(55, 58)
        rt_t, rt_d = fan_graph_px_to_temp_duty(px_n2, py_n2)
        hit_cpu = hit_test_fan_curve_node(
            *fan_curve_point_to_px(55, 52),
            [(40, 50), (55, 52), (75, 72), (100, 100)],
            [(40, 50), (62, 64), (84, 82), (100, 100)],
        )
        hit_gpu = hit_test_fan_curve_node(
            *fan_curve_point_to_px(84, 82),
            [(40, 50), (55, 52), (75, 72), (100, 100)],
            [(40, 50), (62, 64), (84, 82), (100, 100)],
        )
        hit_miss = hit_test_fan_curve_node(
            *fan_curve_point_to_px(32, 10),
            [(40, 50), (55, 52), (75, 72), (100, 100)],
            [(40, 50), (62, 64), (84, 82), (100, 100)],
        )
        check(
            "Dual-curve node clamping, px round-trip, and hit-testing",
            clamped_n2_hi[1] == (79, 69)
            and clamped_n2_lo[1] == (41, 51)
            and clamped_n3_lo[2] == (61, 61)
            and clamped_n3_hi[0] == (35, 40)
            and clamped_n3_hi[2] == (99, 100)
            and (rt_t, rt_d) == (55, 58)
            and hit_cpu == ("cpu", 1)
            and hit_gpu == ("gpu", 2)
            and hit_miss is None,
            f"hi={clamped_n2_hi}, lo={clamped_n2_lo}, n3={clamped_n3_hi}, rt={(rt_t, rt_d)}, hits={(hit_cpu, hit_gpu, hit_miss)}",
        )

        graph_render_ok = True
        graph_detail = ""
        sample_trail_cpu = [(42.0, 24.0), (51.0, 30.0), (57.0, 42.0), (61.0, 50.0)]
        sample_trail_gpu = [(40.0, 22.0), (45.0, 28.0), (48.0, 38.0), (49.0, 47.0)]
        test_cpu_curve = [(40, 50), (55, 54), (75, 76), (100, 100)]
        test_gpu_curve = [(40, 50), (62, 61), (82, 84), (100, 100)]
        for mode_key in ("auto", "max", "custom", None):
            for off_val in (0, 20, 100):
                mode_lbl = (
                    "Auto (0x08, Performance Profile)"
                    if (mode_key == "auto" and off_val == 20)
                    else (mode_key or "Snapshot (0xD7 skipped)")
                )
                svg_doc = build_fan_curve_svg(
                    fan_mode_key=mode_key,
                    fan_mode_label=mode_lbl,
                    offset_pct=off_val,
                    cpu_temp=61,
                    cpu_duty_pct=50,
                    cpu_rpm=3100,
                    gpu_temp=49,
                    gpu_duty_pct=47,
                    gpu_rpm=2950,
                    cpu_history=sample_trail_cpu,
                    gpu_history=sample_trail_gpu,
                    offset_is_preview=(off_val == 20),
                    cpu_curve=test_cpu_curve,
                    gpu_curve=test_gpu_curve,
                    active_node=("cpu", 1) if mode_key == "custom" else None,
                )
                has_tags = all(
                    tag in svg_doc
                    for tag in ("<svg", "<rect", "<line", "<polyline", "<polygon", "<circle", "<text", "#38bdf8", "#34d399")
                )
                has_mode_extras = True
                if mode_key == "auto":
                    expected_hdr = (
                        "Mode: Auto (0x08, Performance) • Firmware Curve"
                        if off_val == 20
                        else "Mode: Auto (0x02) • Firmware Curve"
                    )
                    has_mode_extras = (
                        expected_hdr in svg_doc
                        and (
                            "Auto runs internal EC firmware table + Fan Offset (0xE7); "
                            "Custom (0x04) applies CPU F1 &amp; GPU F2 curves"
                        )
                        in svg_doc
                        and (("<path " in svg_doc) if off_val > 0 else True)
                    )
                elif mode_key == "max":
                    has_mode_extras = ("<path " in svg_doc) and ("#f87171" in svg_doc)
                g_pb = _render_svg_pixbuf(svg_doc)
                if not (
                    has_tags
                    and has_mode_extras
                    and g_pb is not None
                    and g_pb.get_width() == 640
                    and g_pb.get_height() == 260
                ):
                    graph_render_ok = False
                    graph_detail = f"mode={mode_key}, off={off_val}, tags={has_tags}, extras={has_mode_extras}, pb={g_pb}"
                    break
            if not graph_render_ok:
                break
        locked_svg = build_fan_curve_svg(
            fan_mode_key="auto",
            fan_mode_label="Silent-Auto (0x80, Quiet Profile)",
            offset_pct=0,
            cpu_temp=48,
            cpu_duty_pct=25,
            cpu_rpm=1900,
            gpu_temp=None,
            gpu_duty_pct=None,
            gpu_rpm=None,
            cpu_curve=test_cpu_curve,
            gpu_curve=test_gpu_curve,
            active_node=("cpu", 1),
            fan_locked=True,
        )
        locked_pb = _render_svg_pixbuf(locked_svg)
        locked_auto_svg = build_fan_curve_svg(
            fan_mode_key="auto",
            fan_mode_label="Auto (0x02, Power Saving Eco Policy)",
            offset_pct=35,
            cpu_temp=52,
            cpu_duty_pct=30,
            cpu_rpm=2100,
            gpu_temp=46,
            gpu_duty_pct=25,
            gpu_rpm=1850,
            cpu_curve=test_cpu_curve,
            gpu_curve=test_gpu_curve,
            fan_locked=True,
        )
        locked_auto_pb = _render_svg_pixbuf(locked_auto_svg)
        check(
            "SVG dual-curve fan graph rendering (stock modes, offsets, & read-only Quiet/Power Saving, 640x260)",
            graph_render_ok
            and "Mode: EC Silent-Auto Curve (0x80 / XRAM 0x038C•0x039A • Read-Only)" in locked_svg
            and "Read-Only" in locked_svg.split("</text>", 1)[0]
            and "0x038C" in locked_svg.split("</text>", 1)[0]
            and "Quiet (0xD7=0x80) / Power Saving (0xD7=0x02, 0xDC=0x1A) active — showing read-only EC auto curve" in locked_svg
            and "Internal EC Auto Fan Curve Active (Not Shown in App)" not in locked_svg
            and "EC Auto Curve (Hidden)" not in locked_svg
            and "<polyline" in locked_svg
            and "<circle" in locked_svg
            and 'fill="#34d399" fill-opacity="0.07"' in locked_svg
            and 'fill="#38bdf8" fill-opacity="0.08"' in locked_svg
            and "<path " not in locked_svg
            and "CPU F1" in locked_svg
            and "GPU F2" in locked_svg
            and "CPU N2:" not in locked_svg
            and "(55°C, 54%)" in locked_svg
            and "(62°C, 61%)" in locked_svg
            and "(40°C, 50%)" in locked_svg
            and "(100°C, 100%)" in locked_svg
            and 'r="5.0"' in locked_svg
            and 'r="7.2"' in locked_svg
            and "1900 RPM" in locked_svg
            and 'stroke-dasharray="3,3"' in locked_svg
            and locked_pb is not None
            and locked_pb.get_width() == 640
            and locked_pb.get_height() == 260
            and "Mode: EC Silent / Eco Auto Curve (XRAM 0x038C•0x039A • Read-Only)" in locked_auto_svg
            and "Read-Only" in locked_auto_svg.split("</text>", 1)[0]
            and "0x038C" in locked_auto_svg.split("</text>", 1)[0]
            and "<polyline" in locked_auto_svg
            and "<circle" in locked_auto_svg
            and "<path " not in locked_auto_svg
            and 'fill="#34d399" fill-opacity="0.07"' in locked_auto_svg
            and 'fill="#38bdf8" fill-opacity="0.08"' in locked_auto_svg
            and "Internal EC Auto Fan Curve Active (Not Shown in App)" not in locked_auto_svg
            and "2100 RPM" in locked_auto_svg
            and "1850 RPM" in locked_auto_svg
            and locked_auto_pb is not None
            and locked_auto_pb.get_width() == 640
            and locked_auto_pb.get_height() == 260,
            graph_detail,
        )

        na_svg = build_fan_curve_svg(
            fan_mode_key=None,
            fan_mode_label="Snapshot (0xD7 skipped)",
            offset_pct=0,
            cpu_temp=None,
            cpu_duty_pct=None,
            cpu_rpm=None,
            gpu_temp=None,
            gpu_duty_pct=None,
            gpu_rpm=None,
            cpu_history=(),
            gpu_history=(),
        )
        na_pb = _render_svg_pixbuf(na_svg)
        check(
            "SVG fan curve graph handles N/A temps/duties",
            ("CPU: N/A" in na_svg)
            and ("GPU: N/A" in na_svg)
            and (na_pb is not None)
            and na_pb.get_width() == 640
            and na_pb.get_height() == 260,
        )

        with open(os.path.realpath(__file__), "r", encoding="utf-8") as src_fp:
            tree = ast.parse(src_fp.read(), filename=__file__)
        drawing_area_nodes = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.Attribute) and node.attr == "DrawingArea")
            or (isinstance(node, ast.Name) and node.id == "DrawingArea")
        ]
        draw_connect_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "draw"
        ]
        check(
            "Zero Gtk.DrawingArea & zero 'draw' signal connections in gui.py",
            len(drawing_area_nodes) == 0 and len(draw_connect_nodes) == 0,
            f"DrawingArea={len(drawing_area_nodes)}, draw_connect={len(draw_connect_nodes)}",
        )
        with open(os.path.realpath(__file__), "r", encoding="utf-8") as src_chk_fp:
            gui_src_low = src_chk_fp.read().lower()
        forbidden_wmi_tokens = (
            "acer_rth_" + "wmi",
            "og" + "ns",
            "np" + "cf",
            "nv" + "pcf",
            "dc" + "hu",
        )
        found_wmi = [tok for tok in forbidden_wmi_tokens if tok in gui_src_low]
        check(
            "Zero legacy WMI tokens in gui.py",
            len(found_wmi) == 0,
            str(found_wmi),
        )

        # 4. GTK3 window construction test when DISPLAY or WAYLAND_DISPLAY is present
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            stderr_capture = ""
            saved_stderr_fd = os.dup(2)
            err_tmp_path = os.path.join(tmp_cache, "gtk_stderr.log")
            try:
                with open(err_tmp_path, "w+b") as err_fp:
                    sys.stderr.flush()
                    os.dup2(err_fp.fileno(), 2)
                    try:
                        apply_css()
                        win = ControlCenterWindow(dry_runner, auto_refresh=False)

                        # Verify Tab 1 power profile tooltips/labels, collapsed RAPL expander, card titles & 3 GPU boost buttons
                        expected_tdp_tags = {
                            "quiet": "15W/30W OEM PL1/PL2",
                            "powersaving": "15W/30W OEM PL1/PL2",
                            "entertainment": "45W/115W OEM PL1/PL2",
                            "performance": "90W/115W OEM PL1/PL2",
                        }
                        def _collect_label_texts(w: Gtk.Widget) -> list[str]:
                            texts: list[str] = []
                            if isinstance(w, Gtk.Label):
                                texts.append(w.get_text())
                            if isinstance(w, Gtk.Container):
                                for ch in w.get_children():
                                    texts.extend(_collect_label_texts(ch))
                            return texts

                        tab1_labels = _collect_label_texts(win.notebook.get_nth_page(0))
                        expected_descs = {
                            "quiet": "EC Silent Auto Fan • 15W/30W OEM PL1/PL2 • 25W Quiet dGPU",
                            "powersaving": "EC Eco Auto Fan • 15W/30W OEM PL1/PL2 • dGPU Disabled (D3cold)",
                            "entertainment": "Balanced Fan • 45W/115W OEM PL1/PL2 • 60W dGPU",
                            "performance": "Auto Performance Fan • 90W/115W OEM PL1/PL2 • 75W dGPU",
                        }
                        power_tips_ok = (
                            len(POWER_PROFILES) == 4
                            and all(k != "turbo" for k, _t, _d in POWER_PROFILES)
                            and "turbo" not in POWER_PROFILE_TOOLTIPS
                            and set(win.power_profile_btns.keys()) == set(expected_tdp_tags.keys())
                            and all(
                                expected_descs[k] == win.power_profile_desc_labels[k].get_text()
                                and expected_descs[k] in (win.power_profile_btns[k].get_tooltip_text() or "")
                                for k in expected_descs
                            )
                            and all(desc in tab1_labels for _k, _title, desc in POWER_PROFILES)
                        )
                        check("Tab 1 power profile tooltips/labels updated (4 stock profiles, Quiet vs Power Saving dGPU)", power_tips_ok)

                        check(
                            "Tab 1 collapsed RAPL override expander & unchecked defaults",
                            not win.rapl_advanced_expander.get_expanded()
                            and win.rapl_advanced_expander.get_label()
                            == "Advanced: Optional OS RAPL Software Cap (--pl1 / --pl2)"
                            and not win.pl1_check.get_active()
                            and not win.pl2_check.get_active()
                            and win.pl1_spin.is_ancestor(win.rapl_advanced_expander)
                            and win.pl2_spin.is_ancestor(win.rapl_advanced_expander)
                            and win._build_power_args("performance") == ["performance"],
                            f"expanded={win.rapl_advanced_expander.get_expanded()}, args={win._build_power_args('performance')}",
                        )

                        check(
                            "Tab 1 Direct EC + RAPL telemetry card titles & zero WMI/GPU Boost controls",
                            "EC PL1T/PL2T HARDWARE PECI (0XF0..0XF4)" in tab1_labels
                            and "OEM TABLE (DIRECT EC + RAPL)" in tab1_labels
                            and "OS RAPL CEILING (MSR)" in tab1_labels
                            and "OS RAPL CEILING (MMIO)" in tab1_labels
                            and "DGPU POWER & RUNTIME PM (NVIDIA-SMI / PCI PM)" in tab1_labels
                            and not any(("NV" + "PCF") in lbl or "GPU Dynamic Boost" in lbl for lbl in tab1_labels)
                            and not hasattr(win, "gpu_boost_btns")
                            and not hasattr(win, "_on_gpu_boost_clicked")
                            and not hasattr(win, "_gpu_boost_active_key"),
                            str([t for t in tab1_labels if "PECI" in t or "RAPL" in t or "OEM" in t or "DGPU" in t]),
                        )

                        win._apply_power_status(na_gpu_power)
                        check(
                            "Tab 1 dGPU [N/A] & Direct EC OEM Table card",
                            "[N/A]" in win.card_gpu_power.get_text()
                            and "60.00 W" in win.card_gpu_power.get_text()
                            and "75.00 W" in win.card_gpu_power.get_text()
                            and "nvidia-powerd=active" in win.sub_gpu_power.get_text()
                            and "Quiet=15/30W" in win.card_oem_table.get_text()
                            and "35.000 W / 115.000 W" in win.sub_oem_table.get_text(),
                            f"val={win.card_gpu_power.get_text()!r}, oem={win.card_oem_table.get_text()!r}",
                        )

                        win._apply_power_status(powersaving_d3cold_power)
                        ps_d3cold_ok = (
                            win.power_profile_btns["powersaving"].get_style_context().has_class("active-choice")
                            and "Disabled / Suspended (D3cold / Power Saving mode)" in win.card_gpu_power.get_text()
                            and "runtime=suspended" in win.sub_gpu_power.get_text()
                        )
                        win._apply_power_status(powersaving_pending_power)
                        ps_pending_ok = (
                            "RTD3 Auto / D3cold Pending (Power Saving mode; display active)" in win.card_gpu_power.get_text()
                            and "power/control=auto" in win.sub_gpu_power.get_text()
                        )
                        win._apply_power_status(quiet_25w_power)
                        q_25w_ok = (
                            win.power_profile_btns["quiet"].get_style_context().has_class("active-choice")
                            and "25.00 W" in win.card_gpu_power.get_text()
                            and "Quiet 25W" in win.sub_gpu_power.get_text()
                        )
                        win._apply_power_status(bal_60w_power)
                        e_60w_ok = (
                            win.power_profile_btns["entertainment"].get_style_context().has_class("active-choice")
                            and "60.00 W" in win.card_gpu_power.get_text()
                            and "Entertainment 60W" in win.sub_gpu_power.get_text()
                        )
                        win._apply_power_status(live_power)
                        p_75w_ok = (
                            win.power_profile_btns["performance"].get_style_context().has_class("active-choice")
                            and "75.00 W" in win.card_gpu_power.get_text()
                            and "Performance 75W" in win.sub_gpu_power.get_text()
                            and "Quiet=15/30W" in win.card_oem_table.get_text()
                            and "Performance=90/115W" in win.card_oem_table.get_text()
                            and "90.000 W / 115.000 W" in win.sub_oem_table.get_text()
                        )
                        check(
                            "Tab 1 dGPU telemetry (D3cold Suspended, D3cold Pending, Quiet 25W, Entertainment 60W, Perf 75W)",
                            ps_d3cold_ok and ps_pending_ok and q_25w_ok and e_60w_ok and p_75w_ok,
                            f"d3={ps_d3cold_ok}, pend={ps_pending_ok}, q25={q_25w_ok}, e60={e_60w_ok}, p75={p_75w_ok}",
                        )
                        orig_exec_action = win._execute_action
                        # Populate 25 poll samples to verify rolling history caps at 20
                        for step in range(25):
                            sample = dict(live_fan)
                            sample["cpu_temp"] = 50 + (step % 25)
                            sample["dgpu_temp"] = 45 + (step % 18)
                            sample["cpu_fan_pct"] = 30 + (step % 40)
                            sample["gpu_fan_pct"] = 28 + (step % 35)
                            win._apply_fan_status(sample, sync_inputs=(step == 0))
                        check(
                            "Fan rolling telemetry trail capped at 20 samples",
                            len(win._cpu_telemetry_history) == 20 and len(win._gpu_telemetry_history) == 20,
                            f"cpu={len(win._cpu_telemetry_history)}, gpu={len(win._gpu_telemetry_history)}",
                        )
                        # Also exercise N/A fan status update cleanly
                        na_fan = parse_fan_status("[dry-run] preview")
                        win._apply_fan_status(na_fan, sync_inputs=False)
                        win._apply_fan_status(live_fan, sync_inputs=True)

                        # Verify dragging Fan Offset slider updates SVG preview ONLY (no fan.sh run)
                        script_calls: list[tuple[str, list[str]]] = []
                        orig_run = dry_runner.run_script

                        def spy_run(script: str, args: list[str]) -> ScriptResult:
                            script_calls.append((script, list(args)))
                            return orig_run(script, args)

                        dry_runner.run_script = spy_run  # type: ignore[method-assign]
                        pre_offset_pixels = win.fan_graph_image.get_pixbuf().get_pixels() if win.fan_graph_image.get_pixbuf() else b""
                        try:
                            win.fan_offset_scale.set_value(45)
                            while Gtk.events_pending():
                                Gtk.main_iteration_do(False)
                        finally:
                            dry_runner.run_script = orig_run  # type: ignore[method-assign]
                        post_offset_pixels = win.fan_graph_image.get_pixbuf().get_pixels() if win.fan_graph_image.get_pixbuf() else b""
                        check(
                            "Fan Offset slider previews SVG without invoking fan.sh",
                            win._preview_offset_pct == 45
                            and len(script_calls) == 0
                            and post_offset_pixels != pre_offset_pixels,
                            f"preview={win._preview_offset_pct}, calls={script_calls}",
                        )

                        win._apply_brightness_status(live_bri)
                        win._apply_rgb_status(live_rgb)
                        tab4_labels = _collect_label_texts(win.notebook.get_nth_page(3))
                        win._apply_battery_status(live_bat, sync_inputs=True)
                        bat_60_80_ok = (
                            "ACTIVE FLEXICHARGER (DIRECT EC 0XCB / XRAM 0X06D0..0X06D2)" in tab4_labels
                            and win.lbl_bat_flexi.get_text() == "Enabled (60%–80%)"
                            and "XRAM 0x06D0=0x1B" in win.sub_bat_flexi.get_text()
                            and win.bat_start_spin.get_value_as_int() == 60
                            and win.bat_stop_spin.get_value_as_int() == 80
                            and win.battery_preset_btns["60-80"].get_style_context().has_class("active-choice")
                            and not win.battery_preset_btns["70-85"].get_style_context().has_class("active-choice")
                            and not win.battery_preset_btns["full"].get_style_context().has_class("active-choice")
                        )
                        win._apply_battery_status(bal_bat, sync_inputs=True)
                        bat_70_85_ok = (
                            win.lbl_bat_flexi.get_text() == "Enabled (70%–85%)"
                            and "XRAM 0x06D0=0x1A" in win.sub_bat_flexi.get_text()
                            and win.bat_start_spin.get_value_as_int() == 70
                            and win.bat_stop_spin.get_value_as_int() == 85
                            and win.battery_preset_btns["70-85"].get_style_context().has_class("active-choice")
                            and not win.battery_preset_btns["60-80"].get_style_context().has_class("active-choice")
                        )
                        win._apply_battery_status(dis_bat, sync_inputs=True)
                        bat_dis_ok = (
                            win.lbl_bat_flexi.get_text() == "Disabled (100% Full Charge)"
                            and "XRAM 0x06D0=0x18" in win.sub_bat_flexi.get_text()
                            and win.battery_preset_btns["full"].get_style_context().has_class("active-choice")
                            and win.battery_preset_btns["disable"].get_style_context().has_class("active-choice")
                            and not win.battery_preset_btns["60-80"].get_style_context().has_class("active-choice")
                            and not win.battery_preset_btns["70-85"].get_style_context().has_class("active-choice")
                        )
                        win._apply_battery_status(ready_bat, sync_inputs=True)
                        bat_ready_ok = (
                            win.lbl_bat_flexi.get_text() == "Ready (Select 80% Limit or 100% Full)"
                            and "Direct EC 0xCB / XRAM 0x06D0..0x06D2" in win.sub_bat_flexi.get_text()
                            and win.battery_preset_btns["60-80"].get_sensitive() is True
                            and win.battery_preset_btns["full"].get_sensitive() is True
                            and win.battery_preset_btns["disable"].get_sensitive() is True
                            and all(
                                not btn.get_style_context().has_class("active-choice")
                                for btn in win.battery_preset_btns.values()
                            )
                        )
                        win._apply_battery_status(unavail_bat, sync_inputs=True)
                        bat_unavail_ok = (
                            win.lbl_bat_flexi.get_text() == "Unavailable (Unlock Live EC)"
                            and all(
                                not btn.get_style_context().has_class("active-choice")
                                for btn in win.battery_preset_btns.values()
                            )
                        )
                        check(
                            "Tab 4 Direct EC 0xCB / XRAM 0x06D0..0x06D2 FlexiCharger highlights, Ready 80%/100% sensitivity & spin sync",
                            bat_60_80_ok and bat_70_85_ok and bat_dis_ok and bat_ready_ok and bat_unavail_ok,
                            f"60-80={bat_60_80_ok}, 70-85={bat_70_85_ok}, dis={bat_dis_ok}, ready={bat_ready_ok}, unavail={bat_unavail_ok}",
                        )
                        win._apply_battery_status(live_bat, sync_inputs=True)

                        # Tab 2: Mode layout (3 stock + Reset Factory, no Perf-Auto, no expander), 0x08->Auto, Quiet/Power Saving lock
                        required_mask = (
                            Gdk.EventMask.BUTTON_PRESS_MASK
                            | Gdk.EventMask.BUTTON1_MOTION_MASK
                            | Gdk.EventMask.BUTTON_RELEASE_MASK
                            | Gdk.EventMask.POINTER_MOTION_MASK
                        )
                        eb_children = win.fan_graph_eventbox.get_children()
                        check(
                            "Tab 2 EventBox wraps only 640x260 Gtk.Image with centered alignment & event masks",
                            isinstance(win.fan_graph_eventbox, Gtk.EventBox)
                            and win.fan_graph_eventbox.get_halign() == Gtk.Align.CENTER
                            and win.fan_graph_eventbox.get_valign() == Gtk.Align.CENTER
                            and eb_children == [win.fan_graph_image]
                            and (win.fan_graph_eventbox.get_events() & required_mask) == required_mask,
                            f"children={eb_children}, mask={win.fan_graph_eventbox.get_events()}",
                        )
                        check(
                            "Tab 2 has ONLY 3 stock modes + Reset Factory (no Perf-Auto, no advanced expander)",
                            not hasattr(win, "fan_advanced_expander")
                            and not hasattr(win, "fan_adv_grid")
                            and [k for k, _lbl, _tip in FAN_PRIMARY_MODES] == ["auto", "max", "custom"]
                            and set(win.fan_mode_btns.keys()) == {"auto", "max", "custom", "reset"}
                            and "perf-auto" not in win.fan_mode_btns
                            and set(win.fan_primary_grid.get_children()) == set(win.fan_mode_btns.values()),
                            str(sorted(win.fan_mode_btns.keys())),
                        )
                        win._apply_fan_status(
                            parse_fan_status("  Fan Mode  (0xD7) : Auto (0x08, Performance Profile)\n"),
                            sync_inputs=True,
                        )
                        auto_08_ok = (
                            win.fan_mode_btns["auto"].get_style_context().has_class("active-choice")
                            and not win.fan_mode_btns["max"].get_style_context().has_class("active-choice")
                            and not win.fan_mode_btns["custom"].get_style_context().has_class("active-choice")
                            and "0x08" in win.lbl_fan_mode.get_text()
                        )
                        win._apply_fan_status(fan_d3cold_susp, sync_inputs=True)
                        d3cold_fan_ok = (
                            win.lbl_nvidia_gpu.get_text() == "NVIDIA dGPU\nSuspended (D3cold) • 0 W"
                            and win.fan_mode_btns["auto"].get_style_context().has_class("active-choice")
                        )
                        check(
                            "Tab 2 highlights Auto for 0x08 & 0x02 and handles D3cold GPU temp cleanly",
                            auto_08_ok and d3cold_fan_ok,
                            f"lbl={win.lbl_fan_mode.get_text()!r}, nv={win.lbl_nvidia_gpu.get_text()!r}",
                        )

                        # Quiet & Power Saving keep custom curve section visible (read-only), lock Tab 2 controls,
                        # clear highlights, show banner, update graph note, and block drag; Entertainment & Performance unlock
                        fan_edit_controls = (
                            *win.fan_mode_btns.values(),
                            win.fan_offset_scale,
                            win.fan_offset_spin,
                            win.apply_fan_offset_btn,
                            win.cpu_t2_spin,
                            win.cpu_d2_spin,
                            win.cpu_t3_spin,
                            win.cpu_d3_spin,
                            win.gpu_t2_spin,
                            win.gpu_d2_spin,
                            win.gpu_t3_spin,
                            win.gpu_d3_spin,
                            win.copy_cpu_to_gpu_btn,
                            win.reset_curve_nodes_btn,
                            win.apply_custom_curve_btn,
                            win.fan_graph_eventbox,
                        )
                        expected_lock_text = (
                            "Quiet / Power Saving Profile Active — Showing read-only EC auto fan curve "
                            "(0x038C/0x039A; switch to Entertainment or Performance to edit Fan Modes & Custom Curve)"
                        )
                        initial_curve_vis_ok = win.fan_curve_sec.get_visible() is True
                        win._apply_power_status(quiet_25w_power)
                        win._apply_fan_status(fan_silent_auto, sync_inputs=False)
                        fake_ev = type("FakeEv", (), {"button": 1, "x": float(fan_curve_point_to_px(57, 56)[0]), "y": float(fan_curve_point_to_px(57, 56)[1])})()
                        drag_blocked_q = (
                            win._on_fan_graph_button_press(win.fan_graph_eventbox, fake_ev) is False
                            and win._dragging_curve_node is None
                        )
                        q_locked_ok = (
                            initial_curve_vis_ok
                            and win._active_power_profile == "quiet"
                            and win.fan_lock_banner_box.get_visible() is True
                            and win.fan_curve_sec.get_visible() is True
                            and win.lbl_fan_mode.get_text() == "Silent-Auto (0x80, Quiet Profile)"
                            and win.fan_lock_banner_label.get_text() == expected_lock_text
                            and win.lbl_fan_graph_note.get_text() == expected_lock_text
                            and all(not c.get_sensitive() for c in fan_edit_controls)
                            and all(
                                not b.get_style_context().has_class("active-choice")
                                for b in win.fan_mode_btns.values()
                            )
                            and drag_blocked_q
                        )
                        win._apply_power_status(powersaving_d3cold_power)
                        win._apply_fan_status(fan_eco_auto, sync_inputs=False)
                        ps_locked_ok = (
                            win._active_power_profile == "powersaving"
                            and win.fan_lock_banner_box.get_visible() is True
                            and win.fan_curve_sec.get_visible() is True
                            and win.lbl_fan_mode.get_text() == "Auto (0x02, Power Saving Eco Policy)"
                            and win.lbl_fan_graph_note.get_text() == expected_lock_text
                            and all(not c.get_sensitive() for c in fan_edit_controls)
                            and all(
                                not b.get_style_context().has_class("active-choice")
                                for b in win.fan_mode_btns.values()
                            )
                        )
                        win._apply_power_status(bal_60w_power)
                        win._apply_fan_status(live_fan, sync_inputs=False)
                        ent_unlocked_ok = (
                            win._active_power_profile == "entertainment"
                            and win.fan_lock_banner_box.get_visible() is False
                            and win.fan_curve_sec.get_visible() is True
                            and win.lbl_fan_graph_note.get_text() == win._FAN_GRAPH_NOTE_UNLOCKED
                            and all(c.get_sensitive() for c in fan_edit_controls)
                            and win.fan_mode_btns["auto"].get_style_context().has_class("active-choice")
                        )
                        win._apply_power_status(live_power)
                        perf_unlocked_ok = (
                            win._active_power_profile == "performance"
                            and win.fan_lock_banner_box.get_visible() is False
                            and win.fan_curve_sec.get_visible() is True
                            and win.lbl_fan_graph_note.get_text() == win._FAN_GRAPH_NOTE_UNLOCKED
                            and all(c.get_sensitive() for c in fan_edit_controls)
                            and win.fan_mode_btns["auto"].get_style_context().has_class("active-choice")
                        )
                        check(
                            "Tab 2 keeps curve visible (read-only), locks controls/clears highlights/blocks drag in Quiet & Power Saving",
                            q_locked_ok and ps_locked_ok and ent_unlocked_ok and perf_unlocked_ok,
                            f"q={q_locked_ok}, ps={ps_locked_ok}, ent={ent_unlocked_ok}, perf={perf_unlocked_ok}",
                        )

                        # Drag CPU Node 2 and verify spinbuttons + _curve_dirty + poll immunity
                        target_px, target_py = fan_curve_point_to_px(57, 56)
                        win._apply_drag_to_curve_node("cpu", 1, target_px, target_py)
                        check(
                            "Tab 2 graph drag updates _cpu_curve, syncs spins, sets _curve_dirty",
                            win._curve_dirty is True
                            and win._cpu_curve[1] == (57, 56)
                            and win.cpu_t2_spin.get_value_as_int() == 57
                            and win.cpu_d2_spin.get_value_as_int() == 56,
                            f"curve={win._cpu_curve}, dirty={win._curve_dirty}",
                        )
                        win._apply_fan_status(live_fan, sync_inputs=False)
                        check(
                            "Tab 2 _curve_dirty protects unapplied edits during 2s Auto-Poll",
                            win._curve_dirty is True and win._cpu_curve[1] == (57, 56),
                            f"curve={win._cpu_curve}, dirty={win._curve_dirty}",
                        )

                        # Spinbutton value-changed clamping & Copy CPU -> GPU & Reset Default Nodes
                        win.cpu_t2_spin.set_value(92)
                        while Gtk.events_pending():
                            Gtk.main_iteration_do(False)
                        check(
                            "Tab 2 spinbutton neighbor clamping keeps T2 <= T3-1",
                            win.cpu_t2_spin.get_value_as_int() == win._cpu_curve[2][0] - 1
                            and win._cpu_curve[1][0] < win._cpu_curve[2][0],
                            f"cpu_curve={win._cpu_curve}",
                        )
                        win.copy_cpu_to_gpu_btn.clicked()
                        check(
                            "Tab 2 Copy CPU -> GPU copies CPU nodes to GPU",
                            win._gpu_curve == win._cpu_curve and win._curve_dirty is True,
                            f"cpu={win._cpu_curve}, gpu={win._gpu_curve}",
                        )
                        win.reset_curve_nodes_btn.clicked()
                        check(
                            "Tab 2 Reset Default Nodes restores default 40/50..100/100 & clears _curve_dirty",
                            win._cpu_curve == list(CUSTOM_4PT_WAYPOINTS)
                            and win._gpu_curve == list(CUSTOM_4PT_WAYPOINTS)
                            and win._curve_dirty is False,
                            f"cpu={win._cpu_curve}, gpu={win._gpu_curve}, dirty={win._curve_dirty}",
                        )

                        # Apply Custom CPU & GPU Curve dispatch
                        win.cpu_t2_spin.set_value(54)
                        win.cpu_d2_spin.set_value(53)
                        win.gpu_t3_spin.set_value(84)
                        win.gpu_d3_spin.set_value(85)
                        while Gtk.events_pending():
                            Gtk.main_iteration_do(False)
                        fan_action_calls: list[tuple[str, list[str], bool]] = []

                        def spy_fan_exec(script: str, args: list[str], refresh_after: bool = False) -> None:
                            fan_action_calls.append((script, list(args), refresh_after))

                        win._execute_action = spy_fan_exec  # type: ignore[method-assign]
                        try:
                            win.apply_custom_curve_btn.clicked()
                        finally:
                            win._execute_action = orig_exec_action  # type: ignore[method-assign]
                        check(
                            "Tab 2 Apply Custom CPU & GPU Curve clears _curve_dirty & dispatches 8 args",
                            win._curve_dirty is False
                            and fan_action_calls == [
                                ("fan.sh", ["custom", "54", "53", "80", "70", "60", "60", "84", "85"], True)
                            ],
                            f"dirty={win._curve_dirty}, calls={fan_action_calls}",
                        )

                        # Manual refresh sync clears _curve_dirty
                        win.cpu_t2_spin.set_value(58)
                        win._apply_fan_status(live_fan, sync_inputs=True)
                        check(
                            "Tab 2 manual refresh (sync_inputs=True) clears _curve_dirty & syncs EC curves",
                            win._curve_dirty is False and win._cpu_curve == live_fan.cpu_curve,
                            f"dirty={win._curve_dirty}, cpu_curve={win._cpu_curve}",
                        )

                        # Tab 5: Peripherals & Lid widget updates + action dispatch
                        check(
                            "5-Tab Notebook includes Peripherals & Lid",
                            win.notebook.get_n_pages() == 5
                            and win.notebook.get_tab_label_text(win.notebook.get_nth_page(4)) == "Peripherals & Lid",
                            f"pages={win.notebook.get_n_pages()}",
                        )
                        win._apply_peripherals_status(periph_live_open)
                        check(
                            "Tab 5 _apply_peripherals_status highlights open/suspend/ON states",
                            win.lbl_webcam_state.get_text() == "ON"
                            and win.webcam_btns["on"].get_style_context().has_class("active-choice")
                            and win.lbl_lid_state.get_text() == "Open"
                            and win.lbl_lid_state.get_style_context().has_class("lid-badge-open")
                            and win.lid_close_btns["suspend"].get_style_context().has_class("active-choice")
                            and win.fn_lock_btns["on"].get_style_context().has_class("active-choice"),
                        )
                        win._apply_peripherals_status(periph_live_closed)
                        check(
                            "Tab 5 _apply_peripherals_status highlights closed/clamshell/OFF states",
                            win.lbl_webcam_state.get_text() == "OFF"
                            and win.webcam_btns["off"].get_style_context().has_class("active-choice")
                            and win.trackpad_btns["off"].get_style_context().has_class("active-choice")
                            and win.lbl_lid_state.get_text() == "Closed"
                            and win.lbl_lid_state.get_style_context().has_class("lid-badge-closed")
                            and win.lid_wake_btns["off"].get_style_context().has_class("active-choice")
                            and win.lid_close_btns["ignore"].get_style_context().has_class("active-choice")
                            and win.win_lock_btns["on"].get_style_context().has_class("active-choice")
                            and win.fn_swap_btns["on"].get_style_context().has_class("active-choice")
                            and win.fn_lock_btns["off"].get_style_context().has_class("active-choice"),
                        )

                        periph_calls: list[tuple[str, list[str], bool]] = []

                        def spy_periph_exec(script: str, args: list[str], refresh_after: bool = False) -> None:
                            periph_calls.append((script, list(args), refresh_after))

                        win._execute_action = spy_periph_exec  # type: ignore[method-assign]
                        try:
                            for btn_map in (
                                win.webcam_btns,
                                win.trackpad_btns,
                                win.lid_wake_btns,
                                win.lid_close_btns,
                                win.win_lock_btns,
                                win.fn_swap_btns,
                                win.fn_lock_btns,
                            ):
                                for b in btn_map.values():
                                    b.clicked()
                        finally:
                            win._execute_action = orig_exec_action  # type: ignore[method-assign]
                        expected_periph_calls = [
                            ("peripherals.sh", ["webcam", "on"], True),
                            ("peripherals.sh", ["webcam", "off"], True),
                            ("peripherals.sh", ["touchpad", "on"], True),
                            ("peripherals.sh", ["touchpad", "off"], True),
                            ("peripherals.sh", ["lid-wake", "on"], True),
                            ("peripherals.sh", ["lid-wake", "off"], True),
                            ("peripherals.sh", ["lid-close", "suspend"], True),
                            ("peripherals.sh", ["lid-close", "ignore"], True),
                            ("peripherals.sh", ["win-lock", "on"], True),
                            ("peripherals.sh", ["win-lock", "off"], True),
                            ("peripherals.sh", ["fn-swap", "on"], True),
                            ("peripherals.sh", ["fn-swap", "off"], True),
                            ("peripherals.sh", ["fn-lock", "on"], True),
                            ("peripherals.sh", ["fn-lock", "off"], True),
                        ]
                        check(
                            "Tab 5 peripheral buttons dispatch exact commands with refresh_after=True",
                            periph_calls == expected_periph_calls,
                            str(periph_calls),
                        )

                        # Verify refresh_all(from_poll=True) skips peripherals.sh while from_poll=False includes it
                        def run_refresh_and_collect(from_poll_flag: bool) -> list[str]:
                            called_scripts: list[str] = []

                            def spy_poll_run(script: str, args: list[str]) -> ScriptResult:
                                called_scripts.append(script)
                                return orig_run(script, args)

                            dry_runner.run_script = spy_poll_run  # type: ignore[method-assign]
                            try:
                                win.refresh_all(quiet_log=True, from_poll=from_poll_flag)
                                deadline = time.monotonic() + 5.0
                                while time.monotonic() < deadline:
                                    while Gtk.events_pending():
                                        Gtk.main_iteration_do(False)
                                    with win._refresh_lock:
                                        in_flight = win._refresh_in_flight
                                    if not in_flight and not Gtk.events_pending():
                                        break
                                    time.sleep(0.01)
                                while Gtk.events_pending():
                                    Gtk.main_iteration_do(False)
                            finally:
                                dry_runner.run_script = orig_run  # type: ignore[method-assign]
                            return called_scripts

                        poll_scripts = run_refresh_and_collect(True)
                        manual_scripts = run_refresh_and_collect(False)
                        check(
                            "refresh_all skips peripherals.sh on 2s poll and includes it on manual refresh",
                            "peripherals.sh" not in poll_scripts and "peripherals.sh" in manual_scripts,
                            f"poll={poll_scripts}, manual={manual_scripts}",
                        )

                        win.show_all()
                        for page_idx in range(win.notebook.get_n_pages()):
                            win.notebook.set_current_page(page_idx)
                            while Gtk.events_pending():
                                Gtk.main_iteration_do(False)

                        # Verify show_all() keeps fan_curve_sec visible across Quiet/PowerSaving/Entertainment/Performance
                        win._apply_power_status(quiet_25w_power)
                        win.show_all()
                        show_all_q_ok = (
                            win.fan_curve_sec.get_visible() is True
                            and win.fan_lock_banner_box.get_visible() is True
                        )
                        win._apply_power_status(powersaving_d3cold_power)
                        win.show_all()
                        show_all_ps_ok = (
                            win.fan_curve_sec.get_visible() is True
                            and win.fan_lock_banner_box.get_visible() is True
                        )
                        win._apply_power_status(bal_60w_power)
                        win.show_all()
                        show_all_ent_ok = (
                            win.fan_curve_sec.get_visible() is True
                            and win.fan_lock_banner_box.get_visible() is False
                        )
                        win._apply_power_status(live_power)
                        win.show_all()
                        show_all_perf_ok = (
                            win.fan_curve_sec.get_visible() is True
                            and win.fan_lock_banner_box.get_visible() is False
                        )
                        check(
                            "Tab 2 fan_curve_sec stays visible across show_all() in Quiet/Power Saving/Entertainment/Performance",
                            show_all_q_ok and show_all_ps_ok and show_all_ent_ok and show_all_perf_ok,
                            f"q={show_all_q_ok}, ps={show_all_ps_ok}, ent={show_all_ent_ok}, perf={show_all_perf_ok}",
                        )

                        graph_pb = win.fan_graph_image.get_pixbuf()
                        check(
                            "Tab 2 Fan Curve Gtk.Image pixbuf rendered (640x260)",
                            graph_pb is not None
                            and graph_pb.get_width() == 640
                            and graph_pb.get_height() == 260
                            and len(set(graph_pb.get_pixels()[:4096])) > 4,
                        )

                        def collect_widgets(widget: Gtk.Widget) -> list[Gtk.Widget]:
                            items = [widget]
                            if isinstance(widget, Gtk.Container):
                                for child in widget.get_children():
                                    items.extend(collect_widgets(child))
                            return items

                        all_widgets = collect_widgets(win)
                        rgb_tab_widget = win.notebook.get_nth_page(2)
                        rgb_widgets = collect_widgets(rgb_tab_widget) if rgb_tab_widget is not None else []
                        tab3_labels = _collect_label_texts(rgb_tab_widget) if rgb_tab_widget is not None else []
                        tab3_tooltips = [w.get_tooltip_text() or "" for w in rgb_widgets]
                        tab3_combos = [
                            w for w in rgb_widgets if isinstance(w, (Gtk.ComboBox, Gtk.ComboBoxText))
                        ]
                        forbidden_3zone = (
                            "Zone 3",
                            "Zone 4",
                            "Zone 5",
                            "0xC4",
                            "3-Zone",
                            "Breath 3-Zone",
                            "Cycle",
                            "Wave",
                            "Flash",
                            "Random",
                            "Dance",
                            "Tempo",
                        )
                        has_forbidden_tab3 = any(
                            token in text
                            for token in forbidden_3zone
                            for text in (*tab3_labels, *tab3_tooltips)
                        )
                        check(
                            "Tab 3 has no Zone selector and no 0xC4 / 3-zone effect buttons",
                            (not hasattr(win, "zone_combo"))
                            and len(tab3_combos) == 0
                            and set(win.rgb_effect_btns.keys()) == {"breath"}
                            and win.rgb_effect_btns["breath"].get_label() == "Breathing Effect (0xCA Sub 0x0F)"
                            and not has_forbidden_tab3,
                            f"combos={len(tab3_combos)}, fx={list(win.rgb_effect_btns.keys())}",
                        )

                        win._apply_brightness_status(live_bri)
                        win._apply_rgb_status(fx_rgb)
                        breath_active = win.rgb_effect_btns["breath"].get_style_context().has_class("active-choice")
                        win._apply_rgb_status(live_rgb)
                        check(
                            "Tab 3 _apply_brightness_status & _apply_rgb_status update cards/inputs",
                            win.lbl_bri_state.get_text() == "brightness=143 (0x8F)"
                            and "0xCA sub 0x06" in win.sub_bri_mode.get_text()
                            and "White KB" not in win.sub_bri_mode.get_text()
                            and win.bri_spin.get_value_as_int() == 143
                            and win.lbl_rgb_state.get_text() == "color=(0,255,200) ec=(0,127,100) cc_scale=1"
                            and "KBType=6 1-zone (0xF9=0x03)" in win.sub_rgb_mailbox.get_text()
                            and (win.r_spin.get_value_as_int(), win.g_spin.get_value_as_int(), win.b_spin.get_value_as_int())
                            == (0, 255, 200)
                            and win.cc_scale_check.get_active() is True
                            and breath_active
                            and not win.rgb_effect_btns["breath"].get_style_context().has_class("active-choice"),
                            f"bri={win.sub_bri_mode.get_text()!r}, rgb={win.sub_rgb_mailbox.get_text()!r}",
                        )

                        rgb_action_calls: list[tuple[str, list[str], bool]] = []

                        def spy_rgb_exec(script: str, args: list[str], refresh_after: bool = False) -> None:
                            rgb_action_calls.append((script, list(args), refresh_after))

                        win._execute_action = spy_rgb_exec  # type: ignore[method-assign]
                        try:
                            win.r_spin.set_value(255)
                            win.g_spin.set_value(0)
                            win.b_spin.set_value(128)
                            win.cc_scale_check.set_active(False)
                            win.apply_rgb_btn.clicked()
                            win.cc_scale_check.set_active(True)
                            win.apply_rgb_btn.clicked()
                            win.cc_scale_check.set_active(False)
                            win.rgb_preset_btns["cyan"].clicked()
                            win.rgb_effect_btns["breath"].clicked()
                            win.timeout_spin.set_value(30)
                            win.set_timeout_btn.clicked()
                            win.disable_timeout_btn.clicked()
                        finally:
                            win._execute_action = orig_exec_action  # type: ignore[method-assign]
                        check(
                            "Tab 3 dispatches KBType=6 1-zone RGB/effect/timeout args with no zone",
                            rgb_action_calls == [
                                ("rgb.sh", ["255", "0", "128"], True),
                                ("rgb.sh", ["--cc-scale", "255", "0", "128"], True),
                                ("rgb.sh", ["cyan"], True),
                                ("rgb.sh", ["--effect", "breath"], True),
                                ("rgb.sh", ["--timeout", "30"], True),
                                ("rgb.sh", ["--timeout", "off"], True),
                            ],
                            str(rgb_action_calls),
                        )

                        all_images = [w for w in all_widgets if isinstance(w, Gtk.Image)]
                        rgb_images = [w for w in rgb_widgets if isinstance(w, Gtk.Image) and w.get_pixbuf() is not None]
                        drawing_widgets = [w for w in all_widgets if "DrawingArea" in type(w).__name__ or "DrawingArea" in w.get_name()]
                        check(
                            "RGB tab uses Gtk.Image swatches (zero Gtk.DrawingArea)",
                            len(all_images) >= 12 and len(rgb_images) >= 11 and len(drawing_widgets) == 0,
                            f"images={len(all_images)}, rgb_images={len(rgb_images)}, drawing_widgets={len(drawing_widgets)}",
                        )

                        win._destroyed = True
                        win.destroy()
                        while Gtk.events_pending():
                            Gtk.main_iteration_do(False)
                        check("GTK3 window instantiation & widget update", True)

                        # Verify HeaderBar displays "Daemon Connected (Live EC)" when daemon socket is active
                        win_sock = os.path.join(tmp_cache, "gui_win_daemon.sock")
                        win_daemon_srv = daemon_mod.DaemonServer(
                            socket_path=win_sock,
                            uid=os.getuid(),
                            gid=os.getgid(),
                            cache_dir=tmp_cache,
                        )
                        win_daemon_srv.start_in_thread()
                        try:
                            os.environ["ACER_CONTROL_SOCK"] = win_sock
                            daemon_win = ControlCenterWindow(
                                ScriptRunner(dry_run=False, cache_dir=tmp_cache),
                                auto_refresh=False,
                            )
                            check(
                                "HeaderBar displays 'Daemon Connected (Live EC)' when daemon active",
                                daemon_win.unlock_btn.get_label() == "Daemon Connected (Live EC)"
                                and daemon_win.unlock_btn.get_active() is True
                                and daemon_win.unlock_btn.get_style_context().has_class("unlocked")
                                and daemon_win._unlock_btn_daemon_mode is True
                                and "Daemon Connected (Live EC" in daemon_win.mode_banner_label.get_text()
                                and daemon_win.apply_config_btn.get_label() == "Apply Config"
                                and daemon_win.runner.config_path in (daemon_win.apply_config_btn.get_tooltip_text() or "")
                                and daemon_win.runner.config_path in (daemon_win.auto_apply_switch.get_tooltip_text() or ""),
                                f"btn={daemon_win.unlock_btn.get_label()!r}, banner={daemon_win.mode_banner_label.get_text()!r}",
                            )
                            daemon_win._destroyed = True
                            daemon_win.destroy()
                            while Gtk.events_pending():
                                Gtk.main_iteration_do(False)
                        finally:
                            os.environ["ACER_CONTROL_SOCK"] = ""
                            win_daemon_srv.shutdown()

                        # Verify HeaderBar Apply Config / Auto-apply, launch auto-apply, --no-auto-apply & _execute_action recording
                        def _pump_gtk_until(pred: Any, timeout: float = 5.0) -> bool:
                            deadline = time.monotonic() + timeout
                            while time.monotonic() < deadline:
                                while Gtk.events_pending():
                                    Gtk.main_iteration_do(False)
                                if pred():
                                    return True
                                time.sleep(0.02)
                            while Gtk.events_pending():
                                Gtk.main_iteration_do(False)
                            return bool(pred())

                        win_cfg_path = os.path.join(tmp_cache, "win_isolated_config.json")
                        win_cfg_data = config_store.default_config()
                        win_cfg_data["power"]["profile"] = "entertainment"
                        win_cfg_data["keyboard"]["brightness"] = 143
                        win_cfg_data["battery"] = {"mode": "limit", "start_pct": 60, "stop_pct": 80}
                        config_store.save_config(win_cfg_path, win_cfg_data, uid=os.getuid(), gid=os.getgid())

                        gtk_cfg_sock = os.path.join(tmp_cache, "gui_gtk_cfg.sock")
                        gtk_cfg_srv = daemon_mod.DaemonServer(
                            socket_path=gtk_cfg_sock,
                            uid=os.getuid(),
                            gid=os.getgid(),
                            cache_dir=tmp_cache,
                            config_path=win_cfg_path,
                        )
                        gtk_set_calls: list[tuple[str, list[str]]] = []

                        def _mock_gtk_exec(script: str, act_args: list[str]) -> tuple[int, str, str]:
                            if act_args != ["status"]:
                                gtk_set_calls.append((script, list(act_args)))
                            return (0, f"[mock] {script} {' '.join(act_args)}", "")

                        gtk_cfg_srv._execute_script = _mock_gtk_exec  # type: ignore[method-assign]
                        gtk_cfg_srv.start_in_thread()
                        try:
                            with _temp_env("ACER_CONTROL_SOCK", gtk_cfg_sock):
                                # (a) Launch auto-apply enabled
                                launch_win = ControlCenterWindow(
                                    ScriptRunner(dry_run=False, cache_dir=tmp_cache, config_path=win_cfg_path),
                                    auto_refresh=True,
                                    no_auto_apply=False,
                                )
                                launch_ok = _pump_gtk_until(
                                    lambda: launch_win._last_apply_config_result is not None
                                    and not launch_win._refresh_in_flight,
                                    timeout=5.0,
                                )
                                launch_win.wait_background_tasks(timeout=3.0)
                                log_txt = launch_win.log_buf.get_text(
                                    launch_win.log_buf.get_start_iter(),
                                    launch_win.log_buf.get_end_iter(),
                                    True,
                                )
                                check(
                                    "ControlCenterWindow launch auto-apply runs apply_saved_config, logs summary & refreshes",
                                    launch_ok
                                    and launch_win._last_apply_config_result is not None
                                    and launch_win._last_apply_config_result.mode == "daemon-apply"
                                    and "Startup auto-apply" in log_txt
                                    and gtk_set_calls
                                    == [
                                        ("power_profile.sh", ["entertainment"]),
                                        ("brightness.sh", ["143"]),
                                        ("battery.sh", ["limit", "60", "80"]),
                                    ],
                                    f"ok={launch_ok}, calls={gtk_set_calls}",
                                )

                                # (b) HeaderBar Auto-apply switch + Apply Config button + _execute_action recording
                                gtk_set_calls.clear()
                                launch_win.auto_apply_switch.set_active(False)
                                sw_ok = _pump_gtk_until(
                                    lambda: config_store.load_config(win_cfg_path)["auto_apply_on_launch"] is False,
                                    timeout=4.0,
                                )
                                launch_win.wait_background_tasks(timeout=3.0)

                                launch_win._last_apply_config_result = None
                                launch_win.apply_config_btn.clicked()
                                btn_ok = _pump_gtk_until(
                                    lambda: launch_win._last_apply_config_result is not None
                                    and not launch_win._refresh_in_flight,
                                    timeout=5.0,
                                )
                                launch_win.wait_background_tasks(timeout=3.0)

                                launch_win.auto_apply_switch.set_active(True)
                                _pump_gtk_until(
                                    lambda: config_store.load_config(win_cfg_path)["auto_apply_on_launch"] is True,
                                    timeout=4.0,
                                )
                                launch_win.wait_background_tasks(timeout=3.0)

                                launch_win._execute_action("power_profile.sh", ["quiet"], refresh_after=False)
                                rec_ok = _pump_gtk_until(
                                    lambda: config_store.load_config(win_cfg_path)["power"]["profile"] == "quiet",
                                    timeout=4.0,
                                )

                                # Dry-run _execute_action must NOT overwrite config.json
                                launch_win.runner.dry_run = True
                                launch_win._execute_action("power_profile.sh", ["performance"], refresh_after=False)
                                _pump_gtk_until(
                                    lambda: "power_profile.sh" in launch_win.log_buf.get_text(
                                        launch_win.log_buf.get_start_iter(),
                                        launch_win.log_buf.get_end_iter(),
                                        True,
                                    ),
                                    timeout=2.0,
                                )
                                time.sleep(0.15)
                                while Gtk.events_pending():
                                    Gtk.main_iteration_do(False)
                                still_quiet = config_store.load_config(win_cfg_path)["power"]["profile"] == "quiet"

                                check(
                                    "HeaderBar Auto-apply switch, Apply Config button & _execute_action recording work",
                                    sw_ok
                                    and btn_ok
                                    and len(gtk_set_calls) == 4
                                    and rec_ok
                                    and still_quiet,
                                    f"sw={sw_ok}, btn={btn_ok}, calls={gtk_set_calls}, rec={rec_ok}, quiet={still_quiet}",
                                )
                                launch_win._destroyed = True
                                launch_win.destroy()
                                while Gtk.events_pending():
                                    Gtk.main_iteration_do(False)

                                # (c) --no-auto-apply skips startup auto-apply while still refreshing
                                gtk_set_calls.clear()
                                noauto_win = ControlCenterWindow(
                                    ScriptRunner(dry_run=False, cache_dir=tmp_cache, config_path=win_cfg_path),
                                    auto_refresh=True,
                                    no_auto_apply=True,
                                )
                                noauto_done = _pump_gtk_until(
                                    lambda: "Launch auto-apply skipped (--no-auto-apply)."
                                    in noauto_win.log_buf.get_text(
                                        noauto_win.log_buf.get_start_iter(),
                                        noauto_win.log_buf.get_end_iter(),
                                        True,
                                    )
                                    and not noauto_win._refresh_in_flight,
                                    timeout=4.0,
                                )
                                noauto_win.wait_background_tasks(timeout=3.0)
                                check(
                                    "ControlCenterWindow --no-auto-apply skips startup auto-apply",
                                    noauto_done and len(gtk_set_calls) == 0,
                                    f"done={noauto_done}, calls={gtk_set_calls}",
                                )
                                noauto_win._destroyed = True
                                noauto_win.destroy()
                                while Gtk.events_pending():
                                    Gtk.main_iteration_do(False)
                        finally:
                            gtk_cfg_srv._execute_script = orig_cfg_exec  # type: ignore[method-assign]
                            gtk_cfg_srv.shutdown()
                    finally:
                        sys.stderr.flush()
                        os.dup2(saved_stderr_fd, 2)
                with open(err_tmp_path, "r", encoding="utf-8", errors="replace") as err_read:
                    stderr_capture = err_read.read()
                if stderr_capture.strip():
                    sys.stderr.write(stderr_capture)
                check(
                    "Zero TypeError / cairo.Context warnings on GTK stderr",
                    ("TypeError" not in stderr_capture) and ("cairo.Context" not in stderr_capture),
                    stderr_capture.strip()[:200],
                )
            except Exception as exc:  # noqa: BLE001
                check("GTK3 window instantiation & widget update", False, str(exc))
            finally:
                os.close(saved_stderr_fd)
        else:
            print("[SKIP] No DISPLAY or WAYLAND_DISPLAY set; skipping GTK3 window instantiation.")

    print(f"Self-test finished: {len(failures)} failure(s).")
    return 1 if failures else 0


def parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Acer Aspire A715-59G GTK3 Control Center")
    parser.add_argument("--dry-run", action="store_true", help="Start with Dry-Run mode enabled")
    parser.add_argument("--ec-file", default=None, help="Explicit EC snapshot file for status fallback")
    parser.add_argument("--config", default=None, help="Explicit config.json path")
    parser.add_argument("--no-auto-apply", action="store_true", help="Skip auto-applying saved config on launch")
    parser.add_argument("--self-test", action="store_true", help="Run headless-safe self-tests and exit")
    parser.add_argument("--worker", action="store_true", help="Run privileged JSON stdin/stdout worker loop")
    parser.add_argument("--cache-dir", default=None, help="XDG_CACHE_HOME override for --worker")
    parser.add_argument("--uid", type=int, default=None, help="Target UID for cache chown in --worker")
    parser.add_argument("--gid", type=int, default=None, help="Target GID for cache chown in --worker")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_cli(sys.argv[1:] if argv is None else argv)

    if args.worker:
        return run_worker(args.cache_dir, args.uid, args.gid)

    if args.self_test:
        return run_self_test()

    apply_css()
    runner = ScriptRunner(
        dry_run=args.dry_run,
        explicit_ec_file=args.ec_file,
        config_path=args.config,
    )
    win = ControlCenterWindow(runner, auto_refresh=True, no_auto_apply=args.no_auto_apply)
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
