"""Three JSON-first operations matching the MCP interface."""

import argparse
import json
import os
import sys

from .api import OPERATIONS, call
from .config import control_socket_path, state_directory
from .engine import JobStore, ToolError, connect, wait_for_job


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError("invalid_arguments", message)


def parser():
    result = Parser(description=__doc__)
    result.add_argument("--state-dir", default=str(state_directory()))
    result.add_argument("--socket", default=str(control_socket_path()))
    commands = result.add_subparsers(
        dest="command",
        required=True,
        parser_class=Parser,
        metavar="{wakeup,list_jobs,cancel_job,mcp}",
    )
    for operation in sorted(OPERATIONS):
        command = commands.add_parser(operation)
        command.add_argument("--json", default="{}", help="MCP argument object, or - for stdin.")
    commands.add_parser("mcp", help="Start the MCP stdio server.")
    worker = commands.add_parser("_worker", help=argparse.SUPPRESS)
    commands._choices_actions = [c for c in commands._choices_actions if c.dest != "_worker"]
    worker.add_argument("--job", required=True)
    worker.add_argument("--ready-fd", type=int)
    return result


def main(argv=None):
    args = None
    try:
        args = parser().parse_args(argv)
        if args.command == "mcp":
            from .mcp_server import serve

            return serve(args.state_dir, args.socket)
        if args.command == "_worker":
            wait_for_job(JobStore(args.state_dir), connect, args.job, args.ready_fd)
            return 0
        text = sys.stdin.read(1048577) if args.json == "-" else args.json
        if len(text.encode()) > 1048576:
            raise ToolError("invalid_arguments", "Argument JSON must be at most 1 MiB.")
        try:
            arguments = json.loads(text)
        except ValueError as exc:
            raise ToolError("invalid_arguments", "Supply a JSON argument object.") from exc
        value = call(
            args.command,
            arguments,
            thread=os.environ.get("CODEX_THREAD_ID"),
            state_dir=args.state_dir,
            socket_path=args.socket,
        )
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value["ok"] else 1
    except Exception as exc:
        error = (
            {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
            if isinstance(exc, ToolError)
            else {"code": "operation_failed", "message": str(exc)[:1000], "retryable": False}
        )
        print(json.dumps({"ok": False, "tool": getattr(args, "command", None), "error": error}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
