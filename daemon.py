#!/usr/bin/env python3
"""Privileged EC/ACPI control daemon for Acer Aspire A715-59G (Board RTH)."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import grp
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from typing import Any, Mapping, Optional

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import config_store  # noqa: E402
DEFAULT_SOCKET_PATH = "/run/acer-a715-59g-control.sock"
DEFAULT_CACHE_DIR = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
DEFAULT_UID = 1000
DEFAULT_GID = 1000
DAEMON_VERSION = "1.0"
MAX_REQUEST_BYTES = 65536

ALLOWED_SCRIPTS = frozenset({
    "power_profile.sh",
    "fan.sh",
    "brightness.sh",
    "rgb.sh",
    "battery.sh",
    "peripherals.sh",
})

EC_IO_PATH = "/sys/kernel/debug/ec/ec0/io"
EC_SYS_MODULE_DIR = "/sys/module/ec_sys"
EC_SYS_WRITE_SUPPORT = "/sys/module/ec_sys/parameters/write_support"

DEFAULT_FAN_CURVE = (40, 50, 60, 60, 80, 70, 100, 100)

# Global lock serializing all EC mailbox / XRAM / ACPI hardware operations.
EC_EXEC_LOCK = threading.Lock()


def _read_sysfs_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            return fp.read().strip()
    except OSError:
        return None


def _resolve_tool(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


def _resolve_allowed_uids(target_uid: int) -> set[int]:
    """Return UIDs authorized to connect: root (0), target_uid, and sudo/adm members."""
    allowed: set[int] = {0, int(target_uid)}
    target_gids: set[int] = set()
    for group_name in ("sudo", "adm"):
        try:
            gr = grp.getgrnam(group_name)
        except KeyError:
            continue
        target_gids.add(gr.gr_gid)
        for member in gr.gr_mem:
            try:
                allowed.add(pwd.getpwnam(member).pw_uid)
            except KeyError:
                continue
    if target_gids:
        for entry in pwd.getpwall():
            if entry.pw_gid in target_gids:
                allowed.add(entry.pw_uid)
    return allowed


def _ensure_cache_root_owned(cache_dir: str, uid: int, gid: int) -> None:
    """Ensure <cache_dir> exists with target user ownership when running as root."""
    if not cache_dir:
        return
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
        if os.geteuid() == 0:
            try:
                os.chown(cache_dir, uid, gid, follow_symlinks=False)
            except OSError:
                pass


def _chown_cache_tree(cache_dir: Optional[str], uid: Optional[int], gid: Optional[int]) -> None:
    """Recursively restore ownership of <cache_dir>/acer-a715-59g to uid:gid."""
    if not cache_dir or uid is None or gid is None or os.geteuid() != 0:
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


# ---------------------------------------------------------------------------
# Cache State Parsers & Boot Initialization
# ---------------------------------------------------------------------------

def parse_brightness_state(text: str) -> Optional[list[str]]:
    """Parse brightness_state cache into arguments for brightness.sh, or None if invalid."""
    stripped = text.strip()
    if not stripped:
        return None
    m = re.search(r"\bbrightness=(\d+)\b", stripped)
    if m:
        level = int(m.group(1))
    elif re.fullmatch(r"\d+", stripped):
        level = int(stripped)
    else:
        return None
    if not (0 <= level <= 255):
        return None
    return [str(level)]


def parse_rgb_state(text: str) -> list[list[str]]:
    """Parse rgb_state cache into zero or more argument lists for rgb.sh."""
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        return []
    joined = " ".join(lines)
    cmds: list[list[str]] = []

    m_mode = re.search(r"\bmode=([A-Za-z0-9_-]+)\b", joined)
    mode_val = m_mode.group(1).lower() if m_mode else None

    m_eff = re.search(r"\beffect=([A-Za-z0-9_-]+)\b", joined)
    if m_eff:
        if mode_val not in (None, "effect"):
            return []
        eff_name = m_eff.group(1).lower()
        if eff_name == "breath":
            cmds.append(["--effect", "breath"])
    else:
        cc_scale = bool(re.search(r"\bcc_scale=1\b", joined))
        m_color = re.search(r"\bcolor=\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", joined)
        rgb_vals: Optional[tuple[int, int, int]] = None
        if m_color and mode_val in (None, "static"):
            rgb_vals = (int(m_color.group(1)), int(m_color.group(2)), int(m_color.group(3)))
        elif mode_val in (None, "static"):
            mr = re.search(r"\br=(\d+)\b", joined)
            mg = re.search(r"\bg=(\d+)\b", joined)
            mb = re.search(r"\bb=(\d+)\b", joined)
            if mr and mg and mb:
                rgb_vals = (int(mr.group(1)), int(mg.group(1)), int(mb.group(1)))
        if rgb_vals is not None:
            r, g, b = rgb_vals
            if 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255:
                cmd = ["--cc-scale"] if cc_scale else []
                cmd.extend([str(r), str(g), str(b)])
                cmds.append(cmd)

    m_to = re.search(r"\btimeout=([A-Za-z0-9_-]+)\b", joined)
    if m_to:
        to_raw = m_to.group(1).lower()
        if to_raw in ("off", "0"):
            cmds.append(["--timeout", "off"])
        elif to_raw.isdigit():
            to_sec = int(to_raw)
            if 5 <= to_sec <= 65535:
                cmds.append(["--timeout", str(to_sec)])

    return cmds


def _validate_curve_nodes(vals: tuple[int, ...]) -> bool:
    if len(vals) != 8:
        return False
    t1, d1, t2, d2, t3, d3, t4, d4 = vals
    return (
        30 <= t1 < t2 < t3 < t4 <= 100
        and 0 <= d1 < d2 < d3 <= d4 <= 100
    )


def parse_fan_curve_state(text: str) -> tuple[Optional[list[str]], dict[str, Any]]:
    """
    Parse and validate fan_curve_state.

    Note: fan.sh writes only `cpu=...` and `gpu=...` without recording active
    fan mode (0xD7). To avoid unexpectedly overriding BIOS Auto mode (0x02)
    on boot, custom curves are only restored automatically if the cache file
    explicitly sets `active=custom` / `mode=custom` / `enabled=1`.
    """
    curves: dict[str, tuple[int, ...]] = {}
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
            if len(tokens) != 8 or not all(re.fullmatch(r"\d+", t) for t in tokens):
                return (None, {"valid": False, "reason": f"invalid {key} curve syntax"})
            nums = tuple(int(t) for t in tokens)
            if not _validate_curve_nodes(nums):
                return (None, {"valid": False, "reason": f"invalid {key} curve monotonicity/bounds"})
            curves[key] = nums
        elif key == "active" and val.lower() in ("custom", "1", "true"):
            active_custom = True
        elif key == "mode" and val.lower() == "custom":
            active_custom = True
        elif key == "enabled" and val == "1":
            active_custom = True

    if "cpu" not in curves or "gpu" not in curves:
        return (None, {"valid": False, "reason": "missing cpu or gpu curve"})

    cpu = curves["cpu"]
    gpu = curves["gpu"]
    is_default = cpu == DEFAULT_FAN_CURVE and gpu == DEFAULT_FAN_CURVE
    meta: dict[str, Any] = {
        "valid": True,
        "cpu": cpu,
        "gpu": gpu,
        "is_default": is_default,
        "active_custom": active_custom,
    }
    if not active_custom:
        meta["reason"] = (
            "fan_curve_state stores points only (no active=custom flag); "
            "skipping boot force of Custom (0x04) over BIOS Auto mode"
        )
        return (None, meta)

    args = [
        "custom",
        str(cpu[2]), str(cpu[3]), str(cpu[4]), str(cpu[5]),
        str(gpu[2]), str(gpu[3]), str(gpu[4]), str(gpu[5]),
    ]
    return (args, meta)


def plan_boot_restore_actions(cache_dir: str) -> tuple[list[tuple[str, list[str], str]], list[str]]:
    """Inspect cached state files and build validated restore actions + skip notes."""
    state_dir = os.path.join(cache_dir, "acer-a715-59g")
    actions: list[tuple[str, list[str], str]] = []
    skipped: list[str] = []

    if not os.path.isdir(state_dir):
        return (actions, [f"cache dir absent: {state_dir}"])

    def _read_cache(name: str) -> Optional[str]:
        path = os.path.join(state_dir, name)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fp:
                return fp.read()
        except OSError as exc:
            skipped.append(f"{name}: unreadable ({exc})")
            return None

    bri_raw = _read_cache("brightness_state")
    if bri_raw is not None:
        bri_args = parse_brightness_state(bri_raw)
        if bri_args:
            actions.append(("brightness.sh", bri_args, "brightness_state"))
        else:
            skipped.append("brightness_state: invalid/out-of-range brightness")

    rgb_raw = _read_cache("rgb_state")
    if rgb_raw is not None:
        rgb_cmds = parse_rgb_state(rgb_raw)
        if rgb_cmds:
            for rgb_args in rgb_cmds:
                actions.append(("rgb.sh", rgb_args, "rgb_state"))
        else:
            skipped.append("rgb_state: invalid/unrecognized RGB state")

    fan_raw = _read_cache("fan_curve_state")
    if fan_raw is not None:
        fan_args, fan_meta = parse_fan_curve_state(fan_raw)
        if fan_args:
            actions.append(("fan.sh", fan_args, "fan_curve_state"))
        else:
            skipped.append(f"fan_curve_state: {fan_meta.get('reason', 'skipped')}")

    return (actions, skipped)


def _run_module_cmd(cmd: list[str], dry_run: bool) -> tuple[bool, str]:
    if dry_run:
        return (True, f"[dry-run] {' '.join(shlex.quote(x) for x in cmd)}")
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"rc={proc.returncode}"
        return (False, f"{' '.join(cmd)} failed: {detail}")
    return (True, f"ran {' '.join(cmd)}")


def ensure_kernel_modules(dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Ensure ec_sys (write_support=1) is loaded."""
    actions: list[str] = []
    errors: list[str] = []

    modprobe = _resolve_tool("modprobe", "/usr/sbin/modprobe")

    has_ec_mod = os.path.exists(EC_SYS_MODULE_DIR)
    write_support = _read_sysfs_text(EC_SYS_WRITE_SUPPORT)
    try:
        os.stat(EC_IO_PATH)
        has_ec_io = True
    except FileNotFoundError:
        has_ec_io = False
    except PermissionError:
        # /sys/kernel/debug is 0700 root:root; non-root dry-run cannot stat inside it.
        has_ec_io = has_ec_mod and write_support == "Y"

    if has_ec_mod and has_ec_io and write_support == "Y":
        actions.append("ec_sys: already loaded with write_support=1")
    else:
        if has_ec_mod:
            ok, msg = _run_module_cmd([modprobe, "-r", "ec_sys"], dry_run=dry_run)
            (actions if ok else errors).append(msg)
        ok, msg = _run_module_cmd([modprobe, "ec_sys", "write_support=1"], dry_run=dry_run)
        (actions if ok else errors).append(msg)

    return (actions, errors)


