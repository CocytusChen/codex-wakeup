# Codex compatibility

**Minimum stable Codex version: 0.148.0. Reference version: 0.162.1.**
The Codex host issuing MCP calls and the App Server accepting deliveries must
both meet the minimum and provide the contracts below. A newer CLI can connect
to an older running daemon; the CLI's version alone does not upgrade that daemon.

## Version policy

| Version | Status |
|---|---|
| Before 0.148.0 | Unsupported: required native queue APIs are absent. |
| 0.148.0 | Earliest stable protocol baseline; official binary-generated schemas and publisher source qualified. |
| 0.149.0, 0.155.0, 0.161.0 | Required contracts checked in version-bound publisher source. |
| 0.162.1 | Current reference: host metadata, installed native APIs and package/transport tests checked. |
| Later releases | Eligible when the same contracts remain available; require validation after an incompatible change. |

There is no arbitrary upper version pin. Preflight checks the current conversation
and queue availability; MCP calls require host-supplied identity. These checks
refuse unavailable capabilities before creating a worker. They do not guarantee
that a future daemon update will retain every delivery behavior. If an accepted
submission's reply becomes uncertain, the tool preserves uncertainty rather than
replaying it.

## Required contracts

| Contract | Use |
|---|---|
| `_meta.threadId` supplied by Codex on every MCP call | Bind operations to their calling conversation. |
| Unix WebSocket App Server transport | Connect to the host's existing daemon. |
| `initialize.capabilities.experimentalApi: true` | Enable experimental queue methods. |
| `thread/read`, `thread/turns/list` with bounded `itemsView: "notLoaded"` | Validate conversations and inspect an active turn for steering. |
| `thread/queue/list`, `thread/queue/add` | Validate and submit native queued input. |
| Queue add's `clientUserMessageId` and matching `queuedSubmission` receipt | Bind acceptance to the submitted job. |
| `turn/steer` with `expectedTurnId`, or idle `turn/start` | Native steering delivery. |

MCP metadata is outside model arguments and is overwritten with the issuing
conversation by Codex. The scheduler never substitutes another conversation when
that identity is missing.

## Evidence and scope

Native queue APIs were added in publisher commit
[`9341b383`](https://github.com/openai/codex/commit/9341b38310c73957e1313eab3f7c4034689bdec9)
and first appear in stable
[0.148.0](https://github.com/openai/codex/releases/tag/rust-v0.148.0).
Its preceding stable 0.147.0 lacks those methods.

The official 0.148.0 Linux binary generated experimental schemas containing all
six required request methods, queue receipt identities, bounded history queries
and native steering fields. Version-bound
[queue code](https://github.com/openai/codex/blob/rust-v0.148.0/codex-rs/app-server/src/request_processors/thread_queue_processor.rs),
[MCP identity code](https://github.com/openai/codex/blob/rust-v0.148.0/codex-rs/core/src/mcp_tool_call.rs)
and [Unix/CLI entry code](https://github.com/openai/codex/blob/rust-v0.148.0/codex-rs/cli/src/main.rs)
establish the minimum contracts. This is protocol/source qualification, not a
real model wake-up test on every release.

See official [MCP setup](https://learn.chatgpt.com/docs/extend/mcp) and
[App Server transport guidance](https://learn.chatgpt.com/docs/cli/reference).
