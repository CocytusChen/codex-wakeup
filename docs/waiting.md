# Read-only local and remote observation

The installed `codex-wakeup-wait` helper observes existing work. It does not launch
or modify that work. A turn or process ending is an inspection opportunity, not
proof that the entire task succeeded.

## Local observation

Validate an owner and running process:

```sh
codex-wakeup-wait probe --thread OWNER_THREAD --pid 12345
```

Wait for its exact PID/start/boot identity to end:

```sh
codex-wakeup-wait wait --thread OWNER_THREAD --pid 12345
```

Use `--turn EXACT_TURN_ID` to observe a delegated Codex turn. At least one of
`--turn` or `--pid` is required; with both, either event ends the wait. Observe the
work being delegated, rather than your own ending turn.

Exact-turn lookup is bounded to the 16 most recent turns. Missing metadata refuses
instead of selecting another turn. The helper reads status metadata, not private
reasoning. Transport errors indicate uncertainty, not failure of the observed task.

## SSH observation

Install the package on both hosts and configure noninteractive SSH authentication.
The remote helper must be available on that host's PATH, or selected explicitly:

```sh
codex-wakeup-wait probe --host compute-node --thread OWNER_THREAD --pid 12345 \
  --remote-command /absolute/path/to/codex-wakeup-wait
```

Submit the observer's `wait` command as a command trigger:

```json
{
  "message": "Inspect the event and continue the existing assignment.",
  "trigger": {
    "type": "command",
    "cwd": "/absolute/local/project",
    "argv": [
      "codex-wakeup-wait", "wait", "--host", "compute-node",
      "--thread", "REMOTE_OWNER_THREAD", "--pid", "12345",
      "--remote-command", "/absolute/path/to/codex-wakeup-wait"
    ]
  }
}
```

Use this argument object with MCP `wakeup` or CLI `wakeup --json`. The calling local
conversation remains the wake recipient; the observer's `--thread` identifies the
remote work being read.

`--socket` selects the local observation socket; `--remote-socket` selects the
remote one. The observer checks every 60 seconds by default. `--interval SECONDS`
accepts 5–3,600. The wait has no default deadline. Stopping the observer ends its
observation, without stopping the remote task.

Observation errors return nonzero; a command job delivers that outcome for
inspection. Keep a low-frequency fallback timer when useful and cancel pending
fallbacks once their purpose is fulfilled.
