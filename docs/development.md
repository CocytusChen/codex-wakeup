# Development


```sh
uv sync --extra mcp
uv run python -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
uv build
```

Tests use fake endpoints, detached processes and the MCP stdio protocol. They do
not schedule real model work. [AGENTS.md](../AGENTS.md) describes contribution rules.