def run_script_locked(
    script: str,
    args: list[str],
    cache_dir: str,
    uid: int,
    gid: int,
    exec_lock: threading.Lock = EC_EXEC_LOCK,
) -> tuple[int, str, str]:
    """Execute an allowed script under exec_lock with isolated XDG_CACHE_HOME."""
    if script not in ALLOWED_SCRIPTS:
        return (1, "", f"Disallowed script: {script}")
    if not all(isinstance(a, str) for a in args):
        return (1, "", "Arguments must be strings.")

    script_path = os.path.join(SCRIPT_DIR, script)
    env = os.environ.copy()
    if cache_dir:
        env["XDG_CACHE_HOME"] = cache_dir

    with exec_lock:
        if cache_dir:
            _ensure_cache_root_owned(cache_dir, uid, gid)
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
        return (proc.returncode, proc.stdout, proc.stderr)


def _deep_merge_dict(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(dict(base))
    for k, v in patch.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, Mapping):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def run_boot_init(
    cache_dir: str,
    uid: int = DEFAULT_UID,
    gid: int = DEFAULT_GID,
    restore: bool = True,
    dry_run: bool = False,
    config_path: Optional[str] = None,
) -> dict[str, Any]:
    """Perform kernel module initialization and optional config-backed state restoration."""
    mod_actions, mod_errors = ensure_kernel_modules(dry_run=dry_run)
    restored: list[str] = []
    skipped: list[str] = []
    errors: list[str] = list(mod_errors)

    resolved_cfg_path = str(
        config_store.resolve_config_path(explicit=config_path, uid=uid, cache_dir=cache_dir)
    )
    cfg: dict[str, Any]
    try:
        if dry_run:
            if os.path.exists(resolved_cfg_path):
                cfg = config_store.load_config(resolved_cfg_path, uid=uid, gid=gid)
            else:
                cfg = config_store.seed_from_legacy_cache(cache_dir)
        else:
            cfg = config_store.ensure_config_file(
                resolved_cfg_path,
                cache_dir=cache_dir,
                uid=uid,
                gid=gid,
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"config ({resolved_cfg_path}): {exc}")
        cfg = config_store.default_config()

    if restore:
        if not cfg.get("auto_apply_on_launch", True):
            skipped.append("auto_apply_on_launch is false; skipped config restore")
        else:
            planned = config_store.plan_apply_actions(cfg, force=False)
            fan_cfg = cfg.get("fan", {})
            if fan_cfg.get("mode") is None and (
                fan_cfg.get("cpu_curve") is not None or fan_cfg.get("gpu_curve") is not None
            ):
                skipped.append(
                    "fan_curve_state: curve points stored without active=custom; skipped forcing Custom fan mode"
                )
            for script, args in planned:
                if dry_run:
                    restored.append(f"[dry-run] config: {script} {' '.join(args)}")
                    continue
                rc, _out, err = run_script_locked(script, args, cache_dir, uid, gid)
                if rc == 0:
                    restored.append(f"config: {script} {' '.join(args)}")
                else:
                    errors.append(f"config ({script} {' '.join(args)}): {err.strip() or f'rc={rc}'}")
    else:
        skipped.append("--no-restore set; skipped config restore")

    return {
        "config_path": resolved_cfg_path,
        "modules": mod_actions,
        "restored": restored,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Threaded UNIX Domain Socket Server
# ---------------------------------------------------------------------------

class DaemonServer:
    """Thread-safe AF_UNIX JSON IPC server serializing EC access via EC_EXEC_LOCK."""

    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET_PATH,
        uid: int = DEFAULT_UID,
        gid: int = DEFAULT_GID,
        cache_dir: str = DEFAULT_CACHE_DIR,
        config_path: Optional[str] = None,
        exec_lock: Optional[threading.Lock] = None,
    ) -> None:
        self.socket_path = socket_path
        self.uid = int(uid)
        self.gid = int(gid)
        self.cache_dir = cache_dir
        self.config_path = str(
            config_store.resolve_config_path(explicit=config_path, uid=self.uid, cache_dir=self.cache_dir)
        )
        self._exec_lock = exec_lock if exec_lock is not None else EC_EXEC_LOCK
        self.allowed_uids = _resolve_allowed_uids(self.uid)
        self._server_sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._active_conns: set[socket.socket] = set()
        self._client_threads: set[threading.Thread] = set()
        self._thread: Optional[threading.Thread] = None

        # Telemetry for verifying EC_EXEC_LOCK serialization in tests
        self._critical_active = 0
        self.max_concurrent_exec = 0

    def is_peer_authorized(self, peer_uid: int) -> bool:
        return peer_uid == 0 or peer_uid == self.uid or peer_uid in self.allowed_uids

    def bind(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.socket_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.lexists(self.socket_path):
            st = os.lstat(self.socket_path)
            if stat.S_ISDIR(st.st_mode):
                raise IsADirectoryError(f"Socket path is a directory: {self.socket_path}")
            os.unlink(self.socket_path)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o117)
        try:
            sock.bind(self.socket_path)
        finally:
            os.umask(old_umask)

        os.chmod(self.socket_path, 0o660)
        if os.geteuid() == 0:
            sock_gid = self.gid
            try:
                sock_gid = grp.getgrnam("sudo").gr_gid
            except KeyError:
                pass
            os.chown(self.socket_path, 0, sock_gid)
        else:
            try:
                os.chown(self.socket_path, -1, self.gid)
            except OSError:
                pass

        sock.listen(32)
        sock.settimeout(0.5)
        self._server_sock = sock

    def start_in_thread(self) -> threading.Thread:
        if self._server_sock is None:
            self.bind()
        self._stop_event.clear()
        thread = threading.Thread(target=self.serve_forever, name="AcerDaemonServer", daemon=True)
        self._thread = thread
        thread.start()
        return thread

    def request_stop(self) -> None:
        self._stop_event.set()

    def serve_forever(self) -> None:
        if self._server_sock is None:
            self.bind()
        assert self._server_sock is not None

        try:
            while not self._stop_event.is_set():
                try:
                    conn, _addr = self._server_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        break
                    continue

                try:
                    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    _pid, peer_uid, _peer_gid = struct.unpack("iII", creds)
                except OSError:
                    conn.close()
                    continue

                if not self.is_peer_authorized(peer_uid):
                    sys.stderr.write(f"Rejected unauthorized peer uid={peer_uid}\n")
                    conn.close()
                    continue

                t = threading.Thread(
                    target=self._handle_client,
                    args=(conn,),
                    daemon=True,
                )
                with self._state_lock:
                    self._active_conns.add(conn)
                    self._client_threads.add(t)
                t.start()
        finally:
            self._cleanup_socket_only()

    def _execute_script(self, script: str, args: list[str]) -> tuple[int, str, str]:
        script_path = os.path.join(SCRIPT_DIR, script)
        env = os.environ.copy()
        if self.cache_dir:
            env["XDG_CACHE_HOME"] = self.cache_dir

        with self._exec_lock:
            self._critical_active += 1
            if self._critical_active > self.max_concurrent_exec:
                self.max_concurrent_exec = self._critical_active
            try:
                if self.cache_dir:
                    _ensure_cache_root_owned(self.cache_dir, self.uid, self.gid)
                proc = subprocess.run(
                    [script_path, *args],
                    cwd=SCRIPT_DIR,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                _chown_cache_tree(self.cache_dir, self.uid, self.gid)
                return (proc.returncode, proc.stdout, proc.stderr)
            finally:
                self._critical_active -= 1

    def _apply_config_actions(self, force: bool = True, dry_run: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
        cfg = config_store.ensure_config_file(
            self.config_path,
            cache_dir=self.cache_dir,
            uid=self.uid,
            gid=self.gid,
        )
        planned = config_store.plan_apply_actions(cfg, force=force)
        applied: list[dict[str, Any]] = []
        errors: list[str] = []
        for script, act_args in planned:
            if dry_run:
                applied.append({"script": script, "args": act_args, "rc": 0, "dry_run": True})
                continue
            rc, out, err = self._execute_script(script, act_args)
            applied.append({"script": script, "args": act_args, "rc": rc, "stdout": out, "stderr": err})
            if rc != 0:
                detail = err.strip() or out.strip() or f"rc={rc}"
                errors.append(f"{script} {' '.join(act_args)}: {detail}")
        return applied, errors

    def _process_request(self, raw_line: str) -> dict[str, Any]:
        req_id: Any = None
        try:
            req = json.loads(raw_line)
            if not isinstance(req, dict):
                raise ValueError("Request must be a JSON object.")
            req_id = req.get("id")

            if req.get("ping"):
                return {
                    "id": req_id,
                    "rc": 0,
                    "stdout": "pong",
                    "stderr": "",
                    "euid": os.geteuid(),
                    "version": DAEMON_VERSION,
                }

            if req.get("get_config"):
                cfg = config_store.ensure_config_file(
                    self.config_path,
                    cache_dir=self.cache_dir,
                    uid=self.uid,
                    gid=self.gid,
                )
                return {
                    "id": req_id,
                    "rc": 0,
                    "stdout": json.dumps(cfg),
                    "stderr": "",
                    "config": cfg,
                    "config_path": self.config_path,
                }

            if "set_config" in req and req["set_config"] is not None and req["set_config"] is not False:
                raw_patch = req.get("config") if req["set_config"] is True else req["set_config"]
                if not isinstance(raw_patch, Mapping):
                    raise ValueError("Field 'set_config' must be a JSON object.")
                if req.get("replace"):
                    candidate: Mapping[str, Any] = raw_patch
                else:
                    current = config_store.ensure_config_file(
                        self.config_path,
                        cache_dir=self.cache_dir,
                        uid=self.uid,
                        gid=self.gid,
                    )
                    candidate = _deep_merge_dict(current, raw_patch)
                saved = config_store.save_config(
                    self.config_path,
                    candidate,
                    uid=self.uid,
                    gid=self.gid,
                )
                return {
                    "id": req_id,
                    "rc": 0,
                    "stdout": json.dumps(saved),
                    "stderr": "",
                    "config": saved,
                    "config_path": self.config_path,
                }

            if req.get("apply_config"):
                ap_val = req.get("apply_config")
                ap_opts = ap_val if isinstance(ap_val, Mapping) else {}
                force = bool(req["force"]) if "force" in req else bool(ap_opts.get("force", True))
                dry_run = bool(req["dry_run"]) if "dry_run" in req else bool(ap_opts.get("dry_run", False))
                applied, errors = self._apply_config_actions(force=force, dry_run=dry_run)
                summary_lines = [
                    f"{'[dry-run] ' if dry_run else ''}{item['script']} {' '.join(item['args'])} (rc={item['rc']})"
                    for item in applied
                ]
                return {
                    "id": req_id,
                    "rc": 0 if not errors else 1,
                    "stdout": "\n".join(summary_lines),
                    "stderr": "\n".join(errors),
                    "applied": applied,
                    "errors": errors,
                    "config_path": self.config_path,
                }

            script = req.get("script")
            args = req.get("args", [])
            if not isinstance(script, str) or script not in ALLOWED_SCRIPTS:
                raise ValueError(f"Disallowed or invalid script: {script!r}")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ValueError("Field 'args' must be a list of strings.")

            rc, out, err = self._execute_script(script, args)
            if rc == 0:
                try:
                    config_store.record_action_file(
                        self.config_path,
                        script,
                        args,
                        uid=self.uid,
                        gid=self.gid,
                        cache_dir=self.cache_dir,
                    )
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(f"Warning: failed to record action {script} {args}: {exc}\n")
            return {
                "id": req_id,
                "rc": rc,
                "stdout": out,
                "stderr": err,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "id": req_id,
                "rc": 1,
                "stdout": "",
                "stderr": f"Daemon error: {exc}",
            }

    @staticmethod
    def _send_json_line(conn: socket.socket, payload: dict[str, Any]) -> None:
        raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        conn.sendall(raw)

    def _handle_client(self, conn: socket.socket) -> None:
        buf = bytearray()
        partial_timeouts = 0
        try:
            conn.settimeout(1.0)
            while not self._stop_event.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    if not buf:
                        continue
                    partial_timeouts += 1
                    if partial_timeouts >= 5:
                        self._send_json_line(
                            conn,
                            {"id": None, "rc": 1, "stdout": "", "stderr": "Daemon error: request timeout"},
                        )
                        break
                    continue
                except OSError:
                    break

                if not chunk:
                    if buf:
                        line = bytes(buf).decode("utf-8", errors="replace").strip()
                        if line:
                            resp = self._process_request(line)
                            self._send_json_line(conn, resp)
                    break

                partial_timeouts = 0
                buf.extend(chunk)
                while True:
                    nl_idx = buf.find(b"\n")
                    if nl_idx == -1:
                        if len(buf) > MAX_REQUEST_BYTES:
                            self._send_json_line(
                                conn,
                                {
                                    "id": None,
                                    "rc": 1,
                                    "stdout": "",
                                    "stderr": f"Daemon error: request exceeds {MAX_REQUEST_BYTES} bytes",
                                },
                            )
                            return
                        break
                    if nl_idx > MAX_REQUEST_BYTES:
                        self._send_json_line(
                            conn,
                            {
                                "id": None,
                                "rc": 1,
                                "stdout": "",
                                "stderr": f"Daemon error: request exceeds {MAX_REQUEST_BYTES} bytes",
                            },
                        )
                        return
                    line_bytes = bytes(buf[:nl_idx])
                    del buf[: nl_idx + 1]
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    resp = self._process_request(line)
                    self._send_json_line(conn, resp)
        except OSError:
            pass
        finally:
            with self._state_lock:
                self._active_conns.discard(conn)
                self._client_threads.discard(threading.current_thread())
            try:
                conn.close()
            except OSError:
                pass

    def _cleanup_socket_only(self) -> None:
        sock = self._server_sock
        self._server_sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if os.path.lexists(self.socket_path):
            try:
                st = os.lstat(self.socket_path)
                if not stat.S_ISDIR(st.st_mode):
                    os.unlink(self.socket_path)
            except OSError:
                pass

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._state_lock:
            conns = list(self._active_conns)
            threads = list(self._client_threads)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self._cleanup_socket_only()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
            self._thread = None
        for t in threads:
            if t is not threading.current_thread():
                t.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Self-Test Mode (--self-test)
# ---------------------------------------------------------------------------

def _ipc_roundtrip(socket_path: str, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        buf = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                raise EOFError("Daemon closed socket before newline")
            buf.extend(chunk)
            nl = buf.find(b"\n")
            if nl != -1:
                return json.loads(bytes(buf[:nl]).decode("utf-8"))


def run_self_test() -> int:
    """Verify cache parsers, boot-init dry logic, AF_UNIX IPC, lock serialization, and cleanup."""
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        status = "PASS" if cond else "FAIL"
        suffix = f" ({detail})" if detail else ""
        print(f"[{status}] {name}{suffix}")
        if not cond:
            failures.append(f"{name}: {detail}")

    # 1. Cache parsers
    check("parse_brightness_state standard", parse_brightness_state("brightness=143 (0x8F)\n") == ["143"])
    check("parse_brightness_state bare integer", parse_brightness_state("255\n") == ["255"])
    check("parse_brightness_state rejects >255", parse_brightness_state("brightness=300\n") is None)

    check("parse_rgb_state color", parse_rgb_state("color=(0,255,200)\n") == [["0", "255", "200"]])
    check(
        "parse_rgb_state cc_scale",
        parse_rgb_state("color=(0,255,200) ec=(0,127,100) cc_scale=1\n") == [["--cc-scale", "0", "255", "200"]],
    )
    check("parse_rgb_state effect breath", parse_rgb_state("effect=breath\n") == [["--effect", "breath"]])
    check(
        "parse_rgb_state mode=static / mode=effect",
        parse_rgb_state("mode=static r=10 g=20 b=30\n") == [["10", "20", "30"]]
        and parse_rgb_state("mode=effect effect=breath\n") == [["--effect", "breath"]],
    )
    check("parse_rgb_state timeout", parse_rgb_state("timeout=60\n") == [["--timeout", "60"]])
    check("parse_rgb_state timeout off", parse_rgb_state("timeout=off\n") == [["--timeout", "off"]])
    check("parse_rgb_state rejects bad timeout/color", parse_rgb_state("timeout=2 color=(300,0,0)\n") == [])

    fc_inactive_args, fc_inactive_meta = parse_fan_curve_state(
        "cpu=45,28,60,50,80,66,100,100\ngpu=45,28,60,58,80,66,100,100\n"
    )
    check(
        "parse_fan_curve_state validates points without forcing custom",
        fc_inactive_args is None and fc_inactive_meta.get("valid") is True and fc_inactive_meta.get("active_custom") is False,
        str(fc_inactive_meta),
    )
    fc_active_args, fc_active_meta = parse_fan_curve_state(
        "active=custom\ncpu=40,50,55,54,75,76,100,100\ngpu=40,50,62,61,82,84,100,100\n"
    )
    check(
        "parse_fan_curve_state active=custom emits 8-node args",
        fc_active_args == ["custom", "55", "54", "75", "76", "62", "61", "82", "84"]
        and fc_active_meta.get("valid") is True,
        str(fc_active_args),
    )
    fc_bad_args, _ = parse_fan_curve_state("active=custom\ncpu=40,50,75,60,60,70,100,100\ngpu=40,50,60,60,80,70,100,100\n")
    check("parse_fan_curve_state rejects non-monotonic temps", fc_bad_args is None)

    ec_snapshot = os.path.normpath(
        os.path.join(SCRIPT_DIR, "..", "portable_tools", "acpi_dump", "ec0_io.bin")
    )

    with tempfile.TemporaryDirectory(prefix="acer_daemon_selftest_") as tmp_dir:
        cache_root = os.path.join(tmp_dir, "cache")
        state_dir = os.path.join(cache_root, "acer-a715-59g")
        cfg_file = os.path.join(tmp_dir, "config", "config.json")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "brightness_state"), "w", encoding="utf-8") as fp:
            fp.write("brightness=191 (0xBF)\n")
        with open(os.path.join(state_dir, "rgb_state"), "w", encoding="utf-8") as fp:
            fp.write("color=(0,255,200) ec=(0,127,100) cc_scale=1\n")
        with open(os.path.join(state_dir, "fan_curve_state"), "w", encoding="utf-8") as fp:
            fp.write("cpu=45,28,60,50,80,66,100,100\ngpu=45,28,60,58,80,66,100,100\n")

        boot_dry = run_boot_init(
            cache_root,
            uid=os.getuid(),
            gid=os.getgid(),
            restore=True,
            dry_run=True,
            config_path=cfg_file,
        )
        check(
            "run_boot_init dry-run restore plan",
            len(boot_dry["restored"]) == 2
            and any("brightness.sh 191" in x for x in boot_dry["restored"])
            and any("rgb.sh --cc-scale 0 255 200" in x for x in boot_dry["restored"])
            and any("fan_curve_state" in x for x in boot_dry["skipped"])
            and not os.path.exists(cfg_file),
            str(boot_dry),
        )
        check(
            "ensure_kernel_modules loads ec_sys only (no WMI)",
            any("ec_sys" in x for x in boot_dry["modules"])
            and not any("wmi" in x.lower() for x in boot_dry["modules"])
            and not boot_dry["errors"],
            str(boot_dry["modules"]),
        )
        boot_norestore = run_boot_init(
            cache_root,
            uid=os.getuid(),
            gid=os.getgid(),
            restore=False,
            dry_run=True,
            config_path=cfg_file,
        )
        check(
            "run_boot_init --no-restore skips config restore",
            len(boot_norestore["restored"]) == 0 and any("--no-restore" in x for x in boot_norestore["skipped"]),
            str(boot_norestore),
        )

        # Verify auto_apply_on_launch=False skips config restore
        disabled_cfg_file = os.path.join(tmp_dir, "config", "disabled.json")
        disabled_cfg = config_store.seed_from_legacy_cache(cache_root)
        disabled_cfg["auto_apply_on_launch"] = False
        config_store.save_config(disabled_cfg_file, disabled_cfg, uid=os.getuid(), gid=os.getgid())
        boot_disabled = run_boot_init(
            cache_root,
            uid=os.getuid(),
            gid=os.getgid(),
            restore=True,
            dry_run=True,
            config_path=disabled_cfg_file,
        )
        check(
            "run_boot_init respects auto_apply_on_launch=False",
            len(boot_disabled["restored"]) == 0
            and any("auto_apply_on_launch is false" in x for x in boot_disabled["skipped"]),
            str(boot_disabled),
        )

        # 2. Live UNIX socket server test
        sock_path = os.path.join(tmp_dir, "test.sock")
        server = DaemonServer(
            socket_path=sock_path,
            uid=os.getuid(),
            gid=os.getgid(),
            cache_dir=cache_root,
            config_path=cfg_file,
        )
        server.start_in_thread()
        try:
            mode = stat.S_IMODE(os.lstat(sock_path).st_mode)
            check("Daemon socket permissions 0660", mode == 0o660, oct(mode))
            check(
                "Peer authorization checks root/self/unauthorized",
                server.is_peer_authorized(0)
                and server.is_peer_authorized(os.getuid())
                and not server.is_peer_authorized(65534),
            )

            pong = _ipc_roundtrip(sock_path, {"id": 1, "ping": True})
            check(
                "Daemon ping JSON IPC",
                pong.get("id") == 1
                and pong.get("rc") == 0
                and pong.get("stdout") == "pong"
                and pong.get("euid") == os.geteuid()
                and pong.get("version") == DAEMON_VERSION,
                str(pong),
            )

            pp_resp = _ipc_roundtrip(sock_path, {"id": 2, "script": "power_profile.sh", "args": ["--dry-run", "status"]})
            check(
                "Daemon power_profile.sh --dry-run status",
                pp_resp.get("id") == 2 and pp_resp.get("rc") == 0 and "[dry-run]" in pp_resp.get("stdout", ""),
                str(pp_resp),
            )
            gb_resp = _ipc_roundtrip(sock_path, {"id": 20, "script": "power_profile.sh", "args": ["--gpu-boost", "on"]})
            check(
                "Daemon power_profile.sh --gpu-boost notice",
                gb_resp.get("id") == 20
                and gb_resp.get("rc") == 0
                and "WMI communication is disabled" in gb_resp.get("stdout", ""),
                str(gb_resp),
            )

            if os.path.isfile(ec_snapshot):
                fan_resp = _ipc_roundtrip(
                    sock_path,
                    {"id": 3, "script": "fan.sh", "args": ["--ec-file", ec_snapshot, "status"]},
                )
                check(
                    "Daemon fan.sh --ec-file status",
                    fan_resp.get("id") == 3
                    and fan_resp.get("rc") == 0
                    and "CPU Temp" in fan_resp.get("stdout", ""),
                    f"rc={fan_resp.get('rc')}, err={fan_resp.get('stderr')!r}",
                )

            for bad_script in ("../evil.sh", "rm", "/bin/sh"):
                bad_resp = _ipc_roundtrip(sock_path, {"id": 4, "script": bad_script, "args": ["status"]})
                check(
                    f"Daemon rejects unknown script {bad_script!r}",
                    bad_resp.get("rc") != 0 and "Disallowed" in bad_resp.get("stderr", ""),
                    str(bad_resp),
                )

            bad_args_resp = _ipc_roundtrip(sock_path, {"id": 5, "script": "fan.sh", "args": "status"})
            check(
                "Daemon rejects non-list args",
                bad_args_resp.get("rc") != 0 and "args" in bad_args_resp.get("stderr", ""),
                str(bad_args_resp),
            )

            # Oversized line (> 65536 bytes) rejection
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as big_cli:
                big_cli.settimeout(3.0)
                big_cli.connect(sock_path)
                big_cli.sendall(b"{" + b"a" * (MAX_REQUEST_BYTES + 100) + b"\n")
                big_raw = big_cli.recv(4096).decode("utf-8", errors="replace").strip()
            big_resp = json.loads(big_raw) if big_raw else {}
            check(
                "Daemon rejects request > 65536 bytes",
                big_resp.get("rc") != 0 and "exceeds" in big_resp.get("stderr", ""),
                str(big_resp),
            )

            # Concurrent client requests serialized by EC_EXEC_LOCK
            def _worker_req(idx: int) -> dict[str, Any]:
                if os.path.isfile(ec_snapshot):
                    return _ipc_roundtrip(
                        sock_path,
                        {"id": 100 + idx, "script": "fan.sh", "args": ["--ec-file", ec_snapshot, "status"]},
                    )
                return _ipc_roundtrip(
                    sock_path,
                    {"id": 100 + idx, "script": "power_profile.sh", "args": ["--dry-run", "status"]},
                )

            with ThreadPoolExecutor(max_workers=4) as pool:
                conc_results = list(pool.map(_worker_req, range(4)))
            check(
                "Concurrent socket requests succeed & serialize under EC_EXEC_LOCK",
                all(r.get("rc") == 0 for r in conc_results) and server.max_concurrent_exec == 1,
                f"max_concurrent_exec={server.max_concurrent_exec}, rcs={[r.get('rc') for r in conc_results]}",
            )

            # 3. Config IPC (get_config / set_config / apply_config) & action recording
            get_resp = _ipc_roundtrip(sock_path, {"id": 200, "get_config": True})
            check(
                "Daemon get_config seeds and returns config.json",
                get_resp.get("rc") == 0
                and os.path.isfile(cfg_file)
                and get_resp.get("config", {}).get("keyboard", {}).get("brightness") == 191
                and get_resp.get("config", {}).get("keyboard", {}).get("cc_scale") is True,
                str(get_resp),
            )

            set_resp = _ipc_roundtrip(
                sock_path,
                {
                    "id": 201,
                    "set_config": {
                        "auto_apply_on_launch": False,
                        "power": {"profile": "perf"},
                        "fan": {"mode": "custom", "curve": [54, 52, 76, 74, 58, 56, 82, 84]},
                    },
                },
            )
            check(
                "Daemon set_config normalizes aliases, splits 8-int curve & persists",
                set_resp.get("rc") == 0
                and set_resp.get("config", {}).get("auto_apply_on_launch") is False
                and set_resp.get("config", {}).get("power", {}).get("profile") == "performance"
                and set_resp.get("config", {}).get("fan", {}).get("cpu_curve") == [54, 52, 76, 74]
                and set_resp.get("config", {}).get("fan", {}).get("gpu_curve") == [58, 56, 82, 84],
                str(set_resp),
            )

            bad_set = _ipc_roundtrip(sock_path, {"id": 202, "set_config": {"keyboard": {"brightness": 999}}})
            check(
                "Daemon set_config rejects out-of-bounds values",
                bad_set.get("rc") != 0 and "Daemon error" in bad_set.get("stderr", ""),
                str(bad_set),
            )

            ap_noforce = _ipc_roundtrip(sock_path, {"id": 203, "apply_config": {"force": False, "dry_run": True}})
            ap_force = _ipc_roundtrip(sock_path, {"id": 204, "apply_config": {"force": True, "dry_run": True}})
            check(
                "Daemon apply_config respects force=False vs force=True",
                ap_noforce.get("rc") == 0
                and ap_noforce.get("applied") == []
                and ap_force.get("rc") == 0
                and [x["script"] for x in ap_force.get("applied", [])]
                == ["power_profile.sh", "fan.sh", "brightness.sh", "rgb.sh"],
                f"noforce={ap_noforce}, force={ap_force}",
            )

            # Successful script call records into config.json; failed/ignored does not
            orig_exec = server._execute_script
            try:
                server._execute_script = lambda s, a: (0, "ok", "")  # type: ignore[method-assign]
                rec_ok = _ipc_roundtrip(sock_path, {"id": 205, "script": "battery.sh", "args": ["limit", "60", "80"]})
                server._execute_script = lambda s, a: (1, "", "simulated failure")  # type: ignore[method-assign]
                rec_fail = _ipc_roundtrip(sock_path, {"id": 206, "script": "battery.sh", "args": ["full"]})
            finally:
                server._execute_script = orig_exec  # type: ignore[method-assign]

            after_rec = config_store.load_config(cfg_file)
            check(
                "Daemon records rc=0 script calls and ignores rc!=0 script calls",
                rec_ok.get("rc") == 0
                and rec_fail.get("rc") == 1
                and after_rec["battery"] == {"mode": "limit", "start_pct": 60, "stop_pct": 80},
                str(after_rec["battery"]),
            )
        finally:
            server.shutdown()

        check("Clean shutdown unlinks socket", not os.path.lexists(sock_path), sock_path)

    print(f"Daemon self-test finished: {len(failures)} failure(s).")
    return 1 if failures else 0


def parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Acer Aspire A715-59G EC/ACPI Control Daemon")
    parser.add_argument("--socket", default=DEFAULT_SOCKET_PATH, help="UNIX domain socket path")
    parser.add_argument("--uid", type=int, default=DEFAULT_UID, help="Target desktop user UID")
    parser.add_argument("--gid", type=int, default=DEFAULT_GID, help="Target desktop user GID")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="XDG_CACHE_HOME path for cached states")
    parser.add_argument("--config", default=None, help="Path to unified config.json")
    parser.add_argument("--boot-init", action="store_true", help="Load modules and restore cached states before serving")
    parser.add_argument("--no-restore", action="store_true", help="Skip cached state restoration during --boot-init")
    parser.add_argument("--self-test", action="store_true", help="Run isolated daemon self-tests and exit")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_cli(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return run_self_test()

    resolved_cfg_path = str(
        config_store.resolve_config_path(explicit=args.config, uid=args.uid, cache_dir=args.cache_dir)
    )

    if args.boot_init:
        summary = run_boot_init(
            cache_dir=args.cache_dir,
            uid=args.uid,
            gid=args.gid,
            restore=not args.no_restore,
            dry_run=False,
            config_path=resolved_cfg_path,
        )
        for mod_msg in summary["modules"]:
            print(f"[boot-init] {mod_msg}", file=sys.stderr)
        for res_msg in summary["restored"]:
            print(f"[boot-init] restored {res_msg}", file=sys.stderr)
        for skip_msg in summary["skipped"]:
            print(f"[boot-init] skipped {skip_msg}", file=sys.stderr)
        for err_msg in summary["errors"]:
            print(f"[boot-init] warning: {err_msg}", file=sys.stderr)

    server = DaemonServer(
        socket_path=args.socket,
        uid=args.uid,
        gid=args.gid,
        cache_dir=args.cache_dir,
        config_path=resolved_cfg_path,
    )

    def _on_signal(_signum: int, _frame: Any) -> None:
        server.request_stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    server.bind()
    try:
        server.serve_forever()
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
