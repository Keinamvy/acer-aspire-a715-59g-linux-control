# System Daemon and IPC Architecture

`daemon.py` runs as `root` under `acer-a715-59g-daemon.service`, serializes all EC/RAPL/GPU operations, and serves unprivileged desktop clients (`gui.py` and `tray.py`) over `/run/acer-a715-59g-control.sock`.

---

## Security & Concurrency Model

- **Socket Permissions & Peer Credentials:** `/run/acer-a715-59g-control.sock` is created with `chmod 0660` (`root:<gid>`). Every connection is checked with `SO_PEERCRED`: only UID `0`, the configured `--uid`, or members of `sudo`/`adm` (or target `--gid`) are accepted.
- **Script Allowlist:** Only the 6 suite scripts (`power_profile.sh`, `fan.sh`, `brightness.sh`, `rgb.sh`, `battery.sh`, `peripherals.sh`) can be executed by IPC clients (`ALLOWED_SCRIPTS`).
- **Global EC Execution Lock:** All boot-init steps, script calls, and config reapplies acquire `EC_EXEC_LOCK` (`threading.Lock()`) so GUI polling, Tray actions, and user clicks never race on EC mailbox `0xF8..0xFD`.
- **Ownership Preservation:** After script execution or config writes as `root`, `daemon.py` restores `--uid:--gid` ownership on the cache directory and `config.json`.

---

## JSON IPC Protocol (Line-Delimited over AF_UNIX)

Supported client requests (`MAX_REQUEST_BYTES = 65536`):
- **Ping:** `{"id": 1, "ping": true}` → `{"id": 1, "rc": 0, "stdout": "pong", "euid": 0, "version": "1.0"}`
- **Run Script:** `{"id": 2, "script": "fan.sh", "args": ["status"]}` → executes under `EC_EXEC_LOCK` and records `rc == 0` mutations to `config.json`
- **Read Config:** `{"id": 3, "cmd": "get_config"}`
- **Update Config:** `{"id": 4, "cmd": "set_config", "config": {"auto_apply_on_launch": false}}` (deep-merge by default, or `"replace": true`)
- **Apply Config:** `{"id": 5, "cmd": "apply_config", "force": true}`

---

## Fallbacks When Daemon Is Absent or Outdated

- **No Daemon Running:** `gui.py` offers one-time **`Unlock Live EC (pkexec)`** (`--worker` privileged stdin/stdout JSON pipe). On startup without daemon/worker, GUI/Tray skip auto-apply cleanly instead of triggering multiple `pkexec` prompts.
- **Legacy Daemon Fallback:** If an older in-memory daemon rejects `get_config`/`apply_config`, `ScriptRunner` automatically falls back to local `config_store` read/write plus sequential `{"script", "args"}` IPC replay.
