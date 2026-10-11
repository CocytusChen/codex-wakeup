#!/usr/bin/env python3
"""Read-only local/SSH observation of existing turns and process identities."""

import argparse
import json
import re
import shlex
import subprocess
import time
from pathlib import Path

from .config import control_socket_path
from .rpc import RpcClient

TERMINAL = {"completed", "failed", "interrupted"}


def local_snapshot(thread, turn, pid=None, socket_path=None):
    with RpcClient(
        socket_path or control_socket_path(),
        timeout=20,
        max_bytes=2 * 1024 * 1024,
    ) as client:
        client.rpc("thread/read", {"threadId": thread, "includeTurns": False})
        turns = (
            client.rpc(
                "thread/turns/list",
                {"threadId": thread, "limit": 16, "itemsView": "notLoaded"},
            )["data"]
            if turn
            else []
        )
    selected = next((item for item in turns if item["id"] == turn), None)
    if turn and selected is None:
        raise ValueError("Exact turn unavailable in bounded recent metadata; inspect manually.")
    selected = selected or {"status": "inProgress"}
    result = {
        "turn": turn,
        "status": selected["status"],
        "error_present": bool(selected.get("error")),
    }
    if pid is not None:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            result["process"] = {
                "pid": pid,
                "start_ticks": fields[19],
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "state": fields[0],
            }
        except FileNotFoundError:
            result["process"] = None
    return result


def snapshot(args):
    if not args.host:
        return local_snapshot(args.thread, args.turn, args.pid, args.socket)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]*", args.host):
        raise ValueError("Invalid SSH host/alias.")
    remote_args = [args.remote_command, "probe", "--thread", args.thread]
    if args.turn:
        remote_args += ["--turn", args.turn]
    if args.pid is not None:
        remote_args += ["--pid", str(args.pid)]
    if args.remote_socket:
        remote_args += ["--socket", args.remote_socket]
    remote = shlex.join(remote_args)
    response = subprocess.run(
        ["ssh", "-oBatchMode=yes", "-oConnectTimeout=12", args.host, remote],
        capture_output=True,
        text=True,
        timeout=35,
    )
    if response.returncode:
        raise RuntimeError("Remote observation failed; experiment state is unknown, not failed.")
    if len(response.stdout) > 16384:
        raise RuntimeError("Remote metadata exceeds bound.")
    value = json.loads(response.stdout)
    if not value.get("ok"):
        raise RuntimeError("Remote observation refused; inspect without assuming task failure.")
    return value["data"]


def event(initial, current):
    if current["status"] in TERMINAL or current["error_present"]:
        return "owner_turn_terminal"
    if "process" in initial:
        old, now = initial["process"], current.get("process")
        if (
            now is None
            or now["state"] == "Z"
            or any(old[key] != now[key] for key in ("pid", "start_ticks", "boot_id"))
        ):
            return "original_process_ended"
    if current["status"] != "inProgress":
        raise RuntimeError("Unknown turn status; inspect without assuming completion.")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["probe", "wait"])
    parser.add_argument("--thread", required=True)
    parser.add_argument("--turn")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--host")
    parser.add_argument("--socket", help="Local Codex Unix control socket path.")
    parser.add_argument(
        "--remote-command",
        default="codex-wakeup-wait",
        help="Installed helper executable on the SSH host.",
    )
    parser.add_argument("--remote-socket", help="Optional remote Codex socket path.")
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    try:
        if (
            not (args.turn or args.pid)
            or not 5 <= args.interval <= 3600
            or (args.pid is not None and args.pid <= 0)
        ):
            raise ValueError(
                "Exact turn or PID, interval5..3600s and a positive optional PID required."
            )
        initial = snapshot(args)
        if args.pid is not None and (
            initial["process"] is None or initial["process"]["state"] == "Z"
        ):
            raise ValueError("Monitored process must be running at preflight.")
        result = initial
        if args.action == "wait":
            while True:
                reason = event(initial, result)
                if reason:
                    result = {
                        "event": reason,
                        "observed": result,
                        "business_success": "unassessed",
                    }
                    break
                time.sleep(args.interval)
                result = snapshot(args)
        print(json.dumps({"ok": True, "action": args.action, "data": result}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "action": args.action, "error": str(exc)[:500]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
