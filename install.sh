#!/usr/bin/env bash
# Install the package and register its stdio server in the user's Codex config.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

if ! command -v codex >/dev/null 2>&1; then
    printf 'Install Codex and make the codex command available first.\n' >&2
    exit 1
fi

version_text="$(codex --version)"
if [[ ! "$version_text" =~ ^codex-cli[[:space:]]+([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    printf 'Use a stable Codex release, minimum 0.148.0. Received: %s\n' "$version_text" >&2
    exit 1
fi
major=$((10#${BASH_REMATCH[1]}))
minor=$((10#${BASH_REMATCH[2]}))
if ((major == 0 && minor < 148)); then
    printf 'Codex 0.148.0 or newer is required; installed: %s\n' "$version_text" >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    if ! command -v curl >/dev/null 2>&1; then
        printf 'Install curl or uv, then run this script again.\n' >&2
        exit 1
    fi
    installer="$(mktemp)"
    trap 'rm -f -- "$installer"' EXIT
    curl --fail --silent --show-error --location https://astral.sh/uv/install.sh -o "$installer"
    uv_install_dir="${UV_INSTALL_DIR:-$HOME/.local/bin}"
    UV_INSTALL_DIR="$uv_install_dir" UV_NO_MODIFY_PATH=1 sh "$installer"
    export PATH="$uv_install_dir:$PATH"
fi

(cd -- "$project_dir" && uv tool install --python 3.12 '.[mcp]')
bin_dir="$(uv tool dir --bin)"
mcp_command="$bin_dir/codex-wakeup-mcp"
if [[ "$mcp_command" != /* || ! -x "$mcp_command" ]]; then
    printf 'The installed MCP executable is unavailable: %s\n' "$mcp_command" >&2
    exit 1
fi
codex mcp add codex_wakeup -- "$mcp_command"
codex mcp get codex_wakeup

printf '\nInstalled and registered. Start or reopen a Codex conversation and check /mcp.\n'
