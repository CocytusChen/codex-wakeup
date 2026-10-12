"""MCP descriptions and schemas for the shared three-operation contract."""

import json
import sys
from collections.abc import Mapping
from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .api import call, caller_identity, validate
from .config import state_directory
from .engine import ToolError
from .jobs import MESSAGE_LIMIT


class TimerTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["timer"]
    after: str | None = None
    at: str | None = None
    count: int = Field(default=1, ge=1, description="Number of reminders; count>1 requires after.")
    replace_pending: bool = Field(
        default=False,
        description=(
            "After acceptance, remove older queued reminders of this series; "
            "requires count>1 and queue."
        ),
    )


class CommandTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["command"]
    argv: list[str]
    cwd: str
    timeout_seconds: float | None = None
    output_mode: Literal["message", "file"] = "message"
    output_limit: int = MESSAGE_LIMIT
    output_file: str | None = None


class ProcessTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["process_exit"]
    pid: int
    timeout_seconds: float | None = None


def create_server(state_dir=None, socket_path=None):
    server = MCPServer(
        "codex-wakeup",
        instructions=(
            "Wake only your calling conversation; Codex supplies its identity. "
            "Submit once, then end your turn to wait. queue delegates delivery to Codex; "
            "steer redirects its current turn. Timer count repeats on a fixed schedule. "
            "Timer replace_pending removes only older still-queued reminders of that series. "
            "Cancel pending reminders; a single job ends on accepted submission. "
            "Output defaults to 1024 bytes; "
            "overflow saves all output. "
            "Lists show active jobs; include_completed includes history. Inspect uncertainty; "
            "never blindly repeat possibly accepted work."
        ),
    )

    def invoke(operation, arguments, ctx):
        try:
            meta = ctx.request_context.meta
            thread = caller_identity(meta.get("threadId") if isinstance(meta, Mapping) else None)
            wire_args = (ctx.request_context.params or {}).get("arguments", {})
            validate(operation, wire_args)
            value = call(
                operation,
                arguments,
                thread=thread,
                state_dir=state_dir or state_directory(),
                socket_path=socket_path,
            )
        except ToolError as exc:
            value = {
                "ok": False,
                "tool": operation,
                "error": {"code": exc.code, "message": exc.message, "retryable": False},
            }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(value))],
            structured_content=value,
            is_error=not value["ok"],
        )

    @server.tool()
    def wakeup(
        message: str,
        trigger: TimerTrigger | CommandTrigger | ProcessTrigger,
        ctx: Context,
        title: str = "Background wake-up",
        busy: Literal["queue", "steer"] = "queue",
        dry_run: bool = False,
    ) -> CallToolResult:
        """Wake your own conversation after a timer, command or process exit; returns data.job.id.

        timer: after (2m/1h) or future ISO8601 at with UTC offset, exactly one.
        count defaults to 1; after=1h,count=10 fires at hours 1 through 10 under one job.
        count>1 requires after; reminders follow the original schedule without interval drift.
        replace_pending=true requires count>1 and queue; after accepting a new reminder,
        remove older still-queued reminders of this same job, without interrupting active work.
        command: argv and absolute cwd, optional timeout_seconds; default 1024 BYTES inline.
        Overflow saves COMPLETE output; file mode always saves it. process_exit: existing local PID.
        queue (default): native queued input; worker exits after the final scheduled reminder.
        steer: native Codex steering, which may preempt current inference; idle starts a turn.
        dry_run validates without storing a job or starting work. No default execution deadline.
        """
        return invoke(
            "wakeup",
            {
                "message": message,
                "trigger": trigger.model_dump(exclude_none=True),
                "title": title,
                "busy": busy,
                "dry_run": dry_run,
            },
            ctx,
        )

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def list_jobs(
        ctx: Context, job: str | None = None, limit: int = 20, include_completed: bool = False
    ) -> CallToolResult:
        """Query your active jobs, newest first, default limit 20. include_completed=true also
        returns settled history, including exact job IDs and uncertain/failed submissions.
        Repeated timers include counts, next time and receipt path; pending reminders stay active.
        Accepted Codex queue submission does not prove model execution.
        Results include execution/delivery/output paths, never instructions or command output.
        """
        return invoke(
            "list_jobs", {"job": job, "limit": limit, "include_completed": include_completed}, ctx
        )

    @server.tool()
    def cancel_job(job: str, ctx: Context) -> CallToolResult:
        """Cancel your own pending reminders, including the remainder of a repeated timer.
        An owned command stops; watched processes remain untouched. Sent inputs stay sent.
        A triggered or settled single job returns cancelled=false.
        """
        return invoke("cancel_job", {"job": job}, ctx)

    return server


def serve(state_dir=None, socket_path=None):
    create_server(state_dir, socket_path).run(transport="stdio")
    return 0


def main():
    from .cli import main as cli_main

    return cli_main([*sys.argv[1:], "mcp"])
