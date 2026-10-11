"""Portable defaults, with explicit overrides for each user's Codex installation."""

import os
from pathlib import Path


def control_socket_path():
    override = os.environ.get("CODEX_WAKEUP_SOCKET")
    if override:
        return Path(override).expanduser()
    codex_dir = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return codex_dir / "app-server-control" / "app-server-control.sock"


def state_directory():
    default = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "codex-wakeup"
    return Path(os.environ.get("CODEX_WAKEUP_STATE_DIR", str(default))).expanduser()
