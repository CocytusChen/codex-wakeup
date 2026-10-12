# Installation and connection troubleshooting

Use Linux or WSL with Codex on your PATH. Minimum stable Codex is 0.148.0;
0.162.1 is the reference version. See [compatibility](compatibility.md).

## Quick start: copy and paste

Use a Linux or WSL terminal where `codex` is already installed and signed in.
Download or clone this project, then open a terminal in its folder containing
`install.sh` and `pyproject.toml`.

### 1. Check Codex

```sh
codex --version
```

The minimum is `codex-cli 0.148.0`; 0.162.1 is recommended. If your version is older,
update Codex using its installation method before continuing. The installer checks
this version and stops before installation when it is unsupported.

### 2. Install and add MCP

```sh
bash install.sh
```

This installs `uv` if needed, supplies Python 3.12, installs the package with MCP
dependencies, and registers `codex_wakeup` using the installed executable's
absolute path. It also displays the saved MCP configuration. No administrator
privileges are needed. The installed package is independent of this source folder.
The registration includes the package version in the server environment so
configuration refresh can distinguish an upgraded server at the same executable path.

### 3. Open or reopen Codex

```sh
codex
```

Use your usual Codex CLI, desktop app or IDE client. Reopen it after registration
so the MCP configuration is loaded. The tool uses your existing Codex connection;
the normal installation flow does not require a separate server-start command.

### 4. Verify the tools

Inside Codex, type:

```text
/mcp
```

Look for `codex_wakeup` with `wakeup`, `list_jobs` and `cancel_job`. Then send this
message to Codex:

```text
Use codex_wakeup.wakeup to validate a timer with message "Installation check",
trigger {"type":"timer","after":"2m"}, and dry_run true.
```

A successful result reports `validation_passed: true` and `dry_run: true`.
This verifies caller identity and delivery preflight without scheduling a wake-up.
If the server is missing, check `codex mcp get codex_wakeup` in your terminal and
reopen your client. For a connection error, see the troubleshooting section below.

## Connection troubleshooting

The current delivery implementation connects to Codex's Unix control socket.
Standard compatible installations can supply that connection automatically.
If dry-run reports that the socket is unavailable, verify that the MCP runs on
the same host and with the same `CODEX_HOME` as its Codex conversation. A custom
existing socket can be selected with `CODEX_WAKEUP_SOCKET`.

For a custom CLI installation without a shared connection, use Codex's supported
local-server setup and connect the conversation to it:

```sh
codex app-server daemon start
codex --remote unix://
```

Use this only when ordinary startup does not provide the required connection.
An existing daemon can be older than the CLI; `codex app-server daemon version`
shows both. If necessary, update it through Codex's normal workflow after existing
work settles. Adding MCP configuration does not upgrade a running daemon.

## Manual installation and configuration

With `uv` already installed, the equivalent package and registration steps are:

```sh
uv tool install --python 3.12 '.[mcp]'
codex mcp add codex_wakeup --env CODEX_WAKEUP_VERSION=0.5.1 -- "$(uv tool dir --bin)/codex-wakeup-mcp"
codex mcp get codex_wakeup
```

For CLI-only use, install with `uv tool install .`. A virtual environment with
`pip install '.[mcp]'` also works.

| Installed command | Purpose |
|---|---|
| `codex-wakeup` | The three JSON operations and MCP server entry |
| `codex-wakeup-mcp` | MCP stdio server |
| `codex-wakeup-wait` | Optional read-only local/SSH observer |

Editing the checkout does not change the installed copy. Reinstall when adopting
source changes. `./wakeup` and `./wait_control.py` are development launchers.

To configure a custom socket or state directory, use the installed executable's
absolute path in your Codex configuration:

```toml
[mcp_servers.codex_wakeup]
command = "/absolute/path/to/codex-wakeup-mcp"

[mcp_servers.codex_wakeup.env]
CODEX_WAKEUP_SOCKET = "/absolute/path/to/control.sock"
CODEX_WAKEUP_STATE_DIR = "/absolute/path/to/job-state"
```

The MCP server provides instructions, descriptions and schemas through discovery.
Markdown documentation is a user guide, not automatically loaded model context.
The registration format follows [official Codex MCP setup](https://learn.chatgpt.com/docs/extend/mcp);
Unix daemon connections follow [the CLI transport reference](https://learn.chatgpt.com/docs/cli/reference).
