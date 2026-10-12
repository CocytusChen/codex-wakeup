# MCP and CLI argument reference

Both interfaces expose `wakeup`, `list_jobs`, and `cancel_job`, with identical JSON
argument objects and result envelopes. MCP receives the calling conversation from
host `_meta.threadId`; CLI uses host `CODEX_THREAD_ID`. All operations are scoped to
that caller. The server publishes these tools' descriptions and schemas through
MCP discovery.

CLI syntax:

```sh
codex-wakeup OPERATION --json '{"argument":"value"}'
```

`--json -` reads stdin. Global `--state-dir` and `--socket` select local transport
configuration and precede the operation. Installation and configuration are in
[README.md](../README.md). Minimum Codex: **0.148.0**; reference: **0.162.1**.
See [compatibility requirements](compatibility.md).

## wakeup

| Argument | Meaning | Default |
|---|---|---|
| `message` | Required nonempty instruction, at most 65,536 UTF-8 bytes | Required |
| `trigger` | Required timer, command or process-exit object | Required |
| `title` | One nonempty line, at most 200 characters | `Background wake-up` |
| `busy` | `queue` or `steer` | `queue` |
| `dry_run` | Validate without storing or executing | `false` |

### Timer

```json
{
  "message": "Inspect progress.",
  "trigger": {"type": "timer", "after": "1h", "count": 10},
  "dry_run": true
}
```

Exactly one of `after` and `at` is required. Relative durations use `s`, `m`, `h`
or `d`, from one second through ten years. Absolute `at` uses a future ISO8601
value with `Z` or a UTC offset. Times are stored in UTC and rounded up to a whole
second for the first reminder.

`count` is a positive integer, default 1. With `after: "1h", count: 10`, the same
job schedules reminders at hours 1, 2, ... 10. `after` supplies the interval;
`count > 1` requires `after`, while absolute `at` schedules one reminder. The final
scheduled time must fit the datetime range. Each reminder follows the original
schedule rather than waiting another interval after delivery or model completion.
If delivery is delayed, overdue occurrences are submitted sequentially.

One worker handles the series. Each occurrence has its own submission identity
and a retained receipt. Failed or uncertain occurrences are counted separately;
an uncertain occurrence is never resubmitted. Later scheduled occurrences continue.
Use the same job ID to query progress or cancel the remaining reminders.

Optional `replace_pending: true` requires `count > 1` and `busy: "queue"`.
After Codex accepts a new reminder, the tool removes older still-queued reminders
of this same job and conversation. Default `false` retains every reminder.
An already dispatched reminder is not recalled or interrupted; other jobs and
user messages are untouched. Accepted counts retain the original submissions;
`timer.replaced` counts confirmed queue removals. Cleanup is best effort: failures
or uncertainty are recorded separately and do not resend the accepted new input.
Queue listing/deletion and submission are separate operations, so a dispatch race
can still start an older reminder. Each new occurrence checks the queue once,
with pagination as needed; the tool does not poll between reminders.

### Command

```json
{
  "message": "Review the result file and continue.",
  "trigger": {
    "type": "command",
    "argv": ["python3", "work.py"],
    "cwd": "/absolute/project",
    "output_mode": "message",
    "output_limit": 1024
  }
}
```

| Trigger field | Meaning |
|---|---|
| `type` | `command` |
| `argv` | Nonempty string array; executable must be available |
| `cwd` | Accessible absolute working directory |
| `timeout_seconds` | Optional positive execution timeout; omitted means none |
| `output_mode` | `message` (default) or `file` |
| `output_limit` | Inline UTF-8 byte limit, default 1,024; range 1–65,536 |
| `output_file` | Optional new absolute output-file path with a writable parent |

`argv` is an argument array, not shell text. Use a foreground command; a launcher
that immediately exits is not a monitor for its detached work.
The command starts in its own session. stdout and stderr are combined. Small
output is included in the wake message; overflow saves the complete stream and
reports its path. File mode always writes the stream. The tool creates the file;
existing files and symlinks refuse. A nonzero command exit still delivers its
outcome for inspection.

### Process exit

```json
{
  "message": "Inspect the process outcome.",
  "trigger": {"type": "process_exit", "pid": 12345, "timeout_seconds": 3600}
}
```

`pid` must identify a running local process. The job binds its PID/start/boot
identity. Optional `timeout_seconds` ends observation; it never kills the process.

### Submission and delivery

Success returns `data.job.id`, `data.dry_run` and `data.validation_passed`. A dry run
starts no worker and stores no queryable job. Ordinary submission returns after
worker startup. End your turn when ready to wait.

`queue` delegates to Codex's native queue and settles each reminder on accepted enqueue;
subsequent model execution belongs to Codex. A repeated timer stays active until
its final occurrence settles or its remainder is cancelled. `steer` calls native steering when
active and starts a turn when idle. It may preempt inference within the active
turn; exact handling belongs to Codex. Steering requires a loaded conversation.

## list_jobs

Active jobs:

```json
{"limit": 20}
```

A specific active job:

```json
{"job": "JOB_ID"}
```

Settled history, including a specific settled job:

```json
{"job": "JOB_ID", "include_completed": true}
```

Omit `job` for all matching records. `limit` defaults to 20, accepts 1–10,000, and
results are newest first. `data.total` counts matching records before the limit;
`data.truncated` indicates omitted matches. Settled includes accepted, rejected,
failed, cancelled and uncertain outcomes; it does not imply success.

Each compact job reports:

- `id`, `title`, creation/schedule/trigger times and `active`.
- `execution`: known status, exit code, reason and output-file information.
- `delivery`: status, method, error and returned turn or queue identity.
- `worker_alive`: current PID/start-identity observation.
- Repeated timers also include `timer`: configured count, completed/accepted/failed/
  uncertain/cancelled counts, remaining count, next scheduled time and receipt path.
  Completed counts settled reminder attempts, not completed model tasks.
  `replace_pending` records the selected policy; `replaced` counts confirmed old
  queue removals. When used, `last_queue_replacement` reports cleanup status/counts.

Accepted native queue submission settles that occurrence even if Codex has not yet
executed its input. Repeated timers with remaining reminders stay active. Records and files remain available; the list filter does not
delete history. Read output files with ordinary file tools.

## cancel_job

```json
{"job": "JOB_ID"}
```

Before triggering, cancellation stops the job's wait and any owned command; a
watched process is untouched. For repeated timers, it cancels the pending remainder
even after earlier reminders were sent; those earlier inputs cannot be recalled. Returns `data.job`, `data.cancelled` and
`data.reason`. `cancelled: false` with `already_triggered_or_settled` means the job
has passed that cancellation boundary and has no cancellable reminders remaining.
An already cancelled job returns true.
Owned execution may still be settling briefly.

## Result envelope and errors

Success:

```json
{"ok": true, "tool": "list_jobs", "data": {"jobs": [], "total": 0, "truncated": false}}
```

Refusal:

```json
{
  "ok": false,
  "tool": "wakeup",
  "error": {"code": "invalid_trigger", "message": "Trigger must be timer, command or process_exit.", "retryable": false}
}
```

Unknown arguments and invalid types refuse before work starts. Missing host caller
identity returns `caller_identity_unavailable`. Invalid paths, unavailable
executables/processes, inaccessible conversations or unsupported queue APIs also
refuse preflight. An empty default query can mean the job settled; query history
explicitly.

Once an RPC may have been submitted, a lost reply is recorded as uncertain and is
not automatically replayed. Likewise, an interrupted worker does not restart a
possibly executed command. Inspect retained evidence and Codex state first.
