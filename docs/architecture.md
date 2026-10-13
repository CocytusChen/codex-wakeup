# Execution, delivery and local state

A job is one background wait and its message delivery, or one finite timer series. It is separate from the
work that Codex performs after receiving that message. Submission returns promptly
with a job ID; a detached worker observes the trigger and then submits the input.

## Codex connection

The current implementation uses Codex App Server JSON-RPC through a Unix WebSocket
control socket. It reuses the server associated with the calling conversation.
Whether that Codex process runs in the foreground or background is not itself a
requirement. The conversation must belong to the selected server.

The default socket is `~/.codex/app-server-control/app-server-control.sock`.
`CODEX_HOME` changes its base directory; `CODEX_WAKEUP_SOCKET` or CLI `--socket`
selects a different existing socket. MCP and its Codex conversation run on the same
host. See [connection troubleshooting](installation.md#connection-troubleshooting)
and [version contracts](compatibility.md).

The installed `codex` command is used for installation, configuration and normal
client startup. The worker currently submits delivery RPCs directly through the
socket. MCP registration and successful later message delivery are distinct steps;
a dry run validates the current connection before creating any runnable job.

## CLI and caller identity

CLI takes the same argument object as MCP:

```sh
codex-wakeup wakeup --json '{"message":"Review the result.","trigger":{"type":"timer","after":"1h"}}'
codex-wakeup list_jobs --json '{}'
codex-wakeup cancel_job --json '{"job":"JOB_ID"}'
```

Use `--json -` to read the object from stdin. Global transport options precede the
operation:

```sh
codex-wakeup --state-dir /absolute/job-state --socket /absolute/control.sock list_jobs --json '{}'
```

MCP receives caller identity from host-supplied `_meta.threadId`; CLI receives it
from the Codex shell environment's `CODEX_THREAD_ID`. Identity is outside the
argument object. Missing identity refuses the operation. Queries and cancellation
remain scoped to that caller. This trusts the local host and control socket; it
is not isolation against a process that can modify its environment or access the
socket directly.

Both interfaces return one JSON result with `ok`, `tool`, and either `data` or
`error`. CLI uses exit status 0 for success and 1 for refusal; `--help` displays text.

## Delivery and cancellation

| `busy` | Delivery |
|---|---|
| `queue` (default) | Enqueue once, then start that exact input with native `thread/queue/start` when idle. Busy work stays queued. |
| `steer` | Use native `turn/steer` for an active turn, or `turn/start` when idle. |

Queue acceptance settles the occurrence. For a single job the worker exits; a
repeated timer waits until the next original scheduled time and exits after the
last occurrence. The worker
does not poll for an idle turn. After acceptance it checks the conversation once.
Native start-if-idle refuses a racing active or pending turn without interrupting
it. A successful start adds the returned turn ID to the receipt. Busy inputs remain
in Codex's queue for subsequent dispatch; acceptance is not task completion.

Supported unloaded conversations are restored using `thread/resume` with only the
thread ID and `excludeTurns: true`. Codex restores its own persisted settings;
the tool supplies no permission, approval, sandbox, model or configuration overrides.
Automatic restoration requires both server and saved-thread version 0.149.0+
and a completed saved turn with the persisted permission-profile contract.
Empty sessions and older or unknown contracts stay queued with an explicit error.
A refused start retains its accepted queued input;
a lost start reply is uncertain and is never retried or resubmitted.

Repeated timers can opt into `replace_pending`. After a new queue acceptance is
saved, the worker lists the queue and deletes only older submission identities of
its own series. Listing finishes before deletions to avoid shifting pagination.
The per-occurrence receipt retains confirmed removals, already absent inputs and
any uncertain deletion. The new acceptance remains settled even if cleanup fails.
This does not interrupt dispatched work or guarantee atomic queue replacement.

Steer can redirect active inference before the turn finishes. Codex controls
preemption and running-tool behavior; it continues the same active turn. Steering
requires a loaded conversation.

`cancel_job` acts before the trigger fires. It cancels a future timer, stops an
owned command, or ends a process observation. After triggering or settlement it
returns `cancelled: false` for a settled single job without recalling its message.
A repeated timer can cancel its future reminders after earlier ones were sent. A command
cancellation can remain settling briefly; query again when needed.

Transport uncertainty after submission is retained without automatic replay.
Inspect the job and Codex state before deciding what to do next.

## Output and history

Command stdout and stderr share one stream:

- `output_mode: "message"` includes complete output when it fits `output_limit`,
  default **1 KiB (1,024 bytes)**. Overflow saves **all** output to a file and sends
  result metadata and the file path.
- `output_mode: "file"` always saves complete output. `output_file` can select a
  new absolute path, otherwise the tool creates a unique file in its state directory.

The limit accepts 1 through 65,536 bytes and measures UTF-8 output bytes, not tokens
or characters. Existing files and symlinks refuse registration. Capture memory is
bounded; file growth depends on the command's output.

`list_jobs` defaults to active jobs. Use `include_completed: true` for settled
history, including an exact settled job ID. The compact result includes execution
and delivery status, output paths and observed worker availability. It does not
include instructions, command arguments or captured output.

State defaults to `$XDG_STATE_HOME/codex-wakeup`, or `~/.local/state/codex-wakeup`.
`CODEX_WAKEUP_STATE_DIR` or CLI `--state-dir` overrides it. Job records, lock files,
worker logs and saved command output remain there for inspection. Repeated timers
retain one small receipt per occurrence under `deliveries/JOB_ID/`; job progress
stays compact instead of embedding the complete history. There is no
automatic history pruning. Detached workers survive a tool disconnect, not a host
reboot. Keep unresolved records until their effects are understood.

See [the complete MCP/CLI argument reference](mcp.md).
