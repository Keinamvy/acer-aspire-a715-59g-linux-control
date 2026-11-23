#!/usr/bin/env python3
"""Persistent configuration store and action planner for Acer Aspire A715-59G Control."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import sys
import tempfile
from typing import Any, Iterator, Mapping, Optional, Sequence

CONFIG_DIR_NAME = "acer-a715-59g-control"
CONFIG_FILE_NAME = "config.json"
LEGACY_CACHE_SUBDIR = "acer-a715-59g"
CONFIG_VERSION = 1

DEFAULT_MIDDLE_CURVE: list[int] = [60, 60, 80, 70]

POWER_PROFILE_ALIASES: dict[str, str] = {
    "quiet": "quiet",
    "silent": "quiet",
    "powersaving": "powersaving",
    "power-saving": "powersaving",
    "power_saving": "powersaving",
    "saving": "powersaving",
    "saver": "powersaving",
    "eco": "powersaving",
    "entertainment": "entertainment",
    "balanced": "entertainment",
    "ent": "entertainment",
    "performance": "performance",
    "perf": "performance",
}

FAN_MODE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "mode2": "mode2",
    "perf-auto": "mode2",
    "perf_auto": "mode2",
    "max": "max",
    "full": "max",
    "maxq": "maxq",
    "max-q": "maxq",
    "custom": "custom",
    "curve": "custom",
    "silent": "silent",
    "slow": "silent",
    "noiseless": "silent",
    "mode7": "mode7",
}

BATTERY_MODE_ALIASES: dict[str, str] = {
    "limit": "limit",
    "enable": "limit",
    "full": "full",
    "disable": "full",
}

BRIGHTNESS_PRESETS: dict[str, int] = {
    "off": 0,
    "low": 47,
    "p1": 47,
    "med": 95,
    "p2": 95,
    "high": 143,
    "p3": 143,
    "max": 191,
    "p4": 191,
    "full": 255,
}

RGB_PRESETS: dict[str, tuple[int, int, int]] = {
    "blue": (0, 0, 255),
    "green": (0, 255, 0),
    "red": (255, 0, 0),
    "cyan": (0, 255, 200),
    "skyblue": (0, 255, 229),
    "yellow": (255, 255, 0),
    "orange": (255, 127, 0),
    "purple": (162, 0, 255),
    "pink": (255, 0, 210),
    "white": (255, 255, 255),
    "off": (0, 0, 0),
}

PERIPHERAL_KEY_TO_SUBCMD: tuple[tuple[str, str], ...] = (
    ("webcam", "webcam"),
    ("touchpad", "touchpad"),
    ("lid_wake", "lid-wake"),
    ("lid_close", "lid-close"),
    ("win_lock", "win-lock"),
    ("fn_swap", "fn-swap"),
    ("fn_lock", "fn-lock"),
)
PERIPHERAL_SUBCMD_TO_KEY: dict[str, str] = {sub: key for key, sub in PERIPHERAL_KEY_TO_SUBCMD}

IGNORED_FLAGS = frozenset({"--dry-run", "--help", "-h", "--dump-xram", "--scan-xram", "--ec-file"})

_DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "auto_apply_on_launch": True,
    "power": {
        "profile": None,
        "pl1_w": None,
        "pl2_w": None,
        "no_rapl": False,
        "no_os": False,
    },
    "fan": {
        "mode": None,
        "offset_pct": None,
        "cpu_curve": None,
        "gpu_curve": None,
    },
    "keyboard": {
        "brightness": None,
        "mode": None,
        "color": None,
        "cc_scale": False,
        "effect": None,
        "timeout_s": None,
    },
    "battery": {
        "mode": None,
        "start_pct": None,
        "stop_pct": None,
    },
    "peripherals": {
        "webcam": None,
        "touchpad": None,
        "lid_wake": None,
        "lid_close": None,
        "win_lock": None,
        "fn_swap": None,
        "fn_lock": None,
    },
}


def default_config() -> dict[str, Any]:
    """Return a deep copy of the default configuration schema."""
    return deepcopy(_DEFAULT_CONFIG)


def resolve_config_path(
    explicit: str | Path | None = None,
    uid: Optional[int] = None,
    cache_dir: str | Path | None = None,
) -> Path:
    """Resolve config.json path: --config > ACER_CONTROL_CONFIG > pw_dir/.config > cache_dir.parent/.config or XDG_CONFIG_HOME."""
    if explicit is not None and str(explicit).strip():
        return Path(str(explicit).strip()).expanduser()

    env_cfg = os.environ.get("ACER_CONTROL_CONFIG", "").strip()
    if env_cfg:
        return Path(env_cfg).expanduser()

    def _fallback_config_root() -> Path:
        if cache_dir is not None and str(cache_dir).strip():
            return Path(str(cache_dir).strip()).expanduser().parent / ".config"
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        if xdg:
            return Path(xdg).expanduser()
        return Path.home() / ".config"

    if uid is not None:
        try:
            pw_dir = pwd.getpwuid(int(uid)).pw_dir
            if pw_dir:
                return Path(pw_dir) / ".config" / CONFIG_DIR_NAME / CONFIG_FILE_NAME
        except (KeyError, ValueError, TypeError):
            pass
        return _fallback_config_root() / CONFIG_DIR_NAME / CONFIG_FILE_NAME

    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / CONFIG_DIR_NAME / CONFIG_FILE_NAME

    try:
        pw_dir = pwd.getpwuid(os.getuid()).pw_dir
        if pw_dir:
            return Path(pw_dir) / ".config" / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    except KeyError:
        pass
    return _fallback_config_root() / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def _is_strict_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _require_bool(val: Any, field_name: str) -> bool:
    if not isinstance(val, bool):
        raise ValueError(f"{field_name} must be a boolean, got {val!r}")
    return val


def _require_int_range(val: Any, field_name: str, low: int, high: int) -> Optional[int]:
    if val is None:
        return None
    if not _is_strict_int(val) or val < low or val > high:
        raise ValueError(f"{field_name} must be an integer in {low}..{high}, got {val!r}")
    return int(val)


def _normalize_alias(
    val: Any,
    mapping: Mapping[str, str],
    field_name: str,
) -> Optional[str]:
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"{field_name} must be a string or null, got {val!r}")
    cleaned = val.strip().lower()
    if not cleaned:
        return None
    if cleaned not in mapping:
        raise ValueError(f"Invalid {field_name}: {val!r}")
    return mapping[cleaned]


def _validate_middle_curve(vals: Sequence[Any], field_name: str) -> list[int]:
    if not isinstance(vals, (list, tuple)) or len(vals) != 4:
        raise ValueError(f"{field_name} must be a 4-integer list [t2, d2, t3, d3], got {vals!r}")
    if not all(_is_strict_int(v) for v in vals):
        raise ValueError(f"{field_name} entries must be integers, got {vals!r}")
    t2, d2, t3, d3 = (int(v) for v in vals)
    if not (0 < t2 < t3 < 100):
        raise ValueError(f"{field_name} temps must satisfy 0 < t2 < t3 < 100, got ({t2}, {t3})")
    if not (0 < d2 < d3 <= 100):
        raise ValueError(f"{field_name} duties must satisfy 0 < d2 < d3 <= 100, got ({d2}, {d3})")
    return [t2, d2, t3, d3]


def _validate_full_curve_8(vals: Sequence[int]) -> bool:
    if len(vals) != 8 or not all(_is_strict_int(v) for v in vals):
        return False
    t1, d1, t2, d2, t3, d3, t4, d4 = (int(v) for v in vals)
    return 30 <= t1 < t2 < t3 < t4 <= 100 and 0 <= d1 <= d2 <= d3 <= d4 <= 100 and d4 > 0


def _validate_section_dict(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    val = raw.get(key)
    if val is None:
        return {}
    if not isinstance(val, Mapping):
        raise ValueError(f"Config section '{key}' must be an object, got {type(val).__name__}")
    return val


def _validate_power_section(raw_power: Mapping[str, Any], out: dict[str, Any]) -> None:
    out["profile"] = _normalize_alias(raw_power.get("profile"), POWER_PROFILE_ALIASES, "power.profile")
    out["pl1_w"] = _require_int_range(raw_power.get("pl1_w"), "power.pl1_w", 1, 250)
    out["pl2_w"] = _require_int_range(raw_power.get("pl2_w"), "power.pl2_w", 1, 250)
    if "no_rapl" in raw_power and raw_power["no_rapl"] is not None:
        out["no_rapl"] = _require_bool(raw_power["no_rapl"], "power.no_rapl")
    if "no_os" in raw_power and raw_power["no_os"] is not None:
        out["no_os"] = _require_bool(raw_power["no_os"], "power.no_os")


def _validate_fan_section(raw_fan: Mapping[str, Any], out: dict[str, Any]) -> None:
    out["mode"] = _normalize_alias(raw_fan.get("mode"), FAN_MODE_ALIASES, "fan.mode")
    out["offset_pct"] = _require_int_range(raw_fan.get("offset_pct"), "fan.offset_pct", 0, 100)

    cpu_raw = raw_fan.get("cpu_curve")
    gpu_raw = raw_fan.get("gpu_curve")
    combined_raw = raw_fan.get("curve")

    if combined_raw is not None:
        if not isinstance(combined_raw, (list, tuple)) or not all(_is_strict_int(v) for v in combined_raw):
            raise ValueError(f"fan.curve must be a 4- or 8-integer list, got {combined_raw!r}")
        if len(combined_raw) == 8:
            cpu_raw = list(combined_raw[:4])
            gpu_raw = list(combined_raw[4:])
        elif len(combined_raw) == 4:
            cpu_raw = list(combined_raw)
            gpu_raw = list(combined_raw)
        else:
            raise ValueError(f"fan.curve must have 4 or 8 integers, got {len(combined_raw)}")
    elif (
        isinstance(cpu_raw, (list, tuple))
        and len(cpu_raw) == 8
        and gpu_raw is None
        and all(_is_strict_int(v) for v in cpu_raw)
    ):
        if int(cpu_raw[6]) == 100 and _validate_full_curve_8([int(v) for v in cpu_raw]):
            cpu_raw = [int(cpu_raw[2]), int(cpu_raw[3]), int(cpu_raw[4]), int(cpu_raw[5])]
        else:
            gpu_raw = list(cpu_raw[4:])
            cpu_raw = list(cpu_raw[:4])

    if isinstance(gpu_raw, (list, tuple)) and len(gpu_raw) == 8 and all(_is_strict_int(v) for v in gpu_raw):
        if int(gpu_raw[6]) == 100 and _validate_full_curve_8([int(v) for v in gpu_raw]):
            gpu_raw = [int(gpu_raw[2]), int(gpu_raw[3]), int(gpu_raw[4]), int(gpu_raw[5])]

    out["cpu_curve"] = _validate_middle_curve(cpu_raw, "fan.cpu_curve") if cpu_raw is not None else None
    out["gpu_curve"] = _validate_middle_curve(gpu_raw, "fan.gpu_curve") if gpu_raw is not None else None


def _validate_keyboard_section(raw_kb: Mapping[str, Any], out: dict[str, Any]) -> None:
    bri = raw_kb.get("brightness")
    if isinstance(bri, str) and bri.strip().lower() in BRIGHTNESS_PRESETS:
        bri = BRIGHTNESS_PRESETS[bri.strip().lower()]
    out["brightness"] = _require_int_range(bri, "keyboard.brightness", 0, 255)

    mode = raw_kb.get("mode")
    if mode is not None:
        if not isinstance(mode, str):
            raise ValueError(f"keyboard.mode must be 'static', 'effect', or null, got {mode!r}")
        mode_norm = mode.strip().lower()
        if not mode_norm:
            mode_norm = None
        elif mode_norm not in ("static", "effect"):
            raise ValueError(f"Invalid keyboard.mode: {mode!r}")
        out["mode"] = mode_norm

    color = raw_kb.get("color")
    if color is not None:
        if isinstance(color, str) and color.strip().lower() in RGB_PRESETS:
            color = list(RGB_PRESETS[color.strip().lower()])
        if not isinstance(color, (list, tuple)) or len(color) != 3 or not all(_is_strict_int(c) for c in color):
            raise ValueError(f"keyboard.color must be a 3-integer [R, G, B] list, got {color!r}")
        r, g, b = (int(c) for c in color)
        if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
            raise ValueError(f"keyboard.color channels must be in 0..255, got {[r, g, b]}")
        out["color"] = [r, g, b]

    if "cc_scale" in raw_kb and raw_kb["cc_scale"] is not None:
        out["cc_scale"] = _require_bool(raw_kb["cc_scale"], "keyboard.cc_scale")

    eff = raw_kb.get("effect")
    if eff is not None:
        if not isinstance(eff, str):
            raise ValueError(f"keyboard.effect must be 'breath' or null, got {eff!r}")
        eff_norm = eff.strip().lower()
        if not eff_norm:
            eff_norm = None
        elif eff_norm != "breath":
            raise ValueError(f"Invalid keyboard.effect: {eff!r}")
        out["effect"] = eff_norm

    to_val = raw_kb.get("timeout_s")
    if isinstance(to_val, str) and to_val.strip().lower() == "off":
        to_val = 0
    if to_val is not None:
        if not _is_strict_int(to_val):
            raise ValueError(f"keyboard.timeout_s must be 0 or 5..65535, got {to_val!r}")
        to_int = int(to_val)
        if to_int != 0 and not (5 <= to_int <= 65535):
            raise ValueError(f"keyboard.timeout_s must be 0 ('off') or 5..65535, got {to_int}")
        out["timeout_s"] = to_int


def _validate_battery_section(raw_bat: Mapping[str, Any], out: dict[str, Any]) -> None:
    mode = _normalize_alias(raw_bat.get("mode"), BATTERY_MODE_ALIASES, "battery.mode")
    start = _require_int_range(raw_bat.get("start_pct"), "battery.start_pct", 20, 99)
    stop = _require_int_range(raw_bat.get("stop_pct"), "battery.stop_pct", 21, 100)
    if (start is None) != (stop is None):
        raise ValueError("battery.start_pct and battery.stop_pct must both be set or both null")
    if start is not None and stop is not None and start >= stop:
        raise ValueError(f"battery thresholds must satisfy 20 <= start_pct < stop_pct <= 100, got ({start}, {stop})")
    if mode == "limit" and start is None and stop is None:
        start, stop = 60, 80
    out["mode"] = mode
    out["start_pct"] = start
    out["stop_pct"] = stop


def _validate_peripherals_section(raw_per: Mapping[str, Any], out: dict[str, Any]) -> None:
    on_off_map = {"on": "on", "off": "off"}
    lid_close_map = {"suspend": "suspend", "ignore": "ignore"}
    for key in ("webcam", "touchpad", "lid_wake", "win_lock", "fn_swap", "fn_lock"):
        out[key] = _normalize_alias(raw_per.get(key), on_off_map, f"peripherals.{key}")
    out["lid_close"] = _normalize_alias(raw_per.get("lid_close"), lid_close_map, "peripherals.lid_close")


def validate_config(raw: Any) -> dict[str, Any]:
    """Validate and normalize a configuration mapping into canonical schema v1."""
    if not isinstance(raw, Mapping):
        raise ValueError("Configuration root must be a JSON object.")

    cfg = default_config()
    if "version" in raw and raw["version"] is not None:
        ver = raw["version"]
        if not _is_strict_int(ver) or ver != CONFIG_VERSION:
            raise ValueError(f"Unsupported config version: {ver!r} (expected {CONFIG_VERSION})")
    if "auto_apply_on_launch" in raw and raw["auto_apply_on_launch"] is not None:
        cfg["auto_apply_on_launch"] = _require_bool(raw["auto_apply_on_launch"], "auto_apply_on_launch")

    _validate_power_section(_validate_section_dict(raw, "power"), cfg["power"])
    _validate_fan_section(_validate_section_dict(raw, "fan"), cfg["fan"])
    _validate_keyboard_section(_validate_section_dict(raw, "keyboard"), cfg["keyboard"])
    _validate_battery_section(_validate_section_dict(raw, "battery"), cfg["battery"])
    _validate_peripherals_section(_validate_section_dict(raw, "peripherals"), cfg["peripherals"])
    return cfg


def _seed_brightness(state_dir: Path, cfg: dict[str, Any]) -> None:
    path = state_dir / "brightness_state"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return
    if not text:
        return
    m = re.search(r"\bbrightness=(\d+)\b", text)
    if m:
        level = int(m.group(1))
    elif re.fullmatch(r"\d+", text):
        level = int(text)
    else:
        return
    if 0 <= level <= 255:
        cfg["keyboard"]["brightness"] = level


def _seed_rgb(state_dir: Path, cfg: dict[str, Any]) -> None:
    path = state_dir / "rgb_state"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return
    joined = " ".join(lines)
    kb = cfg["keyboard"]

    m_mode = re.search(r"\bmode=([A-Za-z0-9_-]+)\b", joined)
    mode_val = m_mode.group(1).lower() if m_mode else None

    m_eff = re.search(r"\beffect=([A-Za-z0-9_-]+)\b", joined)
    if m_eff and mode_val in (None, "effect"):
        eff = m_eff.group(1).lower()
        if eff == "breath":
            kb["mode"] = "effect"
            kb["effect"] = "breath"
    elif mode_val in (None, "static"):
        cc_scale = bool(re.search(r"\bcc_scale=1\b", joined))
        m_color = re.search(r"\bcolor=\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", joined)
        rgb: Optional[tuple[int, int, int]] = None
        if m_color:
            rgb = (int(m_color.group(1)), int(m_color.group(2)), int(m_color.group(3)))
        else:
            mr = re.search(r"\br=(\d+)\b", joined)
            mg = re.search(r"\bg=(\d+)\b", joined)
            mb = re.search(r"\bb=(\d+)\b", joined)
            if mr and mg and mb:
                rgb = (int(mr.group(1)), int(mg.group(1)), int(mb.group(1)))
        if rgb is not None and all(0 <= c <= 255 for c in rgb):
            kb["mode"] = "static"
            kb["color"] = list(rgb)
            kb["cc_scale"] = cc_scale

    m_to = re.search(r"\btimeout=([A-Za-z0-9_-]+)\b", joined)
    if m_to:
        to_raw = m_to.group(1).lower()
        if to_raw in ("off", "0"):
            kb["timeout_s"] = 0
        elif to_raw.isdigit():
            to_sec = int(to_raw)
            if 5 <= to_sec <= 65535:
                kb["timeout_s"] = to_sec


def _seed_fan_curve(state_dir: Path, cfg: dict[str, Any]) -> None:
    path = state_dir / "fan_curve_state"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    curves: dict[str, list[int]] = {}
    active_custom = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().lower()
        val = val.strip()
        if key in ("cpu", "gpu"):
            tokens = [tok for tok in re.split(r"[,\s]+", val) if tok]
            if len(tokens) == 8 and all(re.fullmatch(r"\d+", t) for t in tokens):
                nums = [int(t) for t in tokens]
                if _validate_full_curve_8(nums):
                    curves[key] = [nums[2], nums[3], nums[4], nums[5]]
        elif key == "active" and val.lower() in ("custom", "1", "true"):
            active_custom = True
        elif key == "mode" and val.lower() == "custom":
            active_custom = True
        elif key == "enabled" and val == "1":
            active_custom = True

    if "cpu" in curves:
        cfg["fan"]["cpu_curve"] = curves["cpu"]
    if "gpu" in curves:
        cfg["fan"]["gpu_curve"] = curves["gpu"]
    if active_custom and ("cpu" in curves or "gpu" in curves):
        cfg["fan"]["mode"] = "custom"


def _seed_peripherals(state_dir: Path, cfg: dict[str, Any]) -> None:
    path = state_dir / "peripherals_state"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    key_map = {
        "WEBCAM": "webcam",
        "TOUCHPAD": "touchpad",
        "LID_WAKE": "lid_wake",
        "LID_CLOSE": "lid_close",
        "WIN_LOCK": "win_lock",
        "FN_SWAP": "fn_swap",
        "FN_LOCK": "fn_lock",
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        raw_k, raw_v = line.split("=", 1)
        k = raw_k.strip().upper()
        v = raw_v.strip().lower()
        cfg_key = key_map.get(k)
        if not cfg_key:
            continue
        if cfg_key == "lid_close":
            if v in ("suspend", "ignore"):
                cfg["peripherals"][cfg_key] = v
        else:
            if v in ("on", "1", "true"):
                cfg["peripherals"][cfg_key] = "on"
            elif v in ("off", "0", "false"):
                cfg["peripherals"][cfg_key] = "off"


def seed_from_legacy_cache(cache_dir: str | Path | None) -> dict[str, Any]:
    """Migrate legacy <cache_dir>/acer-a715-59g/* state files into a fresh config dict."""
    cfg = default_config()
    if cache_dir is None or not str(cache_dir).strip():
        return cfg
    state_dir = Path(str(cache_dir).strip()).expanduser() / LEGACY_CACHE_SUBDIR
    if not state_dir.is_dir():
        return cfg
    _seed_brightness(state_dir, cfg)
    _seed_rgb(state_dir, cfg)
    _seed_fan_curve(state_dir, cfg)
    _seed_peripherals(state_dir, cfg)
    return validate_config(cfg)


def _lock_path_for(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.name}.lock")


def _chown_if_root(path: Path, uid: Optional[int], gid: Optional[int]) -> None:
    if os.geteuid() != 0 or uid is None or gid is None:
        return
    try:
        os.chown(path, int(uid), int(gid), follow_symlinks=False)
    except OSError:
        pass


def _ensure_parent_dir(config_path: Path, uid: Optional[int], gid: Optional[int]) -> None:
    parent = config_path.parent
    grandparent = parent.parent
    grandparent_missing = grandparent.name == ".config" and not grandparent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if grandparent_missing:
        _chown_if_root(grandparent, uid, gid)
    _chown_if_root(parent, uid, gid)


@contextmanager
def _config_lock(
    config_path: Path,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    exclusive: bool = True,
) -> Iterator[None]:
    _ensure_parent_dir(config_path, uid, gid)
    lock_path = _lock_path_for(config_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _chown_if_root(lock_path, uid, gid)
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_atomic_unlocked(
    config_path: Path,
    cfg: dict[str, Any],
    uid: Optional[int] = None,
    gid: Optional[int] = None,
) -> None:
    _ensure_parent_dir(config_path, uid, gid)
    payload = json.dumps(cfg, indent=2) + "\n"
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(config_path.parent),
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fp:
            tmp_path = Path(fp.name)
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp_path, 0o644)
        _chown_if_root(tmp_path, uid, gid)
        os.replace(tmp_path, config_path)
        _chown_if_root(config_path, uid, gid)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _backup_malformed_and_reset_unlocked(
    config_path: Path,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
) -> dict[str, Any]:
    bak_path = config_path.with_name(f"{config_path.name}.bak")
    if config_path.exists():
        os.replace(config_path, bak_path)
        _chown_if_root(bak_path, uid, gid)
    defaults = default_config()
    _write_atomic_unlocked(config_path, defaults, uid=uid, gid=gid)
    return defaults


def _read_and_validate_unlocked(
    config_path: Path,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
) -> dict[str, Any]:
    try:
        raw_text = config_path.read_text(encoding="utf-8")
        raw_obj = json.loads(raw_text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _backup_malformed_and_reset_unlocked(config_path, uid=uid, gid=gid)
    if not isinstance(raw_obj, Mapping):
        return _backup_malformed_and_reset_unlocked(config_path, uid=uid, gid=gid)
    return validate_config(raw_obj)


def save_config(
    path: str | Path,
    cfg: Mapping[str, Any],
    uid: Optional[int] = None,
    gid: Optional[int] = None,
) -> dict[str, Any]:
    """Validate and atomically save configuration JSON under an exclusive flock."""
    config_path = Path(path).expanduser()
    validated = validate_config(cfg)
    with _config_lock(config_path, uid=uid, gid=gid, exclusive=True):
        _write_atomic_unlocked(config_path, validated, uid=uid, gid=gid)
    return validated


def load_config(
    path: str | Path,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
) -> dict[str, Any]:
    """Load and validate config.json, backing up malformed JSON to .bak and resetting defaults."""
    config_path = Path(path).expanduser()
    if not config_path.exists():
        return default_config()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
        raw_obj = json.loads(raw_text)
        if not isinstance(raw_obj, Mapping):
            raise ValueError("Config root is not a JSON object")
        return validate_config(raw_obj)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"Warning: malformed config JSON at {config_path} ({exc}); backing up to .bak", file=sys.stderr)
    except ValueError as exc:
        if "not a JSON object" not in str(exc):
            raise

    with _config_lock(config_path, uid=uid, gid=gid, exclusive=True):
        if not config_path.exists():
            return default_config()
        return _read_and_validate_unlocked(config_path, uid=uid, gid=gid)


def ensure_config_file(
    path: str | Path,
    cache_dir: str | Path | None = None,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
) -> dict[str, Any]:
    """Ensure config.json exists (migrating legacy cache on first run) and return validated config."""
    config_path = Path(path).expanduser()
    with _config_lock(config_path, uid=uid, gid=gid, exclusive=True):
        if not config_path.exists():
            cfg = seed_from_legacy_cache(cache_dir)
            _write_atomic_unlocked(config_path, cfg, uid=uid, gid=gid)
            return cfg
        return _read_and_validate_unlocked(config_path, uid=uid, gid=gid)


def _has_ignored_flag(args: Sequence[str]) -> bool:
    for arg in args:
        if arg in IGNORED_FLAGS or arg.startswith("--ec-file="):
            return True
    return False


def _record_power_action(cfg: dict[str, Any], args: Sequence[str]) -> bool:
    pl1_w: Optional[int] = None
    pl2_w: Optional[int] = None
    no_rapl = False
    no_os = False
    action: Optional[str] = None

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--pl1":
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                return False
            pl1_w = int(args[i + 1])
            i += 2
        elif tok.startswith("--pl1="):
            val = tok.split("=", 1)[1]
            if not val.isdigit():
                return False
            pl1_w = int(val)
            i += 1
        elif tok == "--pl2":
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                return False
            pl2_w = int(args[i + 1])
            i += 2
        elif tok.startswith("--pl2="):
            val = tok.split("=", 1)[1]
            if not val.isdigit():
                return False
            pl2_w = int(val)
            i += 1
        elif tok == "--gpu-boost" or tok.startswith("--gpu-boost="):
            return False
        elif tok == "--no-wmi":
            i += 1
        elif tok == "--no-rapl":
            no_rapl = True
            i += 1
        elif tok == "--no-os":
            no_os = True
            i += 1
        elif tok.startswith("-"):
            return False
        else:
            if action is not None:
                return False
            action = tok
            i += 1

    if action is None or action.lower() in ("status", "get"):
        return False
    profile = POWER_PROFILE_ALIASES.get(action.lower())
    if profile is None:
        return False
    if no_rapl and (pl1_w is not None or pl2_w is not None):
        return False
    if pl1_w is not None and not (1 <= pl1_w <= 250):
        return False
    if pl2_w is not None and not (1 <= pl2_w <= 250):
        return False

    cfg["power"]["profile"] = profile
    cfg["power"]["no_rapl"] = no_rapl
    cfg["power"]["no_os"] = no_os
    cfg["power"]["pl1_w"] = None if no_rapl else pl1_w
    cfg["power"]["pl2_w"] = None if no_rapl else pl2_w
    return True


def _record_fan_action(cfg: dict[str, Any], args: Sequence[str]) -> bool:
    pos = [a for a in args if not a.startswith("-")]
    if any(a.startswith("-") for a in args):
        return False
    if not pos:
        return False
    cmd = pos[0].lower()
    extra = pos[1:]

    if cmd in ("status", "dump-xram", "scan-xram"):
        return False
    if cmd == "offset":
        if len(extra) != 1 or not extra[0].isdigit():
            return False
        pct = int(extra[0])
        if not (0 <= pct <= 100):
            return False
        cfg["fan"]["offset_pct"] = pct
        return True
    if cmd == "reset":
        if extra:
            return False
        cfg["fan"]["mode"] = "auto"
        cfg["fan"]["cpu_curve"] = list(DEFAULT_MIDDLE_CURVE)
        cfg["fan"]["gpu_curve"] = list(DEFAULT_MIDDLE_CURVE)
        return True
    if cmd in ("custom", "curve"):
        if len(extra) == 0:
            cfg["fan"]["mode"] = "custom"
            return True
        if len(extra) not in (4, 8) or not all(x.isdigit() for x in extra):
            return False
        nums = [int(x) for x in extra]
        try:
            cpu_curve = _validate_middle_curve(nums[:4], "fan.cpu_curve")
            gpu_curve = _validate_middle_curve(nums[4:] if len(nums) == 8 else nums[:4], "fan.gpu_curve")
        except ValueError:
            return False
        cfg["fan"]["mode"] = "custom"
        cfg["fan"]["cpu_curve"] = cpu_curve
        cfg["fan"]["gpu_curve"] = gpu_curve
        return True

    mode = FAN_MODE_ALIASES.get(cmd)
    if mode is None or extra:
        return False
    cfg["fan"]["mode"] = mode
    return True


def _record_brightness_action(cfg: dict[str, Any], args: Sequence[str]) -> bool:
    if any(a.startswith("-") for a in args) or len(args) != 1:
        return False
    tok = args[0].strip().lower()
    if tok == "status":
        return False
    if tok in BRIGHTNESS_PRESETS:
        level = BRIGHTNESS_PRESETS[tok]
    elif tok.isdigit():
        level = int(tok)
    else:
        return False
    if not (0 <= level <= 255):
        return False
    cfg["keyboard"]["brightness"] = level
    return True


def _record_rgb_action(cfg: dict[str, Any], args: Sequence[str]) -> bool:
    cc_scale = False
    effect: Optional[str] = None
    timeout: Optional[str] = None
    pos: list[str] = []

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--cc-scale":
            cc_scale = True
            i += 1
        elif tok == "--effect":
            if i + 1 >= len(args):
                return False
            effect = args[i + 1]
            i += 2
        elif tok.startswith("--effect="):
            effect = tok.split("=", 1)[1]
            i += 1
        elif tok == "--timeout":
            if i + 1 >= len(args):
                return False
            timeout = args[i + 1]
            i += 2
        elif tok.startswith("--timeout="):
            timeout = tok.split("=", 1)[1]
            i += 1
        elif tok.startswith("-"):
            return False
        else:
            pos.append(tok)
            i += 1

    if effect is not None and timeout is not None:
        return False

    if effect is not None:
        if cc_scale or pos or effect.strip().lower() != "breath":
            return False
        cfg["keyboard"]["mode"] = "effect"
        cfg["keyboard"]["effect"] = "breath"
        return True

    if timeout is not None:
        if cc_scale or pos:
            return False
        to_low = timeout.strip().lower()
        if to_low in ("off", "0"):
            to_int = 0
        elif to_low.isdigit():
            to_int = int(to_low)
            if not (5 <= to_int <= 65535):
                return False
        else:
            return False
        cfg["keyboard"]["timeout_s"] = to_int
        return True

    if not pos or pos[0].lower() == "status":
        return False

    if len(pos) == 1 and pos[0].lower() in RGB_PRESETS:
        r, g, b = RGB_PRESETS[pos[0].lower()]
    elif len(pos) == 3 and all(p.isdigit() for p in pos):
        r, g, b = (int(p) for p in pos)
        if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
            return False
    else:
        return False

    cfg["keyboard"]["mode"] = "static"
    cfg["keyboard"]["color"] = [r, g, b]
    cfg["keyboard"]["cc_scale"] = cc_scale
    return True


def _record_battery_action(cfg: dict[str, Any], args: Sequence[str]) -> bool:
    pos: list[str] = []
    for idx, tok in enumerate(args):
        if tok == "--":
            pos.extend(args[idx + 1 :])
            break
        if tok.startswith("-"):
            return False
        pos.append(tok)

    if not pos or pos[0].lower() == "status":
        return False
    cmd = pos[0].lower()
    extra = pos[1:]

    if cmd == "enable":
        if len(extra) == 0:
            start, stop = 60, 80
        elif len(extra) == 2 and all(x.isdigit() for x in extra):
            start, stop = int(extra[0]), int(extra[1])
        else:
            return False
    elif cmd == "limit":
        if len(extra) != 2 or not all(x.isdigit() for x in extra):
            return False
        start, stop = int(extra[0]), int(extra[1])
    elif cmd in ("full", "disable"):
        if extra:
            return False
        cfg["battery"]["mode"] = "full"
        return True
    else:
        return False

    if not (20 <= start < stop <= 100):
        return False
    cfg["battery"]["mode"] = "limit"
    cfg["battery"]["start_pct"] = start
    cfg["battery"]["stop_pct"] = stop
    return True


def _record_peripherals_action(cfg: dict[str, Any], args: Sequence[str]) -> bool:
    pos: list[str] = []
    for idx, tok in enumerate(args):
        if tok == "--":
            pos.extend(args[idx + 1 :])
            break
        if tok.startswith("-"):
            return False
        pos.append(tok)

    if not pos or pos[0].lower() == "status" or len(pos) != 2:
        return False
    subcmd = pos[0].lower()
    val = pos[1].lower()
    cfg_key = PERIPHERAL_SUBCMD_TO_KEY.get(subcmd)
    if cfg_key is None:
        return False

    if cfg_key == "lid_close":
        if val in ("suspend", "ignore"):
            cfg["peripherals"][cfg_key] = val
            return True
        if val == "toggle":
            cur = cfg["peripherals"].get(cfg_key)
            if cur in ("suspend", "ignore"):
                cfg["peripherals"][cfg_key] = "ignore" if cur == "suspend" else "suspend"
                return True
        return False

    if val in ("on", "off"):
        cfg["peripherals"][cfg_key] = val
        return True
    if val == "toggle":
        cur = cfg["peripherals"].get(cfg_key)
        if cur in ("on", "off"):
            cfg["peripherals"][cfg_key] = "off" if cur == "on" else "on"
            return True
    return False


_SCRIPT_RECORDERS = {
    "power_profile.sh": _record_power_action,
    "fan.sh": _record_fan_action,
    "brightness.sh": _record_brightness_action,
    "rgb.sh": _record_rgb_action,
    "battery.sh": _record_battery_action,
    "peripherals.sh": _record_peripherals_action,
}


def record_action(cfg: dict[str, Any], script: str, args: Sequence[str]) -> bool:
    """Update cfg in-place from a successful script invocation; returns True if updated."""
    arg_list = [str(a) for a in args]
    if _has_ignored_flag(arg_list):
        return False
    recorder = _SCRIPT_RECORDERS.get(os.path.basename(script))
    if recorder is None:
        return False
    return recorder(cfg, arg_list)


def record_action_file(
    path: str | Path,
    script: str,
    args: Sequence[str],
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    cache_dir: str | Path | None = None,
) -> bool:
    """Safely update config.json on disk under lock for a successful script invocation."""
    arg_list = [str(a) for a in args]
    if _has_ignored_flag(arg_list):
        return False
    if os.path.basename(script) not in _SCRIPT_RECORDERS:
        return False

    probe_cfg = default_config()
    if not record_action(probe_cfg, script, arg_list):
        # Fast skip for status/get/no-op commands unless toggle depends on saved state.
        is_toggle = len(arg_list) >= 2 and arg_list[-1].lower() == "toggle"
        if not is_toggle:
            return False

    config_path = Path(path).expanduser()
    with _config_lock(config_path, uid=uid, gid=gid, exclusive=True):
        if config_path.exists():
            cfg = _read_and_validate_unlocked(config_path, uid=uid, gid=gid)
        else:
            cfg = seed_from_legacy_cache(cache_dir)
        updated = record_action(cfg, script, arg_list)
        if not updated:
            return False
        validated = validate_config(cfg)
        _write_atomic_unlocked(config_path, validated, uid=uid, gid=gid)
        return True


def plan_apply_actions(cfg: Mapping[str, Any], force: bool = False) -> list[tuple[str, list[str]]]:
    """Build deterministic ordered list of (script, args) actions from validated config."""
    norm = validate_config(cfg)
    if not force and not norm.get("auto_apply_on_launch", True):
        return []

    actions: list[tuple[str, list[str]]] = []

    # 1. power_profile.sh
    power = norm["power"]
    profile = power["profile"]
    if profile is not None:
        p_args: list[str] = []
        if power["no_rapl"]:
            p_args.append("--no-rapl")
        else:
            if power["pl1_w"] is not None:
                p_args.extend(["--pl1", str(power["pl1_w"])])
            if power["pl2_w"] is not None:
                p_args.extend(["--pl2", str(power["pl2_w"])])
        if power["no_os"]:
            p_args.append("--no-os")
        p_args.append(profile)
        actions.append(("power_profile.sh", p_args))

    # 2. fan.sh (skip completely under quiet / powersaving)
    if profile not in ("quiet", "powersaving"):
        fan = norm["fan"]
        if fan["offset_pct"] is not None:
            actions.append(("fan.sh", ["offset", str(fan["offset_pct"])]))
        fan_mode = fan["mode"]
        if fan_mode is not None:
            if not (fan_mode == "auto" and profile == "performance"):
                if fan_mode == "custom":
                    cpu_c = fan["cpu_curve"]
                    gpu_c = fan["gpu_curve"]
                    if cpu_c is not None and gpu_c is not None:
                        actions.append(("fan.sh", ["custom", *(str(x) for x in cpu_c), *(str(x) for x in gpu_c)]))
                    elif cpu_c is not None:
                        actions.append(("fan.sh", ["custom", *(str(x) for x in cpu_c)]))
                    elif gpu_c is not None:
                        actions.append(
                            ("fan.sh", ["custom", *(str(x) for x in DEFAULT_MIDDLE_CURVE), *(str(x) for x in gpu_c)])
                        )
                    else:
                        actions.append(("fan.sh", ["custom"]))
                else:
                    actions.append(("fan.sh", [fan_mode]))

    # 3. brightness.sh
    kb = norm["keyboard"]
    if kb["brightness"] is not None:
        actions.append(("brightness.sh", [str(kb["brightness"])]))

    # 4. rgb.sh (--timeout first if set, then off/--effect/[--cc-scale] color)
    if kb["timeout_s"] is not None:
        to_str = "off" if kb["timeout_s"] == 0 else str(kb["timeout_s"])
        actions.append(("rgb.sh", ["--timeout", to_str]))

    kb_mode = kb["mode"]
    if kb_mode == "effect" or (kb_mode is None and kb["effect"] is not None):
        if kb["effect"] is not None:
            actions.append(("rgb.sh", ["--effect", kb["effect"]]))
    elif kb_mode == "static" or (kb_mode is None and kb["color"] is not None):
        if kb["color"] is not None:
            r, g, b = kb["color"]
            if [r, g, b] == [0, 0, 0]:
                rgb_args = ["--cc-scale", "off"] if kb["cc_scale"] else ["off"]
            else:
                rgb_args = (["--cc-scale"] if kb["cc_scale"] else []) + [str(r), str(g), str(b)]
            actions.append(("rgb.sh", rgb_args))

    # 5. battery.sh
    bat = norm["battery"]
    if bat["mode"] == "limit" and bat["start_pct"] is not None and bat["stop_pct"] is not None:
        actions.append(("battery.sh", ["limit", str(bat["start_pct"]), str(bat["stop_pct"])]))
    elif bat["mode"] == "full":
        actions.append(("battery.sh", ["full"]))

    # 6. peripherals.sh
    per = norm["peripherals"]
    for cfg_key, subcmd in PERIPHERAL_KEY_TO_SUBCMD:
        if per[cfg_key] is not None:
            actions.append(("peripherals.sh", [subcmd, per[cfg_key]]))

    return actions


def run_self_test() -> int:
    """Run isolated config_store unit tests strictly inside tempfile.TemporaryDirectory()."""
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        status = "PASS" if cond else "FAIL"
        suffix = f" ({detail})" if detail else ""
        print(f"[{status}] {name}{suffix}")
        if not cond:
            failures.append(f"{name}: {detail}")

    with tempfile.TemporaryDirectory(prefix="acer_config_selftest_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        old_env = os.environ.get("ACER_CONTROL_CONFIG")
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        try:
            # 1. Path resolution precedence
            env_cfg = tmp_path / "env_cfg.json"
            explicit_cfg = tmp_path / "explicit_cfg.json"
            os.environ["ACER_CONTROL_CONFIG"] = str(env_cfg)
            os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "xdg_cfg")
            check(
                "resolve_config_path prefers explicit over env",
                resolve_config_path(explicit=explicit_cfg, uid=os.getuid()) == explicit_cfg,
            )
            check(
                "resolve_config_path uses ACER_CONTROL_CONFIG when explicit omitted",
                resolve_config_path(uid=os.getuid()) == env_cfg,
            )
            os.environ.pop("ACER_CONTROL_CONFIG", None)
            fallback_cache = tmp_path / "user_home" / ".cache"
            check(
                "resolve_config_path falls back to cache_dir.parent/.config on unknown uid",
                resolve_config_path(uid=9999999, cache_dir=fallback_cache)
                == tmp_path / "user_home" / ".config" / CONFIG_DIR_NAME / CONFIG_FILE_NAME,
            )
            check(
                "resolve_config_path uses XDG_CONFIG_HOME when uid is None",
                resolve_config_path(uid=None)
                == tmp_path / "xdg_cfg" / CONFIG_DIR_NAME / CONFIG_FILE_NAME,
            )

            # 2. Alias normalization, 8-int curve splitting & bounds checks
            norm = validate_config({
                "power": {"profile": "eco"},
                "fan": {"mode": "full", "curve": [52, 48, 74, 72, 58, 54, 80, 82], "offset_pct": 20},
                "keyboard": {"brightness": "max", "color": "cyan", "mode": "static", "timeout_s": "off"},
                "battery": {"mode": "enable"},
                "peripherals": {"webcam": "OFF", "lid_close": "IGNORE"},
            })
            check(
                "validate_config normalizes aliases and splits 8-int curve",
                norm["power"]["profile"] == "powersaving"
                and norm["fan"]["mode"] == "max"
                and norm["fan"]["cpu_curve"] == [52, 48, 74, 72]
                and norm["fan"]["gpu_curve"] == [58, 54, 80, 82]
                and norm["keyboard"]["brightness"] == 191
                and norm["keyboard"]["color"] == [0, 255, 200]
                and norm["keyboard"]["timeout_s"] == 0
                and norm["battery"] == {"mode": "limit", "start_pct": 60, "stop_pct": 80}
                and norm["peripherals"]["webcam"] == "off"
                and norm["peripherals"]["lid_close"] == "ignore",
                str(norm),
            )
            for p_alias, p_exp in (
                ("silent", "quiet"),
                ("power-saving", "powersaving"),
                ("saving", "powersaving"),
                ("saver", "powersaving"),
                ("balanced", "entertainment"),
                ("ent", "entertainment"),
                ("perf", "performance"),
            ):
                check(
                    f"validate_config profile alias {p_alias}->{p_exp}",
                    validate_config({"power": {"profile": p_alias}})["power"]["profile"] == p_exp,
                )
            check(
                "validate_config battery alias disable->full",
                validate_config({"battery": {"mode": "disable"}})["battery"]["mode"] == "full",
            )

            bad_cases = [
                {"power": {"pl1_w": 0}},
                {"power": {"profile": "turbo"}},
                {"fan": {"offset_pct": 101}},
                {"fan": {"cpu_curve": [70, 50, 60, 80]}},
                {"keyboard": {"brightness": 256}},
                {"keyboard": {"timeout_s": 3}},
                {"battery": {"mode": "limit", "start_pct": 80, "stop_pct": 80}},
            ]
            bad_rejected = True
            for bad in bad_cases:
                try:
                    validate_config(bad)
                    bad_rejected = False
                    break
                except ValueError:
                    pass
            check("validate_config rejects out-of-bounds/invalid inputs", bad_rejected)

            # 3. Legacy cache seeding & malformed JSON backup
            cache_root = tmp_path / "cache"
            state_dir = cache_root / LEGACY_CACHE_SUBDIR
            state_dir.mkdir(parents=True)
            (state_dir / "brightness_state").write_text("brightness=191 (0xBF)\n", encoding="utf-8")
            (state_dir / "rgb_state").write_text("effect=breath\ntimeout=60\n", encoding="utf-8")
            (state_dir / "fan_curve_state").write_text(
                "cpu=45,28,60,50,80,66,100,100\ngpu=45,28,60,58,80,66,100,100\n",
                encoding="utf-8",
            )
            (state_dir / "peripherals_state").write_text("WEBCAM=OFF\nTOUCHPAD=ON\n", encoding="utf-8")

            cfg_file = tmp_path / "cfg" / "config.json"
            seeded = ensure_config_file(cfg_file, cache_dir=cache_root, uid=os.getuid(), gid=os.getgid())
            check(
                "ensure_config_file migrates legacy cache only on first run",
                cfg_file.is_file()
                and seeded["keyboard"]["brightness"] == 191
                and seeded["keyboard"]["mode"] == "effect"
                and seeded["keyboard"]["effect"] == "breath"
                and seeded["keyboard"]["timeout_s"] == 60
                and seeded["fan"]["mode"] is None
                and seeded["fan"]["cpu_curve"] == [60, 50, 80, 66]
                and seeded["fan"]["gpu_curve"] == [60, 58, 80, 66]
                and seeded["peripherals"]["webcam"] == "off"
                and seeded["peripherals"]["touchpad"] == "on",
                str(seeded),
            )

            corrupt_file = tmp_path / "corrupt" / "config.json"
            corrupt_file.parent.mkdir(parents=True)
            corrupt_file.write_text("{not valid json", encoding="utf-8")
            recovered = load_config(corrupt_file, uid=os.getuid(), gid=os.getgid())
            bak_file = corrupt_file.with_name("config.json.bak")
            check(
                "load_config backs up malformed JSON to .bak and resets defaults",
                bak_file.is_file()
                and bak_file.read_text(encoding="utf-8") == "{not valid json"
                and recovered == default_config(),
            )

            # 4. record_action_file & independent RGB/flag preservation
            rec_file = tmp_path / "rec" / "config.json"
            check(
                "record_action ignores --dry-run/--ec-file/status/--dump-xram/--scan-xram",
                not record_action_file(rec_file, "power_profile.sh", ["--dry-run", "performance"])
                and not record_action_file(rec_file, "fan.sh", ["--ec-file", "/tmp/x", "status"])
                and not record_action_file(rec_file, "fan.sh", ["--dump-xram"])
                and not record_action_file(rec_file, "fan.sh", ["--scan-xram", "0x0300", "0x10"])
                and not record_action_file(rec_file, "rgb.sh", ["--help"])
                and not record_action_file(rec_file, "power_profile.sh", ["get"])
                and not rec_file.exists(),
            )

            record_action_file(rec_file, "power_profile.sh", ["--pl1", "55", "--pl2", "90", "--no-os", "perf"])
            record_action_file(rec_file, "fan.sh", ["offset", "15"])
            record_action_file(rec_file, "fan.sh", ["custom", "54", "52", "76", "74", "58", "56", "82", "84"])
            record_action_file(rec_file, "brightness.sh", ["high"])
            record_action_file(rec_file, "rgb.sh", ["--cc-scale", "cyan"])
            record_action_file(rec_file, "rgb.sh", ["--timeout", "120"])
            record_action_file(rec_file, "rgb.sh", ["--effect", "breath"])
            record_action_file(rec_file, "battery.sh", ["limit", "60", "80"])
            record_action_file(rec_file, "peripherals.sh", ["win-lock", "on"])
            record_action_file(rec_file, "peripherals.sh", ["lid-close", "ignore"])
            rec_cfg = load_config(rec_file)
            check(
                "record_action_file records all 6 scripts and preserves RGB timeout/color/effect",
                rec_cfg["power"] == {"profile": "performance", "pl1_w": 55, "pl2_w": 90, "no_rapl": False, "no_os": True}
                and rec_cfg["fan"] == {"mode": "custom", "offset_pct": 15, "cpu_curve": [54, 52, 76, 74], "gpu_curve": [58, 56, 82, 84]}
                and rec_cfg["keyboard"] == {"brightness": 143, "mode": "effect", "color": [0, 255, 200], "cc_scale": True, "effect": "breath", "timeout_s": 120}
                and rec_cfg["battery"] == {"mode": "limit", "start_pct": 60, "stop_pct": 80}
                and rec_cfg["peripherals"]["win_lock"] == "on"
                and rec_cfg["peripherals"]["lid_close"] == "ignore",
                str(rec_cfg),
            )

            # 5. plan_apply_actions ordering, auto_apply gating & fan skip rules
            planned = plan_apply_actions(rec_cfg)
            expected_plan = [
                ("power_profile.sh", ["--pl1", "55", "--pl2", "90", "--no-os", "performance"]),
                ("fan.sh", ["offset", "15"]),
                ("fan.sh", ["custom", "54", "52", "76", "74", "58", "56", "82", "84"]),
                ("brightness.sh", ["143"]),
                ("rgb.sh", ["--timeout", "120"]),
                ("rgb.sh", ["--effect", "breath"]),
                ("battery.sh", ["limit", "60", "80"]),
                ("peripherals.sh", ["lid-close", "ignore"]),
                ("peripherals.sh", ["win-lock", "on"]),
            ]
            check("plan_apply_actions deterministic order", planned == expected_plan, str(planned))

            quiet_cfg = deepcopy(rec_cfg)
            quiet_cfg["power"]["profile"] = "quiet"
            quiet_plan = plan_apply_actions(quiet_cfg)
            check(
                "plan_apply_actions skips fan.sh under quiet profile",
                all(s != "fan.sh" for s, _ in quiet_plan),
                str(quiet_plan),
            )

            perf_auto_cfg = deepcopy(rec_cfg)
            perf_auto_cfg["fan"]["mode"] = "auto"
            perf_auto_plan = plan_apply_actions(perf_auto_cfg)
            check(
                "plan_apply_actions skips explicit fan auto under performance while keeping offset",
                ("fan.sh", ["offset", "15"]) in perf_auto_plan
                and ("fan.sh", ["auto"]) not in perf_auto_plan,
                str(perf_auto_plan),
            )

            disabled_cfg = deepcopy(rec_cfg)
            disabled_cfg["auto_apply_on_launch"] = False
            check(
                "plan_apply_actions honors auto_apply_on_launch=False unless force=True",
                plan_apply_actions(disabled_cfg, force=False) == []
                and plan_apply_actions(disabled_cfg, force=True) == expected_plan,
            )

            off_rgb_cfg = default_config()
            off_rgb_cfg["keyboard"] = {
                "brightness": 0,
                "mode": "static",
                "color": [0, 0, 0],
                "cc_scale": False,
                "effect": None,
                "timeout_s": 0,
            }
            check(
                "plan_apply_actions formats RGB off and timeout off cleanly",
                plan_apply_actions(off_rgb_cfg)
                == [
                    ("brightness.sh", ["0"]),
                    ("rgb.sh", ["--timeout", "off"]),
                    ("rgb.sh", ["off"]),
                ],
            )
        finally:
            if old_env is None:
                os.environ.pop("ACER_CONTROL_CONFIG", None)
            else:
                os.environ["ACER_CONTROL_CONFIG"] = old_env
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg

    print(f"Config store self-test finished: {len(failures)} failure(s).")
    return 1 if failures else 0


def parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Acer Aspire A715-59G Config Store")
    parser.add_argument("--config", default=None, help="Explicit config.json path")
    parser.add_argument("--uid", type=int, default=None, help="Target user UID for path resolution")
    parser.add_argument("--cache-dir", default=None, help="Cache dir fallback / legacy seed source")
    parser.add_argument("--show", action="store_true", help="Load and print resolved config JSON")
    parser.add_argument("--plan", action="store_true", help="Print planned apply actions from config")
    parser.add_argument("--force", action="store_true", help="Include actions even if auto_apply_on_launch is false")
    parser.add_argument("--self-test", action="store_true", help="Run isolated self-tests inside TemporaryDirectory")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_cli(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return run_self_test()

    cfg_path = resolve_config_path(explicit=args.config, uid=args.uid, cache_dir=args.cache_dir)
    if not args.show and not args.plan:
        print(f"Resolved config path: {cfg_path}")
        return 0

    cfg = load_config(cfg_path, uid=args.uid)
    if args.show:
        print(json.dumps(cfg, indent=2))
    if args.plan:
        actions = plan_apply_actions(cfg, force=args.force)
        if not actions:
            print("(no actions planned)")
        else:
            for script, act_args in actions:
                print(f"{script} {' '.join(shlex.quote(x) for x in act_args)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
