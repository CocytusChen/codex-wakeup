# Codex Wakeup

Wake your Codex conversation after a timer, command completion or process exit.
The tool returns a job ID promptly, waits independently and sends the requested
message back to your conversation. CLI and MCP share three operations.

## Requirements

- Linux or WSL; Python 3.11+ (the installer supplies 3.12).
- Codex on PATH, **0.148.0+**; **0.162.1** is the verified reference.
- A compatible Codex connection; [version details](docs/compatibility.md).

## Install and register MCP

Open a terminal in this downloaded/cloned project's folder and copy:

```sh
bash install.sh
```

The script checks Codex, installs `uv` when needed, installs the MCP dependencies,
and registers the installed executable. No administrator privileges are needed.
Then reopen your usual Codex client, or start the CLI:

```sh
codex
```

Inside Codex, enter `/mcp`. Confirm `codex_wakeup` provides `wakeup`, `list_jobs`
and `cancel_job`. Ask Codex to validate the connection without scheduling a job:

```text
Use codex_wakeup.wakeup with message "Installation check",
trigger {"type":"timer","after":"2m"}, and dry_run true.
```

Success reports `validation_passed: true` and `dry_run: true`.
See [installation and troubleshooting](docs/installation.md) if needed.

## Use

- `wakeup`: wait for a timer, an owned command, or an existing local process.
- `list_jobs`: query your active jobs; `include_completed: true` includes history.
- `cancel_job`: cancel pending reminders or your own waiting job.

Example arguments for MCP `wakeup`:

```json
{
  "message": "Inspect progress and continue.",
  "trigger": {"type":"timer","after":"1h","count":10}
}
```

This schedules hours 1 through 10 under one job; cancel it to stop the remainder.
After submission, end your turn when ready to wait. The tool handles delivery.
Each operation acts only on the calling conversation's jobs.
`queue` is the default; `steer` redirects active work through native Codex steering.

CLI accepts the same JSON arguments inside a Codex shell:

```sh
codex-wakeup wakeup --json '{"message":"Check progress.","trigger":{"type":"timer","after":"1h"}}'
codex-wakeup list_jobs --json '{}'
codex-wakeup cancel_job --json '{"job":"JOB_ID"}'
```

Command output defaults to **1 KiB (1,024 bytes)** inline. Overflow saves the
complete output to a file and reports its path. Job records remain available.

## Documentation

- [Arguments, defaults and result formats](docs/mcp.md)
- [Installation, configuration and troubleshooting](docs/installation.md)
- [Codex compatibility](docs/compatibility.md)
- [Execution, delivery and state storage](docs/architecture.md)
- [Local/SSH observation](docs/waiting.md)
- [Development](docs/development.md) and [contribution rules](AGENTS.md)

MIT licensed. Installed commands work independently of the source checkout.
