# playwright-gpt-core

A fail-closed Python execution core for ChatGPT Web running in an existing persistent Chromium exposed through CDP.

The core uses the real ChatGPT composer and the real Send/Stop controls. It observes the unchanged frontend request/response, persists staged turn identity, monitors the authenticated conversation graph, and returns success only when the final response is proven to belong to the exact submitted or attached turn.

## Safety properties

- Connects only to a loopback CDP endpoint by default: `http://127.0.0.1:9222`.
- Never calls `browser.close()` on the persistent browser.
- Never installs request interception, aborts, fulfills, mutates, or replays the conversation POST.
- Persists the click boundary before the real Send click.
- Never retries automatically after an uncertain Send.
- Resolves only the exact current graph branch below the persisted user node.
- Re-evaluates mutable nodes and requires bounded post-`COMPLETE` convergence.
- Keeps access tokens and authorization headers in memory only.
- Persists prompt digest and length, not prompt body.
- Uses atomic JSON replacement, file and directory `fsync`, record revisions, and POSIX file locks.
- Persists the exact Chromium helper target ID and keep/closed policy; recovery never closes a tab by URL matching.

## Install

```bash
uv sync --all-groups
```

The package requires Python 3.10 or newer. Cross-process locking currently requires a POSIX platform because it uses `fcntl.flock`.

## CLI

Fresh conversation:

```bash
uv run playwright-gpt send "Reply with exactly OK" --fresh
```

Reuse a conversation ID or URL:

```bash
uv run playwright-gpt send "Continue" \
  --conversation '<conversation-id-or-https://chatgpt.com/c/...>'
```

Attach to an exact persisted request without sending:

```bash
uv run playwright-gpt watch '<request-id>'
```

Wait for the exact active request, revalidate idle state, then send:

```bash
uv run playwright-gpt send "Next task" \
  --conversation '<conversation-id>' \
  --wait-idle
```

Cancellation request:

```bash
uv run playwright-gpt cancel '<request-id>'
```

Machine-readable output:

```bash
uv run playwright-gpt watch '<request-id>' --json
```

Shared options include:

```text
--cdp-endpoint
--state-dir
--timeout
--poll
--send-timeout
--identity-timeout
--json
--keep-helper-tab
```

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Exact success, successful `get`, or cancel of an already complete request |
| 2 | Invalid input or configuration |
| 10 | Recoverable external failure |
| 11 | Terminal external failure |
| 12 | Timeout, missing proof, or ambiguous outcome requiring watch/recovery |
| 20 | Local invariant, schema, or state failure |
| 21 | Ownership/concurrency conflict |
| 22 | Cancellation positively represented |
| 23 | Cancellation requested but not proven |
| 130 | Interrupted by the operator |

JSON mode writes exactly one result object to stdout. Diagnostics are not mixed into stdout.

## Python API

```python
import asyncio
from pathlib import Path

from playwright_gpt_core import ChatGPTCore, CoreConfig


async def main() -> None:
    core = ChatGPTCore(
        CoreConfig(
            cdp_endpoint="http://127.0.0.1:9222",
            state_dir=Path(".playwright-gpt"),
            timeout=1800,
            poll=1.0,
        )
    )

    result = await core.send(
        "Reply with exactly OK",
        fresh=True,
    )
    if not result.success:
        raise RuntimeError(result.failure)
    print(result.response)

    attached = await core.watch(result.request_id)
    print(attached.state)


asyncio.run(main())
```

Primary methods:

- `send(...)`
- `wait_idle_and_send(...)`
- `watch(request_id)`
- `recover(request_id)`
- `cancel(request_id)`
- `get(request_id)`

Transport, state, graph resolution, locking, and browser behavior are implemented below the API and are not duplicated in the CLI.

## State directory

Default: `.playwright-gpt/`

```text
.playwright-gpt/
├── turns/<request-id>.json
├── conversations/<sha256-of-conversation-id>.json
└── locks/*.lock
```

State schema v4 contains allowlisted identity, state-machine provenance, the exact Chromium helper target ID and keep/closed lifecycle, hashes, lengths, revisions, timestamps, and sanitized failures. Helper lifecycle fields are decoded without coercion: keep policy must be an exact JSON boolean, target identity must be null or bounded printable ASCII, and the close marker must be null or a timezone-aware ISO timestamp. Invalid state fails as `corrupt_state` before CDP connection or browser mutation. State does not contain prompt bodies, response bodies, cookies, access tokens, authorization headers, or raw network payloads.

## Documentation

- [Architecture and reliability contract](docs/architecture.md)
- [Live facts and remaining assumptions](docs/live-facts-and-assumptions.md)
- [Live acceptance evidence](docs/live-acceptance.md)
- [Future CDPA adapter design](docs/cdpa-adapter.md)
