"""Command/exit triggers sharing the existing validated wake-up delivery engine."""

import os
import select
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from .engine import (
    DetachedProcess,
    ToolError,
    process_identity,
    schedule,
    timestamp,
)

OUTPUT_LIMIT = 65536
MESSAGE_LIMIT = 1024
TERMINAL = {"exited", "timed_out", "failed", "unknown", "cancelled"}


def identity(pid):
    """Include boot and start identity; a zombie is observable but no longer running."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ToolError("invalid_pid", "PID must be a positive integer on this host.")
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {"pid": pid, "start_ticks": fields[19], "boot_id": boot, "state": fields[0]}
    except FileNotFoundError:
        return None
    except (OSError, IndexError) as exc:
        raise ToolError("process_unreadable", "Cannot establish the process identity.") from exc


def same_process(expected, actual):
    return bool(actual) and all(expected[k] == actual[k] for k in ("pid", "start_ticks", "boot_id"))


def submit(
    store,
    factory,
    *,
    thread,
    socket_path,
    instructions,
    trigger,
    title="Background task",
    busy="queue",
    dry_run=False,
    manager=None,
):
    """Validate everything before starting either the worker or submitted command."""
    kind = trigger.get("type")
    trigger = dict(trigger)
    if kind == "command":
        argv = trigger.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(x, str) or "\0" in x for x in argv)
            or not argv[0]
            or sum(len(x.encode()) for x in argv) > 65536
        ):
            raise ToolError("invalid_command", "Supply a nonempty argv array, without NUL.")
        cwd = Path(trigger.get("cwd", "")).expanduser()
        if not cwd.is_absolute() or not cwd.is_dir() or not os.access(cwd, os.R_OK | os.X_OK):
            raise ToolError(
                "invalid_command_cwd", "Command cwd must be an accessible absolute directory."
            )
        exe = str(cwd / argv[0]) if "/" in argv[0] and not Path(argv[0]).is_absolute() else argv[0]
        exe = shutil.which(exe)
        if not exe:
            raise ToolError(
                "executable_not_found", "The command executable is absent or not executable."
            )
        trigger["argv"] = [exe, *argv[1:]]
        trigger["cwd"] = str(cwd.resolve())
        mode = trigger.setdefault("output_mode", "message")
        limit = trigger.get("output_limit", MESSAGE_LIMIT)
        low, high = 1, 65536
        if isinstance(limit, bool) or not isinstance(limit, int) or not low <= limit <= high:
            raise ToolError("invalid_output_limit", f"Output limit must be {low}..{high} bytes.")
        trigger["output_limit"] = limit
        if mode is not None:
            if mode not in {"message", "file"}:
                raise ToolError("invalid_output_mode", "Output mode must be message or file.")
            if not 1 <= limit <= 65536:
                raise ToolError(
                    "invalid_output_limit", "Inline output limit must be 1..65536 bytes."
                )
            filename = trigger.get("output_file")
            if filename is not None:
                path = Path(filename).expanduser()
                if not path.is_absolute() or not path.parent.is_dir():
                    raise ToolError(
                        "invalid_output_file",
                        "Use an absolute output path in an existing directory.",
                    )
                if os.path.lexists(path) or not os.access(path.parent, os.W_OK | os.X_OK):
                    raise ToolError(
                        "invalid_output_file", "Output file must be new and its parent writable."
                    )
                trigger["output_file"] = str(path)
    elif kind == "process_exit":
        current = identity(trigger.get("pid"))
        if current is None or current["state"] == "Z":
            raise ToolError(
                "process_not_running", "The process must exist and be running at registration."
            )
        trigger["identity"] = current
    else:
        raise ToolError("invalid_trigger", "Trigger must be command or process_exit.")
    timeout = trigger.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (float, int))
        or not 0 < timeout <= 315360000
    ):
        raise ToolError(
            "invalid_timeout", "Optional timeout must be positive; omitted means no deadline."
        )
    manager = manager or DetachedProcess(store)
    job = schedule(
        store,
        manager,
        factory,
        thread=thread,
        socket_path=socket_path,
        instructions=instructions,
        after="1s",
        title=title,
        busy=busy,
        dry_run=True,
    )
    job.update(
        trigger=trigger,
        scheduled_at=datetime.now(UTC).isoformat(),
        execution={"status": "pending"},
        backend="process",
    )
    if dry_run:
        return job
    with store.lock(job["id"]):
        store.save(job)
        try:
            manager.install(job)
        except Exception:
            job["delivery"]["status"] = "registration_failed"
            job["execution"] = {"status": "failed", "reason": "worker_registration_failed"}
            store.save(job)
            manager.remove(job)
            raise
    return job


def update_execution(store, job_id, **fields):
    # Cancellation owns delivery.status; never write an old whole-job snapshot over it.
    with store.lock(job_id):
        job = store.read(job_id)
        job["execution"].update(fields)
        store.save(job)
        return job


def stop_owned_group(proc, expected):
    """The unreaped direct child retains its PID, preventing a group-identity race."""
    if not same_process(expected, identity(proc.pid)):
        return False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        time.sleep(0.15)
        if same_process(expected, identity(proc.pid)):
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


class OutputCapture:
    """Bounded inline output with complete-file overflow."""

    def __init__(self, store, job_id, trigger):
        self.mode = trigger.get("output_mode", "tail")
        self.limit = trigger["output_limit"]
        self.path = Path(trigger.get("output_file") or store.root / "output" / f"{job_id}.stdout")
        self.buffer = bytearray()
        self.total = 0
        self.stream = None
        if self.mode == "file":
            self.open_file()

    def open_file(self):
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.stream = os.fdopen(fd, "wb")

    def feed(self, chunk):
        self.total += len(chunk)
        if self.mode == "tail":
            self.buffer.extend(chunk)
            del self.buffer[: -self.limit]
        elif self.stream is not None:
            self.stream.write(chunk)
        elif len(self.buffer) + len(chunk) <= self.limit:
            self.buffer.extend(chunk)
        else:
            self.open_file()
            self.stream.write(self.buffer)
            self.buffer.clear()
            self.stream.write(chunk)

    def finish(self):
        # Replacement of invalid UTF-8 can enlarge the model-visible representation.
        text = self.buffer.decode("utf-8", errors="replace")
        if self.mode == "message" and self.stream is None and len(text.encode()) > self.limit:
            self.open_file()
            self.stream.write(self.buffer)
            self.buffer.clear()
        if self.mode == "tail":
            self.open_file()
            self.stream.write(self.buffer)
        persisted = self.stream is not None
        self.close()
        fields = {
            "output_mode": self.mode,
            "output_path": str(self.path) if persisted else None,
            "output_bytes_seen": self.total,
            "output_bytes_retained": len(self.buffer) if self.mode == "tail" else self.total,
            "output_truncated": self.mode == "tail" and self.total > len(self.buffer),
            "output_spilled": self.mode == "message" and persisted,
        }
        if self.mode == "message" and not persisted:
            fields["output_text"] = text
        return fields

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def execute_command(store, job_id, interrupted):
    job = store.read(job_id)
    trigger = job["trigger"]
    if interrupted[0] or job["delivery"]["status"] == "cancelled":
        update_execution(store, job_id, status="cancelled", finished_at=timestamp())
        return
    capture = OutputCapture(store, job_id, trigger)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            trigger["argv"],
            cwd=trigger["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        capture.close()
        raise
    expected = identity(proc.pid)
    if expected is None:
        raise ToolError("child_identity_missing", "Owned child identity is unavailable.")
    try:
        update_execution(store, job_id, status="running", started_at=timestamp(), process=expected)
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        reason = None
        while True:
            # WNOWAIT preserves the child identity until descendants are settled.
            exited = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if interrupted[0] or store.read(job_id)["delivery"]["status"] == "cancelled":
                reason = "cancelled"
            timeout = trigger.get("timeout_seconds")
            if timeout is not None and time.monotonic() - started >= timeout and exited is None:
                reason = "timed_out"
            if reason or exited is not None:
                break
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                chunk = os.read(fd, 65536)
                capture.feed(chunk)
                if not chunk:
                    time.sleep(0.05)
            else:
                time.sleep(0.01)
        # A command job owns its session. Do not leave detached children behind.
        group_settled = stop_owned_group(proc, expected)
        code = proc.wait(timeout=5)
        while True:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            capture.feed(chunk)
        update_execution(
            store,
            job_id,
            status=reason or "exited",
            finished_at=timestamp(),
            exit_code=code,
            elapsed_seconds=time.monotonic() - started,
            **capture.finish(),
            owned_group_settlement=group_settled,
        )
    finally:
        # Covers fixture failures or unexpected exceptions without signalling peers.
        if proc.returncode is None:
            stop_owned_group(proc, expected)
            proc.wait(timeout=5)
        proc.stdout.close()
        capture.close()


def execute_watch(store, job_id, interrupted):
    trigger = store.read(job_id)["trigger"]
    expected = trigger["identity"]
    started = time.monotonic()
    update_execution(store, job_id, status="watching", started_at=timestamp())
    while True:
        if interrupted[0] or store.read(job_id)["delivery"]["status"] == "cancelled":
            state, reason = "cancelled", "watch_cancelled_target_untouched"
            break
        actual = identity(expected["pid"])
        if not same_process(expected, actual) or actual["state"] == "Z":
            state, reason = "exited", "original_process_no_longer_running"
            break
        timeout = trigger.get("timeout_seconds")
        if timeout is not None and time.monotonic() - started >= timeout:
            state, reason = "timed_out", "watch_deadline_target_untouched"
            break
        time.sleep(0.25)
    update_execution(
        store,
        job_id,
        status=state,
        reason=reason,
        finished_at=timestamp(),
        exit_code=None,
        elapsed_seconds=time.monotonic() - started,
    )


def execute_trigger(store, job_id):
    with store.lock(job_id):
        job = store.read(job_id)
        state = job["execution"]["status"]
        if state in TERMINAL:
            return True
        runner = job["execution"].get("runner", {})
        if state != "pending":
            if runner.get("pid") and process_identity(runner["pid"]) == runner.get("start_ticks"):
                return False  # Another worker owns this execution; no premature delivery.
            job["execution"].update(
                status="unknown",
                reason="worker_restarted_after_possible_execution",
                finished_at=timestamp(),
            )
            store.save(job)
            return True  # Never rerun a possibly started script.
        if job["delivery"]["status"] == "cancelled":
            job["execution"].update(status="cancelled", finished_at=timestamp())
            store.save(job)
            return True
        job["execution"].update(
            status="starting",
            runner={"pid": os.getpid(), "start_ticks": process_identity(os.getpid())},
        )
        store.save(job)  # Admission marker precedes Popen, so crashes cannot replay the command.
    interrupted = [False]
    old = signal.signal(signal.SIGTERM, lambda *_args: interrupted.__setitem__(0, True))
    try:
        if job["trigger"]["type"] == "command":
            execute_command(store, job_id, interrupted)
        else:
            execute_watch(store, job_id, interrupted)
    except Exception as exc:
        update_execution(
            store,
            job_id,
            status="failed",
            reason=f"{type(exc).__name__}: {exc}"[:1000],
            finished_at=timestamp(),
        )
    finally:
        signal.signal(signal.SIGTERM, old)
    return True


def read_output(store, job_id, limit=4096):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 65536:
        raise ToolError("invalid_output_limit", "Read at most 1..65536 retained bytes.")
    job = store.read(job_id)
    execution = job.get("execution", {})
    if "output_text" in execution:
        value = execution["output_text"].encode()[-limit:]
        return {
            "job_id": job_id,
            "execution": execution,
            "available": True,
            "text": value.decode("utf-8", errors="replace"),
            "bytes_returned": len(value),
            "stored_tail_only": False,
        }
    path = Path(execution.get("output_path") or store.root / "output" / f"{job_id}.stdout")
    if not path.exists():
        return {"job_id": job_id, "execution": job.get("execution"), "available": False}
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        value = stream.read(limit)
    return {
        "job_id": job_id,
        "execution": job.get("execution"),
        "available": True,
        "text": value.decode("utf-8", errors="replace"),
        "bytes_returned": len(value),
        "stored_tail_only": execution.get("output_mode", "tail") == "tail",
    }
