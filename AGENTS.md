# Contributor instructions

Read [README.md](README.md), [the argument reference](docs/mcp.md), and the relevant
implementation before editing. This is a standalone Linux utility for detached
waits and Codex message delivery.

## Contract

- CLI and MCP expose the same three operations: `wakeup`, `list_jobs`, `cancel_job`.
  Keep their JSON arguments, defaults, validation and result envelopes identical.
- MCP caller identity comes from host-supplied per-call `_meta.threadId`; CLI uses
  host `CODEX_THREAD_ID`. Identity stays outside operation arguments. Missing
  identity refuses; submission, queries and cancellation remain self-scoped.
- Lists default to active jobs. `include_completed` exposes settled history.
  Keep routine results compact and preserve uncertain records for inspection.
- Validate trigger, endpoint, conversation, paths and output handling before work.
- Default queue delivery enqueues once, checks state once, and uses native
  start-if-idle on that exact input. Busy work stays queued. Restore supported
  unloaded sessions with Codex's persisted settings and no overrides; never send
  permission, approval, model or configuration fields. Older/unverifiable resume
  contracts stay queued with an explicit error. No idle polling or uncertain replay.
  Single jobs exit on acceptance; finite timers retain their original schedule.
  Steer delegates active-turn redirection to Codex.
- Optional repeated-timer `replace_pending` removes only older queued inputs of
  the same job after new acceptance. Preserve deletion uncertainty; never recall
  active work or delete unrelated input. Accepted submission remains settled.
- Cancel untriggered work, including the remaining occurrences of a timer series.
  Preserve prior accepted/uncertain receipts. Owned commands can be stopped; observed
  processes remain untouched. Keep process identity and cancellation race checks.
- Limit side effects to authorized job execution, storage and message delivery.
  Keep execution, submission acceptance and application success distinct. Never
  blindly repeat possibly started work or uncertain submissions.
- Keep inline command output bounded to 1 KiB by default, measured in bytes.
  Overflow saves the complete stream and reports the path.
- Essential calling rules belong in MCP descriptions/schemas. Markdown documents
  current use; it is not automatically injected into model context.

## Packaging and changes

- Use configurable paths and environment defaults; no private machines or IDs.
- Installed commands must work without the checkout, including detached workers
  and remote helpers. Package dependencies explicitly.
- Keep ordinary CLI dependencies minimal and MCP dependencies optional.
- Keep stdout to one JSON object per operation; `--help` is the text exception.
- Source work and deployment are separate. Register, reload or replace installed
  services only when requested.
- Upgrade installations manually only when no active jobs use them. A shared
  installation requires checking all calling conversations. Preserve settled
  job records during an upgrade.
- Exclude job state, credentials, personal notes, logs, caches and local deployment
  scripts from source archives.

## Verification

```sh
uv sync --extra mcp
uv run python -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
uv build
```

Use fake endpoints, detached fixture processes and actual MCP stdio transport.
Verify affected execution, output, cancellation, identity and uncertainty behavior.
Use temporary state, close listeners and settle fixture children. Do not create
real conversation wake-ups or model calls for routine tests. Check the installed
package outside its checkout before publishing.
