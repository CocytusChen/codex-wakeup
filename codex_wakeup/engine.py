"""Detached job lifecycle and native Codex queue/steer delivery."""

import fcntl
import hashlib
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .rpc import RpcClient, RpcError

RETRY = 75
DELIVERY_TERMINAL = {
    "accepted",
    "uncertain",
    "rejected",
    "failed",
    "cancelled",
    "registration_failed",
}
INSTRUCTION_LIMIT = 65536
JOB_PATTERN = re.compile(r"[a-z0-9-]{1,64}\Z")


class ToolError(Exception):
    def __init__(self, code, message, retryable=False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


def timestamp():
    return datetime.now(UTC).isoformat()


def duration(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h|d)", value)
    if not match:
        raise ToolError("invalid_duration", "Use a positive duration such as 2m, 3h or 1d.")
    seconds = float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    if not 1 <= seconds <= 315360000:
        raise ToolError("invalid_duration", "Duration must be between 1 second and 10 years.")
    return seconds


def absolute_time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(
            "invalid_time", "Use an ISO8601 datetime with an explicit UTC offset."
        ) from exc
    if result.tzinfo is None:
        raise ToolError("ambiguous_time", "The time must include Z or a UTC offset, e.g. +02:00.")
    return result.astimezone(UTC)


def instruction_text(message=None, filename=None):
    try:
        text = message if message is not None else Path(filename).expanduser().read_text("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ToolError(
            "invalid_instructions", "Instruction file must exist and be readable UTF-8."
        ) from exc
    if not text.strip() or "\0" in text or len(text.encode()) > INSTRUCTION_LIMIT:
        raise ToolError(
            "invalid_instructions",
            "Instructions must be nonempty UTF-8, without NUL, at most 65536 bytes.",
        )
    return text


def inspect_thread(client, thread_id, *, read_active=True):
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or len(thread_id) > 256
        or any(c.isspace() for c in thread_id)
    ):
        raise ToolError(
            "invalid_thread", "Supply a nonempty existing thread ID without whitespace."
        )
    try:
        thread = client.rpc("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
        turns = (
            client.rpc(
                "thread/turns/list", {"threadId": thread_id, "limit": 1, "itemsView": "notLoaded"}
            )["data"]
            if read_active
            else []
        )
    except RpcError as exc:
        raise ToolError("thread_unavailable", f"Target thread cannot be read: {exc}") from exc
    if thread.get("ephemeral") or thread.get("archived"):
        raise ToolError("invalid_thread", "Target must be a durable, unarchived thread.")
    state = thread.get("status", {})
    if state.get("type") == "systemError":
        raise ToolError("thread_unavailable", "Target thread currently reports a system error.")
    cwd = thread.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_dir() or not os.access(cwd, os.R_OK | os.X_OK):
        raise ToolError("invalid_cwd", "Target cwd must exist and be accessible on this host.")
    active = next((turn for turn in turns if turn.get("status") == "inProgress"), None)
    return {"id": thread_id, "name": thread.get("name"), "cwd": cwd, "status": state}, active


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def writable_destination(path):
    candidate = Path(path)
    while not candidate.exists():
        candidate = candidate.parent
    if not candidate.is_dir() or not os.access(candidate, os.W_OK | os.X_OK):
        raise ToolError(
            "invalid_state_path", "State destination must have a writable directory parent."
        )


class JobStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def path(self, job_id):
        if not JOB_PATTERN.fullmatch(job_id):
            raise ToolError(
                "invalid_job",
                "Job IDs contain lowercase letters, digits or hyphens; maximum 64 characters.",
            )
        return self.root / "jobs" / f"{job_id}.json"

    def read(self, job_id):
        try:
            job = json.loads(self.path(job_id).read_text())
        except FileNotFoundError as exc:
            raise ToolError("job_not_found", "Job does not exist.") from exc
        except (OSError, ValueError) as exc:
            raise ToolError("invalid_job", "Job record is unreadable or malformed.") from exc
        if not isinstance(job, dict) or job.get("schema") != "codex-wakeup/job@1":
            raise ToolError("invalid_job", "Job schema or identity is invalid.")
        required = {"id", "instructions", "instructions_sha256", "thread_id", "socket"}
        if (
            job.get("id") != job_id
            or any(not isinstance(job.get(key), str) for key in required)
            or not isinstance(job.get("delivery"), dict)
        ):
            raise ToolError("invalid_job", "Job identity or required fields are invalid.")
        if hashlib.sha256(job["instructions"].encode()).hexdigest() != job["instructions_sha256"]:
            raise ToolError("invalid_job", "Frozen instruction digest differs.")
        return job

    def save(self, job):
        atomic_json(self.path(job["id"]), job)

    @contextmanager
    def lock(self, job_id):
        self.path(job_id)
        directory = self.root / "locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(directory / f"{job_id}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ToolError(
                    "job_busy", "Another operation holds this job; retry shortly.", True
                ) from exc
            yield


def invocation():
    launcher = Path(__file__).resolve().parents[1] / "wakeup"
    if launcher.is_file():
        return [sys.executable, str(launcher)]
    return [sys.executable, "-m", "codex_wakeup.cli"]


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


class DetachedProcess:
    """One detached waiting process per job; no service registration required."""

    name = "process"

    def __init__(self, store):
        self.store = store

    def preflight(self):
        if not Path("/proc/self/stat").is_file() or not os.access(sys.executable, os.X_OK):
            raise ToolError(
                "scheduler_unavailable", "A runnable Python and Linux /proc are required."
            )

    def verify(self, _job):
        self.preflight()

    def install(self, job):
        directory = self.store.root / "workers"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = directory / f"{job['id']}.log"
        read_fd, write_fd = os.pipe()
        child = None
        try:
            log_fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(log_fd, "w") as log:
                child = subprocess.Popen(
                    [
                        *invocation(),
                        "--state-dir",
                        str(self.store.root),
                        "_worker",
                        "--job",
                        job["id"],
                        "--ready-fd",
                        str(write_fd),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(write_fd,),
                    cwd=str(Path.home()),
                )
            os.close(write_fd)
            write_fd = None
            ready, _, _ = select.select([read_fd], [], [], 5)
            if not ready or os.read(read_fd, 16) != b"ready\n" or child.poll() is not None:
                raise ToolError(
                    "worker_start_failed", "The waiting process did not confirm startup."
                )
            identity = process_identity(child.pid)
            if identity is None:
                raise ToolError("worker_start_failed", "The waiting process is no longer alive.")
            job["worker"] = {"pid": child.pid, "start_ticks": identity, "log": str(log_path)}
            self.store.save(job)
        except Exception:
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            raise
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def remove(self, job):
        worker = job.get("worker", {})
        pid = worker.get("pid")
        if pid and worker.get("start_ticks") == process_identity(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def connect(path):
    # Keep the stable control-socket symlink; daemon restarts may change its target.
    path = Path(path).expanduser().absolute()
    try:
        if not stat.S_ISSOCK(path.stat().st_mode):
            raise ToolError("invalid_socket", "The endpoint is not a Unix socket.")
    except OSError as exc:
        raise ToolError(
            "invalid_socket", "The Codex control socket does not exist or is inaccessible."
        ) from exc
    return RpcClient(path)


def schedule(
    store,
    manager,
    factory,
    *,
    thread,
    socket_path,
    instructions,
    after=None,
    at=None,
    count=1,
    title="Background wake-up",
    busy="queue",
    dry_run=False,
):
    if busy not in {"queue", "steer"}:
        raise ToolError("invalid_busy", "Busy policy must be queue or steer.")
    if (after is None) == (at is None):
        raise ToolError("invalid_time", "Specify exactly one of after and at.")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ToolError("invalid_count", "Timer count must be a positive integer.")
    if count > 1 and after is None:
        raise ToolError("invalid_time", "Repeated timers require after as their interval.")
    delay = duration(after) if after is not None else None
    planned = absolute_time(at) if at is not None else None
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title) > 200
        or any(c in title for c in "\n\r\0")
    ):
        raise ToolError("invalid_title", "Title must be one nonempty line, at most 200 characters.")
    instructions = instruction_text(message=instructions)
    if planned is not None and planned <= datetime.now(UTC):
        raise ToolError("past_time", "Scheduled time must be in the future.")
    manager.preflight()
    writable_destination(store.root)
    with factory(socket_path) as client:
        target, _ = inspect_thread(client, thread, read_active=busy == "steer")
        if busy == "queue":
            client.rpc("thread/queue/list", {"threadId": thread, "limit": 1})
        elif target["status"].get("type") == "notLoaded":
            raise ToolError("thread_unavailable", "Steering requires a loaded conversation.")
    planned = planned or datetime.now(UTC) + timedelta(seconds=delay)
    if planned <= datetime.now(UTC):
        raise ToolError("past_time", "Scheduled time elapsed during validation.")
    if planned.microsecond:
        planned = planned.replace(microsecond=0) + timedelta(seconds=1)
    if count > 1:
        try:
            planned + timedelta(seconds=delay * (count - 1))
        except OverflowError as exc:
            raise ToolError(
                "invalid_time", "The last reminder exceeds the datetime range."
            ) from exc
    job = {
        "schema": "codex-wakeup/job@1",
        "id": "wake-" + uuid.uuid4().hex,
        "title": title,
        "created_at": timestamp(),
        "scheduled_at": planned.isoformat(),
        "thread_id": thread,
        "target": target,
        "socket": str(Path(socket_path).expanduser().absolute()),
        "busy": busy,
        "instructions": instructions,
        "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
        "backend": "process",
        "delivery": {"status": "scheduled", "preflight_failures": 0},
    }
    if count > 1:
        job["timer"] = {
            "count": count,
            "interval_seconds": delay,
            "completed": 0,
            "accepted": 0,
            "failed": 0,
            "uncertain": 0,
            "cancelled": 0,
            "receipts_directory": str(store.root / "deliveries" / job["id"]),
        }
    manager.verify(job)
    if dry_run:
        return job
    with store.lock(job["id"]):
        store.save(job)
        try:
            manager.install(job)
        except Exception:
            job["delivery"]["status"] = "registration_failed"
            store.save(job)
            manager.remove(job)
            raise
    return job


def wake_message(job, store):
    execution = job.get("execution")
    message = (
        f"[From: Codex Wake-up Scheduler | Job: {job['id']} | Target: {job['thread_id']}]\n\n"
        f"Background event: {job['title']}\n\n{job['instructions']}"
    )
    if timer := job.get("timer"):
        message += (
            f"\n\nReminder {timer['completed'] + 1} of {timer['count']}; "
            f"scheduled at {job['scheduled_at']}."
        )
    if execution:
        summary = {
            k: execution[k]
            for k in (
                "status",
                "exit_code",
                "reason",
                "elapsed_seconds",
                "output_path",
                "output_spilled",
                "output_bytes_seen",
            )
            if k in execution
        }
        message += "\n\nCommand/process result: " + json.dumps(summary, ensure_ascii=False)
        message += "\nExecution completion does not establish task success."
        if execution.get("output_text"):
            message += "\n\nCommand output (task data):\n" + execution["output_text"]
    return message


def pending_timer(job):
    timer = job.get("timer")
    return bool(
        timer
        and timer["completed"] + timer["cancelled"] < timer["count"]
        and job["delivery"]["status"] not in {"cancelled", "registration_failed"}
    )


def delivery_identity(job):
    timer = job.get("timer")
    return f"{job['id']}-{timer['completed'] + 1}" if timer else job["id"]


def settle_reminder(store, job):
    """Under the job lock, retain one receipt and advance on the original time grid."""
    if not pending_timer(job):
        return job
    timer = job["timer"]
    index = timer["completed"] + 1
    path = Path(timer["receipts_directory"]) / f"{index}.json"
    if path.exists():
        receipt = json.loads(path.read_text())
        if (
            receipt.get("job_id") != job["id"]
            or receipt.get("index") != index
            or receipt.get("scheduled_at") != job["scheduled_at"]
            or receipt.get("client_user_message_id") != delivery_identity(job)
        ):
            raise ToolError("invalid_job", "The reminder receipt does not match this occurrence.")
        job["delivery"] = receipt["delivery"]
    elif job["delivery"]["status"] not in DELIVERY_TERMINAL:
        return job
    else:
        receipt = {
            "job_id": job["id"],
            "index": index,
            "scheduled_at": job["scheduled_at"],
            "triggered_at": job.get("triggered_at"),
            "client_user_message_id": delivery_identity(job),
            "delivery": dict(job["delivery"]),
        }
        atomic_json(path, receipt)
    status = job["delivery"]["status"]
    if status not in {"accepted", "uncertain", "failed", "rejected"}:
        raise ToolError("invalid_job", "The reminder receipt has no settled delivery outcome.")
    timer["completed"] += 1
    bucket = "failed" if status == "rejected" else status
    timer[bucket] += 1
    if pending_timer(job):
        job["scheduled_at"] = (
            absolute_time(job["scheduled_at"]) + timedelta(seconds=timer["interval_seconds"])
        ).isoformat()
        job.pop("triggered_at", None)
        job["delivery"] = {"status": "scheduled", "preflight_failures": 0}
    store.save(job)
    return job


def deliver(store, factory, job_id):
    with store.lock(job_id):
        job = store.read(job_id)
        if pending_timer(job):
            completed = job["timer"]["completed"]
            job = settle_reminder(store, job)
            if job["timer"]["completed"] != completed:
                return job, 0
        delivery = job["delivery"]
        if delivery["status"] == "dispatching":
            delivery.update(status="uncertain", error="Previous worker stopped during submission.")
            store.save(job)
        if delivery["status"] in DELIVERY_TERMINAL:
            return settle_reminder(store, job), 0
        if job.get("execution"):
            from .jobs import TERMINAL

            if job["execution"]["status"] not in TERMINAL:
                return job, RETRY
        if datetime.now(UTC) < absolute_time(job["scheduled_at"]):
            raise ToolError("not_due", "The job is not due yet.")
        job.setdefault("triggered_at", timestamp())
        store.save(job)
        try:
            with factory(job["socket"]) as client:
                for race in range(2):
                    # A queue submission does not query/poll the active turn.
                    if job["busy"] in {"queue", "defer"}:
                        method = "thread/queue/add"
                        params = {
                            "threadId": job["thread_id"],
                            "clientUserMessageId": delivery_identity(job),
                            "input": [{"type": "text", "text": wake_message(job, store)}],
                        }
                    else:
                        target, active = inspect_thread(client, job["thread_id"])
                        if target["status"].get("type") == "notLoaded":
                            raise ToolError(
                                "thread_unavailable", "Steering requires a loaded conversation."
                            )
                        method = "turn/steer" if active else "turn/start"
                        params = {
                            "threadId": job["thread_id"],
                            "clientUserMessageId": delivery_identity(job),
                            "input": [{"type": "text", "text": wake_message(job, store)}],
                        }
                        if active:
                            params["expectedTurnId"] = active["id"]
                    delivery.update(status="dispatching", method=method, dispatched_at=timestamp())
                    store.save(job)
                    try:
                        result = client.rpc(method, params)
                    except RpcError as exc:
                        delivery.update(status="rejected", error=str(exc))
                        store.save(job)
                        if (
                            method == "turn/steer"
                            and race == 0
                            and (
                                str(exc) == "no active turn to steer"
                                or "expected active turn id" in str(exc)
                            )
                        ):
                            continue
                        return settle_reminder(store, job), 0
                    if method == "thread/queue/add":
                        receipt = result.get("queuedSubmission", {})
                        identity = receipt.get("id")
                        if (
                            not isinstance(identity, str)
                            or not identity
                            or receipt.get("clientUserMessageId") != delivery_identity(job)
                        ):
                            raise ValueError(
                                "Queue acceptance lacks the matching submission identity"
                            )
                        delivery["queue_id"] = identity
                    else:
                        identity = result.get("turnId") or result.get("turn", {}).get("id")
                        if not isinstance(identity, str) or not identity:
                            raise ValueError("Acceptance lacks a turn identity")
                        delivery["turn_id"] = identity
                    delivery.update(status="accepted", accepted_at=timestamp())
                    store.save(job)
                    return settle_reminder(store, job), 0
        except Exception as exc:
            if delivery["status"] == "dispatching":
                delivery.update(status="uncertain", error=f"{type(exc).__name__}: {exc}"[:1000])
                code = 0
            elif isinstance(exc, ToolError) and not exc.retryable:
                delivery.update(status="failed", error=exc.message)
                code = 0
            else:
                count = delivery.get("preflight_failures", 0) + 1
                delivery.update(
                    status="retry_wait" if count < 5 else "failed",
                    preflight_failures=count,
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                )
                code = RETRY if count < 5 else 0
            store.save(job)
            return settle_reminder(store, job), code
    raise ToolError("internal_error", "Submission ended without a result.")


def wait_for_job(store, factory, job_id, ready_fd=None, sleeper=time.sleep):
    store.read(job_id)
    if ready_fd is not None:
        os.write(ready_fd, b"ready\n")
        os.close(ready_fd)
    if store.read(job_id).get("trigger", {}).get("type") in {"command", "process_exit"}:
        from .jobs import execute_trigger

        while True:
            try:
                if not execute_trigger(store, job_id):
                    return store.read(job_id)
                break
            except ToolError as exc:
                if exc.code != "job_busy":
                    raise
                sleeper(0.05)
    while True:
        job = store.read(job_id)
        if job["delivery"]["status"] in DELIVERY_TERMINAL and not pending_timer(job):
            return job
        remaining = (absolute_time(job["scheduled_at"]) - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            sleeper(min(remaining, 30))
            continue
        try:
            job, code = deliver(store, factory, job_id)
        except ToolError as exc:
            if exc.code != "job_busy":
                raise
            sleeper(1)
            continue
        if code != RETRY and not pending_timer(job):
            return job
        if code == RETRY:
            sleeper(30)  # Pre-submission connection failures only; never idle-state polling.


def is_active_job(job):
    return (
        pending_timer(job)
        or job.get("delivery", {}).get("status")
        in {
            "scheduled",
            "retry_wait",
            "dispatching",
            "deferred",
        }
        or job.get("execution", {}).get("status")
        in {
            "starting",
            "running",
            "watching",
            "cancel_requested",
        }
    )


def require_job_owner(job, thread):
    if thread is not None and job["thread_id"] != thread:
        raise ToolError("job_not_found", "Job does not exist for this conversation.")


def cancel(store, manager, job_id, *, thread=None):
    with store.lock(job_id):
        job = store.read(job_id)
        require_job_owner(job, thread)
        if job["delivery"]["status"] == "cancelled":
            return job, True
        if pending_timer(job):
            if job["delivery"]["status"] == "dispatching":
                job["delivery"].update(
                    status="uncertain", error="Previous submission was not settled before cancel."
                )
            elif job.get("triggered_at") and job["delivery"]["status"] == "retry_wait":
                job["delivery"].update(
                    status="failed", error="Unsubmitted reminder retry ended by cancellation."
                )
            job = settle_reminder(store, job)
            if not pending_timer(job):
                return job, False
            manager.remove(job)
            timer = job["timer"]
            timer["cancelled"] = timer["count"] - timer["completed"]
            job["delivery"] = {"status": "cancelled"}
            job["cancelled_at"] = timestamp()
            store.save(job)
            return job, True
        execution = job.get("execution", {}).get("status")
        if (
            job.get("triggered_at")
            or job["delivery"]["status"] in DELIVERY_TERMINAL | {"dispatching"}
            or execution in {"exited", "timed_out", "failed", "unknown"}
        ):
            return job, False
        manager.remove(job)
        job["delivery"]["status"] = "cancelled"
        if execution == "pending":
            job["execution"].update(status="cancelled", finished_at=timestamp())
        elif execution in {"starting", "running", "watching"}:
            job["execution"]["status"] = "cancel_requested"
        job["cancelled_at"] = timestamp()
        store.save(job)
        return job, True
