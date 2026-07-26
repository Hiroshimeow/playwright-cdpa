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
- Re-evaluates mutable nodes and requires consecutive bounded post-`COMPLETE` convergence; any nonterminal, missing-graph, unresolvable, or raw-content-changing observation resets the candidate window. Convergence fingerprints hash exact allowlisted in-memory graph material and expose only digests.
- Keeps access tokens and authorization headers in memory only.
- Persists prompt digest and length, not prompt body.
- Uses atomic JSON replacement, file and directory `fsync`, record revisions, and POSIX file locks.
- Separates repository-local turn results from deployment-wide conversation ownership, so clients with different `state_dir` values still serialize mutation of the same CDP conversation.
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
--coordination-dir
--deployment-id
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
            # Optional override. The default is shared under the user state
            # directory and namespaced by the normalized CDP endpoint.
            coordination_dir=Path("/var/tmp/playwright-gpt-coordination"),
            deployment_id="shared-cdp-9222",
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

## Persistence and coordination

Repository-local result state defaults to `.playwright-gpt/`:

```text
.playwright-gpt/
├── turns/<request-id>.json
└── locks/*.lock
```

Deployment-wide conversation ownership is separate. Its default base is an absolute `$XDG_STATE_HOME/playwright-gpt-core/coordination` when `XDG_STATE_HOME` is absolute, otherwise the core safely falls back to the absolute `~/.local/state/playwright-gpt-core/coordination`. The validated configuration freezes that base so later working-directory changes cannot move the coordination plane. It is namespaced by a digest of the normalized loopback CDP endpoint. `localhost`, `127.0.0.1`, and `::1` aliases for the same scheme and port use the same default namespace. Use `--deployment-id` when multiple persistent browser profiles share one endpoint, and use an absolute `--coordination-dir` when embedding clients must share an explicit location.

```text
<coordination-base>/<deployment-id>/
├── conversations/<sha256-of-conversation-id>.json
└── locks/*.lock
```

The coordination plane contains only conversation ID, active request ID, terminal request marker, revision, and timestamp. It never contains a repository state path or turn payload. A stale owner is not guessed away: competing send, wait-idle, and cancel operations fail closed until the exact owner is resolved.

State schema v4 contains allowlisted identity, state-machine provenance, the exact Chromium helper target ID and keep/closed lifecycle, hashes, lengths, revisions, timestamps, and sanitized failures. Every persisted scalar, enum, boolean, integer, hash, timestamp, identity, helper field, and cross-field state/provenance combination is decoded without coercion. A turn file is accepted only when its filename/requested ID exactly matches the embedded request ID. Assistant graph recipients must be explicit: final candidates require `recipient == "all"`, while tool-call assistants require an explicit non-`all` recipient. Invalid state or graph schema fails closed before browser mutation. State does not contain prompt bodies, response bodies, cookies, access tokens, authorization headers, or raw network payloads. Free-form sanitization uses the same normalized secret-label predicate for structured keys, assignments, query keys, and URL path contexts, including generic `*_secret`, `*_password`, credential/credentials, private/signing-key families, compact/camel variants, and credential-marker-plus-payload path segments.

## Documentation

- [Architecture and reliability contract](docs/architecture.md)
- [Live facts and remaining assumptions](docs/live-facts-and-assumptions.md)
- [Live acceptance evidence](docs/live-acceptance.md)
- [Future CDPA adapter design](docs/cdpa-adapter.md)
