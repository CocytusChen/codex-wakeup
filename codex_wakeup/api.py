"""Shared three-operation contract for the CLI and MCP server."""

from .config import control_socket_path
from .engine import (
    DetachedProcess,
    JobStore,
    ToolError,
    cancel,
    connect,
    is_active_job,
    process_identity,
    schedule,
)
from .jobs import submit
from .rpc import RpcError

OPERATIONS = {"wakeup", "list_jobs", "cancel_job"}


def caller_identity(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ToolError(
            "caller_identity_unavailable", "The Codex host must supply caller identity."
        )
    return value


def object_fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ToolError(
            "invalid_arguments", "Supply the required fields and only supported fields."
        )


def boolean(value):
    if not isinstance(value, bool):
        raise ToolError("invalid_arguments", "Boolean arguments must be true or false.")
    return value


def validate(operation, arguments):
    if operation == "wakeup":
        object_fields(
            arguments, {"message", "trigger", "title", "busy", "dry_run"}, {"message", "trigger"}
        )
        if not isinstance(arguments["message"], str):
            raise ToolError("invalid_arguments", "Message must be text.")
        trigger = dict(arguments["trigger"]) if isinstance(arguments["trigger"], dict) else None
        kind = trigger.get("type") if trigger is not None else None
        if kind == "timer":
            object_fields(trigger, {"type", "after", "at", "count"})
            count = trigger.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ToolError("invalid_count", "Timer count must be a positive integer.")
            for key in {"after", "at"} & set(trigger):
                if trigger[key] is not None and not isinstance(trigger[key], str):
                    raise ToolError("invalid_arguments", "Timer times must be strings.")
        elif kind == "command":
            object_fields(
                trigger,
                {
                    "type",
                    "argv",
                    "cwd",
                    "timeout_seconds",
                    "output_mode",
                    "output_limit",
                    "output_file",
                },
                {"argv", "cwd"},
            )
            if not isinstance(trigger["cwd"], str):
                raise ToolError("invalid_arguments", "Command cwd must be a path string.")
            argv = trigger["argv"]
            if not isinstance(argv, list) or any(not isinstance(v, str) for v in argv):
                raise ToolError("invalid_arguments", "Command argv must be a string array.")
            mode = trigger.get("output_mode", "message")
            if not isinstance(mode, str) or mode not in {"message", "file"}:
                raise ToolError("invalid_output_mode", "Output mode must be message or file.")
            limit = trigger.get("output_limit", 1024)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 65536:
                raise ToolError("invalid_output_limit", "Output limit must be 1..65536 bytes.")
            if trigger.get("output_file") is not None and not isinstance(
                trigger["output_file"], str
            ):
                raise ToolError("invalid_output_file", "Output file must be a path string.")
        elif kind == "process_exit":
            object_fields(trigger, {"type", "pid", "timeout_seconds"}, {"pid"})
            if isinstance(trigger["pid"], bool) or not isinstance(trigger["pid"], int):
                raise ToolError("invalid_pid", "PID must be a positive integer.")
        else:
            raise ToolError("invalid_trigger", "Trigger must be timer, command or process_exit.")
        if kind != "timer" and trigger.get("timeout_seconds") is not None:
            timeout = trigger["timeout_seconds"]
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ToolError("invalid_timeout", "Timeout must be a positive number of seconds.")
        busy = arguments.get("busy", "queue")
        if not isinstance(busy, str) or busy not in {"queue", "steer"}:
            raise ToolError("invalid_busy", "Busy policy must be queue or steer.")
        return {
            "message": arguments["message"],
            "trigger": trigger,
            "title": arguments.get("title", "Background wake-up"),
            "busy": busy,
            "dry_run": boolean(arguments.get("dry_run", False)),
        }
    if operation == "list_jobs":
        object_fields(arguments, {"job", "limit", "include_completed"})
        limit = arguments.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ToolError("invalid_limit", "List limit must be 1..10000.")
        job = arguments.get("job")
        if job is not None and not isinstance(job, str):
            raise ToolError("invalid_arguments", "Job must be a job ID.")
        return {
            "job": job,
            "limit": limit,
            "include_completed": boolean(arguments.get("include_completed", False)),
        }
    if operation == "cancel_job":
        object_fields(arguments, {"job"}, {"job"})
        if not isinstance(arguments["job"], str):
            raise ToolError("invalid_arguments", "Job must be a job ID.")
        return arguments
    raise ToolError("invalid_arguments", "Unknown operation.")


def job_summary(job):
    execution = job.get("execution", {})
    worker = job.get("worker", {})
    summary = {
        "id": job["id"],
        "title": job["title"],
        "created_at": job["created_at"],
        "scheduled_at": job["scheduled_at"],
        "triggered_at": job.get("triggered_at"),
        "active": is_active_job(job),
        "execution": {
            k: v
            for k, v in execution.items()
            if k
            in {
                "status",
                "exit_code",
                "reason",
                "output_path",
                "output_bytes_seen",
                "output_spilled",
            }
        },
        "delivery": {
            k: v
            for k, v in job["delivery"].items()
            if k in {"status", "method", "error", "turn_id", "queue_id"}
        },
        "worker_alive": bool(
            worker.get("pid") and worker.get("start_ticks") == process_identity(worker["pid"])
        ),
    }
    if timer := job.get("timer"):
        summary["timer"] = {
            key: timer[key]
            for key in ("count", "completed", "accepted", "failed", "uncertain", "cancelled")
        }
        summary["timer"].update(
            remaining=timer["count"] - timer["completed"] - timer["cancelled"],
            next_at=job["scheduled_at"] if is_active_job(job) else None,
            receipts_directory=timer["receipts_directory"],
        )
    return summary


def execute(operation, arguments, *, thread, state_dir, socket_path=None):
    thread = caller_identity(thread)
    args = validate(operation, arguments)
    store = JobStore(state_dir)
    if operation == "wakeup":
        trigger = args["trigger"]
        options = {
            "thread": thread,
            "socket_path": socket_path or control_socket_path(),
            "instructions": args["message"],
            "title": args["title"],
            "busy": args["busy"],
            "dry_run": args["dry_run"],
        }
        if trigger["type"] == "timer":
            job = schedule(
                store,
                DetachedProcess(store),
                connect,
                after=trigger.get("after"),
                at=trigger.get("at"),
                count=trigger.get("count", 1),
                **options,
            )
        else:
            job = submit(store, connect, trigger=trigger, **options)
        return {"job": job_summary(job), "dry_run": args["dry_run"], "validation_passed": True}
    if operation == "list_jobs":
        jobs = (
            [store.read(args["job"])]
            if args["job"] is not None
            else [store.read(p.stem) for p in (store.root / "jobs").glob("*.json")]
        )
        jobs = [
            j
            for j in jobs
            if j["thread_id"] == thread and (args["include_completed"] or is_active_job(j))
        ]
        jobs.sort(key=lambda j: j["created_at"], reverse=True)
        return {
            "jobs": [job_summary(j) for j in jobs[: args["limit"]]],
            "total": len(jobs),
            "truncated": len(jobs) > args["limit"],
        }
    job, cancelled = cancel(store, DetachedProcess(store), args["job"], thread=thread)
    return {
        "job": job_summary(job),
        "cancelled": cancelled,
        "reason": "cancelled" if cancelled else "already_triggered_or_settled",
    }


def call(operation, arguments, *, thread, state_dir, socket_path=None):
    try:
        return {
            "ok": True,
            "tool": operation,
            "data": execute(
                operation, arguments, thread=thread, state_dir=state_dir, socket_path=socket_path
            ),
        }
    except Exception as exc:
        if isinstance(exc, ToolError):
            error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
        else:
            error = {
                "code": "rpc_rejected" if isinstance(exc, RpcError) else "operation_failed",
                "message": str(exc)[:1000],
                "retryable": False,
            }
        return {"ok": False, "tool": operation, "error": error}
